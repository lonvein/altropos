"""Персистентная очередь фоновых заданий в SQLite (таблица jobs).

Обеспечивает:
- Атомарный захват задач (BEGIN IMMEDIATE) для последовательной обработки.
- Статусы жизненного цикла: queued -> processing -> ready / needs_review / failed.
- Восстановление прерванных задач (processing -> queued) при перезапуске сервиса.
- Изоляцию ошибок: сбой в одном файле не останавливает очередь.
"""
from __future__ import annotations

import json
from uuid import uuid4

from navigator.db import Store


class JobQueue:
    def __init__(self, store: Store):
        self.store = store

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
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT id, kind, payload FROM jobs WHERE status='queued' ORDER BY created ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job_id = row["id"]
            con.execute(
                "UPDATE jobs SET status='processing', updated=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
            return {
                "id": job_id,
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
            }

    def complete_job(
        self,
        job_id: str,
        status: str,
        result: dict | None = None,
        error: str | None = None,
    ):
        """Завершает задание с результатом или ошибкой."""
        res_json = json.dumps(result, ensure_ascii=False) if result is not None else None
        self.store.execute(
            "UPDATE jobs SET status=?, result=?, error=?, updated=CURRENT_TIMESTAMP WHERE id=?",
            (status, res_json, error, job_id),
        )

    def recover_stale_jobs(self) -> int:
        """Сбрасывает зависшие при аварийном завершении задания 'processing' обратно в 'queued'."""
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE jobs SET status='queued', error='Восстановлено после перезапуска системы', "
                "updated=CURRENT_TIMESTAMP WHERE status='processing'"
            )
            return cur.rowcount
