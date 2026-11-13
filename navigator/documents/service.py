"""Сервис управления жизненным циклом документов, очередью и индексацией."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from navigator.config import Settings, get_settings
from navigator.db import Store
from navigator.documents.queue import JobQueue
from navigator.documents.storage import DocumentStorage
from navigator.ingestion import parse_file
from navigator.ingestion.common import ParseError, document_ids, sha256_bytes, to_fragments
from navigator.ingestion.metadata_extractor import extract_metadata
from navigator.interfaces import Indexer
from navigator.schemas import Document, MetadataPatch

logger = logging.getLogger(__name__)


class InitialCorpusReviewRequired(ParseError):
    """Initial corpus bytes may only enter search through approved snapshots."""


def _resolve_indexer(store: Store, settings: Settings, indexer: Indexer | None = None) -> Indexer:
    if indexer is not None:
        return indexer
    # Never hide import/configuration failures behind an incompatible fake shard.
    from navigator.rag.indexing import Indexer as RAGIndexer
    return RAGIndexer(store=store, settings=settings)


class DocumentService:
    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        indexer: Indexer | None = None,
    ):
        self.store = store
        self.settings = settings or get_settings()
        self.storage = DocumentStorage(
            root=self.store.root,
            max_upload_mb=self.settings.max_upload_mb,
        )
        self.queue = JobQueue(store=self.store)
        self.indexer = _resolve_indexer(self.store, self.settings, indexer)

    def _require_manual_upload_allowed(self, sha: str) -> None:
        manifest_path = self.settings.corpus_manifest
        if manifest_path is None:
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                    or not isinstance(manifest.get("files"), list)):
                raise ValueError("Unsupported manifest")
            for item in manifest["files"]:
                if (not isinstance(item, dict) or item.get("group") not in {"initial", "manual_test"}
                        or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64):
                    raise ValueError("Invalid corpus group")
                if item["sha256"] == sha and item["group"] == "initial":
                    raise InitialCorpusReviewRequired("Initial source requires approved extraction")
        except InitialCorpusReviewRequired:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise InitialCorpusReviewRequired("Corpus manifest is unavailable or invalid") from exc

    def upload_file(self, filename: str, content: bytes) -> dict:
        """Прием файла, дедупликация по SHA-256 и постановка в очередь обработки."""
        ext, media_type = self.storage.validate_file(filename, content)
        sha = sha256_bytes(content)
        doc_id, version = document_ids(sha)

        # 1. Проверяем наличие по SHA-256 (дедупликация)
        existing = self.store.rows("SELECT id, status, payload FROM documents WHERE sha=?", (sha,))
        if existing:
            ex_id = existing[0]["id"]
            ex_status = existing[0]["status"]
            # Ищем последнее задание для этого документа
            jobs = self.store.rows(
                "SELECT id FROM jobs WHERE json_extract(payload, '$.document_id')=? ORDER BY created DESC LIMIT 1",
                (ex_id,),
            )
            job_id = jobs[0]["id"] if jobs else None
            return {
                "document_id": ex_id,
                "job_id": job_id,
                "status": ex_status,
                "duplicate": True,
            }

        # 2. Новый файл: сохраняем оригинал
        self._require_manual_upload_allowed(sha)
        doc_id, version, rel_path, sha, media_type = self.storage.save_original(filename, content)

        # 3. Регистрируем документ со статусом 'queued'
        title = Path(filename).stem
        doc = Document(
            id=doc_id,
            version=version,
            name=filename,
            title=title[:500],
            sha256=sha,
            original_path=rel_path,
            media_type=media_type,
            status="queued",
            metadata=MetadataPatch(),
            warnings=[],
        )
        self.store.register_document(doc)

        # 4. Добавляем задание в очередь
        job_id = self.queue.enqueue(
            "process_file",
            {
                "document_id": doc_id,
                "version": version,
                "filename": filename,
                "rel_path": rel_path,
                "media_type": media_type,
            },
        )
        return {
            "document_id": doc_id,
            "job_id": job_id,
            "status": "queued",
            "duplicate": False,
        }

    def process_next_job(self) -> bool:
        """Выполняет одну задачу из очереди. Возвращает True, если задача была найдена и обработана."""
        job = self.queue.acquire_next_job()
        if not job:
            return False

        job_id = job["id"]
        payload = job["payload"]
        doc_id = payload["document_id"]
        version = payload.get("version", "v1")
        filename = payload.get("filename", "")
        rel_path = payload["rel_path"]

        try:
            full_path = self.storage.get_original_path(rel_path)
            registered = self.store.document(doc_id)
            if registered is None or sha256_bytes(full_path.read_bytes()) != registered["sha"]:
                raise ValueError("Changed original requires a new immutable document version")
            self._require_manual_upload_allowed(registered["sha"])
            if full_path.suffix.lower() == ".pdf":
                from navigator.ingestion.ocr import make_ocr
                parsed = parse_file(full_path, ocr=make_ocr(
                    tesseract_cmd=self.settings.tesseract_cmd,
                    tessdata_dir=self.settings.tessdata_prefix,
                ))
            else:
                parsed = parse_file(full_path)

            # Сохраняем адресованные блоки для просмотрщика источников
            self.storage.save_extracted_blocks(doc_id, parsed.blocks)

            # Нарезаем черновики на фрагменты
            drafts = parsed.build()
            fragments = to_fragments(doc_id, version, drafts)

            # Метаданные
            meta = parsed.metadata or extract_metadata(parsed.units, title_hint=parsed.title, filename=filename)
            previous = self.store.document(doc_id)
            if previous and previous.get("payload"):
                saved = Document.model_validate_json(previous["payload"]).metadata
                meta = MetadataPatch.model_validate({**meta.model_dump(), **saved.model_dump(exclude_none=True)})
            title = (meta.title or parsed.title or Path(filename).stem)[:500]
            if title == Path(rel_path).stem:
                # Parsers see an internal storage ID, not the uploaded filename.
                title = Path(filename).stem[:500]
                meta = meta.model_copy(update={"title": title})

            # Формируем обновленный документ
            requires_review = parsed.needs_review or not fragments
            target_status = "needs_review" if requires_review else "processing"
            doc = Document(
                id=doc_id,
                version=version,
                name=filename,
                title=title,
                sha256=sha256_bytes(full_path.read_bytes()),
                original_path=rel_path,
                media_type=parsed.media_type,
                status=target_status,
                metadata=meta,
                warnings=[w[:500] for w in parsed.warnings],
            )

            # Обновляем документ в SQLite
            self.store.execute(
                "UPDATE documents SET title=?, metadata=?, payload=?, status=? WHERE id=?",
                (title, meta.model_dump_json(), doc.model_dump_json(), target_status, doc_id),
            )

            if requires_review:
                # Документ требует проверки оператором (например, нечитаемый скан)
                # Он не включается в поиск до проверки (не публикуется)
                self.queue.complete_job(
                    job_id,
                    status="needs_review",
                    result={
                        "document_id": doc_id,
                        "fragment_count": len(fragments),
                        "warnings": parsed.warnings,
                    },
                )
                return True

            # Публикуем только через совместимый RAG-индексатор
            index_res = self.indexer.index_document(doc_id, fragments)

            self.store.execute("UPDATE documents SET payload=json_set(payload, '$.status', status) WHERE id=?", (doc_id,))
            self.queue.complete_job(
                job_id,
                status="ready",
                result={
                    "document_id": doc_id,
                    "publication_id": index_res.publication_id,
                    "mode": index_res.mode,
                    "fragment_count": index_res.fragment_count,
                    "warnings": index_res.warnings,
                },
            )
            return True

        except Exception:
            logger.exception("Ошибка обработки документа %s в задаче %s", doc_id, job_id)
            self.store.execute(
                "UPDATE documents SET status='failed', payload=json_set(payload, '$.status', 'failed') WHERE id=?",
                (doc_id,),
            )
            self.queue.complete_job(
                job_id,
                status="failed",
                error="Ошибка обработки файла. Подробности в локальном журнале сервера.",
            )
            return True

    def run_all_jobs(self, max_jobs: int = 100) -> int:
        """Синхронно обрабатывает все накопившиеся задачи в очереди."""
        processed = 0
        while processed < max_jobs and self.process_next_job():
            processed += 1
        return processed

    def retry_document(self, doc_id: str) -> dict:
        """Reprocess unchanged bytes without pretending metadata was reviewed."""
        from uuid import uuid4
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if row is None or not row["payload"]:
                raise ValueError("Документ не найден")
            if row["status"] not in {"needs_review", "failed"}:
                raise ValueError("Повтор доступен только для документов с ошибкой или требующих проверки")
            doc = Document.model_validate_json(row["payload"])
            doc.status = "queued"
            job_id = uuid4().hex
            payload = {"document_id": doc.id, "version": doc.version, "filename": doc.name,
                       "rel_path": doc.original_path, "media_type": doc.media_type}
            con.execute("UPDATE documents SET status='queued',payload=? WHERE id=?",
                        (doc.model_dump_json(), doc_id))
            con.execute("INSERT INTO jobs(id,kind,payload) VALUES(?,?,?)",
                        (job_id, "process_file", json.dumps(payload, ensure_ascii=False)))
        return {"document_id": doc_id, "job_id": job_id, "status": "queued"}

    def get_document(self, doc_id: str) -> dict | None:
        row = self.store.document(doc_id)
        if not row:
            return None
        return self._format_doc_record(row)

    def list_documents(self) -> list[dict]:
        rows = self.store.rows("SELECT * FROM documents ORDER BY created DESC")
        return [self._format_doc_record(dict(r)) for r in rows]

    def patch_metadata(self, doc_id: str, patch: MetadataPatch) -> dict:
        # Read/merge/write and optional enqueue must be one transaction. A worker
        # marks processing while holding the same SQLite writer lock.
        with self.store.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not row or not row["payload"]:
                raise ValueError("Документ не найден")
            if row["status"] == "processing":
                raise ValueError("Дождитесь окончания обработки")
            doc = Document.model_validate_json(row["payload"])
            updated_meta = MetadataPatch.model_validate({
                **doc.metadata.model_dump(), **patch.model_dump(exclude_unset=True),
            })
            doc.title = (updated_meta.title or doc.title)[:500]
            doc.metadata = updated_meta
            doc.status = row["status"]
            reprocess = patch.reviewed is True and doc.status == "needs_review"
            if reprocess:
                doc.status = "queued"
            con.execute("UPDATE documents SET title=?,metadata=?,payload=?,status=? WHERE id=?",
                        (doc.title, updated_meta.model_dump_json(), doc.model_dump_json(), doc.status, doc_id))
            if reprocess:
                from uuid import uuid4
                payload = {"document_id": doc.id, "version": doc.version, "filename": doc.name,
                           "rel_path": doc.original_path, "media_type": doc.media_type}
                con.execute("INSERT INTO jobs(id,kind,payload) VALUES(?,?,?)",
                            (uuid4().hex, "process_file", json.dumps(payload, ensure_ascii=False)))
        return self.get_document(doc_id)

    def _format_doc_record(self, row: dict) -> dict:
        meta = row.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        warnings = []
        if row.get("payload"):
            try:
                payload = json.loads(row["payload"])
                warnings = payload.get("warnings", [])
            except Exception:
                pass
        fragment_id = None
        if row["status"] == "ready":
            fragments = self.store.rows("SELECT id FROM fragments WHERE document_id=? ORDER BY ordinal LIMIT 1", (row["id"],))
            fragment_id = fragments[0]["id"] if fragments else None
        return {
            "fragment_id": fragment_id,
            "presentation_url": f"/api/documents/{row['id']}/view" if fragment_id else None,
            "id": row["id"],
            "name": row["name"],
            "title": row["title"],
            "status": row["status"],
            "warnings": warnings,
            "metadata": meta,
            "original_path": row.get("path"),
        }
