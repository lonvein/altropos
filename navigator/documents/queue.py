"""Персистентная очередь фоновых заданий в SQLite (таблица jobs).

Обеспечивает:
- Атомарный захват задач (BEGIN IMMEDIATE) для последовательной обработки.
- Статусы жизненного цикла: queued -> processing -> ready / needs_review / failed.
- Восстановление прерванных задач (processing -> queued) при перезапуске сервиса.
- Изоляцию ошибок: сбой в одном файле не останавливает очередь.
"""
from __future__ import annotations

import json
import os
import threading
from uuid import uuid4

from navigator.db import Store


class JobQueue:
    def __init__(self, store: Store):
        self.store = store
        self._guard = threading.Lock()
        self._lease = None
        self._job_id = None

    def _lock_writer(self) -> bool:
        # OS lock survives for the entire parse/index/publish operation; a process
        # crash releases it. Another service must never recover a live writer.
        if self._lease is not None:
            return False
        stream = (self.store.root / "document-worker.lock").open("a+b")
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                if stream.read(1) == b"":
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._lease = stream
        return True

    def _unlock_writer(self):
        stream, self._lease = self._lease, None
        if stream is not None:
            stream.close()
        self._job_id = None

    @staticmethod
    def _recover(con) -> int:
        con.execute("""UPDATE documents SET status='queued',
            payload=json_set(payload, '$.status', 'queued')
            WHERE status='processing' AND id IN
            (SELECT json_extract(payload, '$.document_id') FROM jobs
             WHERE status='processing' AND kind='process_file')""")
        cur = con.execute(
            "UPDATE jobs SET status='queued', error='Восстановлено после перезапуска системы', "
            "updated=CURRENT_TIMESTAMP WHERE status='processing'"
        )
        return cur.rowcount

    def enqueue(self, kind: str, payload: dict) -> str:
        """Создает новую задачу со статусом 'queued'."""
        job_id = uuid4().hex
        payload_json = json.dumps(payload, ensure_ascii=False)
        self.store.execute(
            "INSERT INTO jobs(id, kind, payload, status) VALUES(?, ?, ?, 'queued')",
            (job_id, kind, payload_json),
        )
        return job_id

    def get_job(self, job_id: str) -> dict | None:
        """Возвращает информацию о задании."""
        rows = self.store.rows("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows:
            return None
        row = dict(rows[0])
        try:
            row["payload"] = json.loads(row["payload"])
        except Exception:
            pass
        if row.get("result"):
            try:
                row["result"] = json.loads(row["result"])
            except Exception:
                pass
        return row

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """Возвращает список последних задач."""
        if status:
            rows = self.store.rows(
                "SELECT * FROM jobs WHERE status=? ORDER BY created DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.store.rows(
                "SELECT * FROM jobs ORDER BY created DESC LIMIT ?",
                (limit,),
            )
        result = []
        for r in rows:
            row = dict(r)
            try:
                row["payload"] = json.loads(row["payload"])
            except Exception:
                pass
            if row.get("result"):
                try:
                    row["result"] = json.loads(row["result"])
                except Exception:
                    pass
            result.append(row)
        return result

    def acquire_next_job(self) -> dict | None:
        """Атомарно захватывает самое старое задание в статусе 'queued' и переводит его в 'processing'."""
        with self._guard:
            if not self._lock_writer():
                return None
            try:
                with self.store.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    self._recover(con)
                    row = con.execute(
                        "SELECT id, kind, payload FROM jobs WHERE status='queued' ORDER BY created ASC, rowid ASC LIMIT 1"
                    ).fetchone()
                    if not row:
                        self._unlock_writer()
                        return None
                    con.execute("UPDATE jobs SET status='processing', updated=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
                    payload = json.loads(row["payload"])
                    if row["kind"] == "process_file":
                        con.execute("UPDATE documents SET status='processing', payload=json_set(payload, '$.status', 'processing') WHERE id=?",
                                    (payload["document_id"],))
                    self._job_id = row["id"]
                    return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"])}
            except BaseException:
                self._unlock_writer()
                raise

    def complete_job(
        self,
        job_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ):
        """Завершает задание с результатом или ошибкой."""
        with self._guard:
            if self._job_id != job_id:
                raise ValueError("Job is not owned by this worker")
            res_json = json.dumps(result, ensure_ascii=False) if result is not None else None
            try:
                self.store.execute(
                    "UPDATE jobs SET status=?, result=?, error=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                    (status, res_json, error, job_id),
                )
            finally:
                self._unlock_writer()

    def recover_stale_jobs(self) -> int:
        """Recover only after acquiring the writer lock, never reset a live job."""
        with self._guard:
            if not self._lock_writer():
                return 0
            try:
                with self.store.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    return self._recover(con)
            finally:
                self._unlock_writer()
