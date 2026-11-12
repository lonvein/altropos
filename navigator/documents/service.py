"""Сервис управления жизненным циклом документов, очередью и индексацией."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from navigator.config import Settings, get_settings
from navigator.db import Store, fragments_digest
from navigator.documents.queue import JobQueue
from navigator.documents.storage import DocumentStorage
from navigator.ingestion import parse_file
from navigator.ingestion.common import document_ids, sha256_bytes, to_fragments
from navigator.ingestion.metadata_extractor import extract_metadata
from navigator.interfaces import Indexer
from navigator.schemas import Artifact, Document, Fragment, IndexManifest, IndexResult, MetadataPatch

logger = logging.getLogger(__name__)


class FallbackBM25Indexer:
    """Легковесный BM25-индексатор для автономного режима и тестов (до подключения Климовского RAG)."""

    def __init__(self, store: Store):
        self.store = store

    def index_document(self, document_id: str, fragments: list[Fragment]) -> IndexResult:
        row = self.store.document(document_id)
        if not row or not row.get("payload"):
            raise ValueError(f"Документ {document_id} не зарегистрирован в Store")
        doc = Document.model_validate_json(row["payload"])

        pub_id = uuid4().hex
        pub_dir = self.store.root / "indexes" / pub_id
        pub_dir.mkdir(parents=True, exist_ok=True)

        # Подготавливаем файл BM25 артефакта
        ordered = sorted(fragments, key=lambda f: f.ordinal)
        bm25_data = {
            "format": 1,
            "document_id": document_id,
            "ids": [f.id for f in ordered],
            "texts": [f.text for f in ordered],
        }
        bm25_bytes = json.dumps(bm25_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        (pub_dir / "bm25.json").write_bytes(bm25_bytes)

        digest = fragments_digest(ordered)
        prefix = f"indexes/{pub_id}/"
        bm25_artifact = Artifact(
            path=f"{prefix}bm25.json",
            sha256=hashlib.sha256(bm25_bytes).hexdigest(),
        )

        manifest = IndexManifest(
            publication_id=pub_id,
            document_id=doc.id,
            document_version=doc.version,
            fragments_sha256=digest,
            mode="bm25",
            bm25=bm25_artifact,
            vectors=None,
            embedding_fingerprint=None,
        )
        return self.store.publish(
            manifest,
            ordered,
            warnings=["Режим BM25: векторный поиск и повторное ранжирование отключены."],
        )


def _resolve_indexer(store: Store, settings: Settings, indexer: Indexer | None = None) -> Indexer:
    if indexer is not None:
        return indexer
    try:
        from navigator.rag.indexing import Indexer as KlimIndexer
        return KlimIndexer(store=store, settings=settings)
    except Exception:
        return FallbackBM25Indexer(store=store)


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

        # Автоматическое восстановление зависших задач при инициализации сервиса
        stale = self.queue.recover_stale_jobs()
        if stale > 0:
            logger.info("Восстановлено %d прерванных фоновых задач", stale)

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
            metadata=MetadataPatch(title=title[:500]),
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
            parsed = parse_file(full_path)

            # Сохраняем адресованные блоки для просмотрщика источников
            self.storage.save_extracted_blocks(doc_id, parsed.blocks)

            # Нарезаем черновики на фрагменты
            drafts = parsed.build()
            fragments = to_fragments(doc_id, version, drafts)

            # Метаданные
            meta = parsed.metadata or extract_metadata(parsed.units, title_hint=parsed.title, filename=filename)
            title = (meta.title or parsed.title or Path(filename).stem)[:500]

            # Формируем обновленный документ
            target_status = "needs_review" if parsed.needs_review else "processing"
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

            if parsed.needs_review:
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

            # Публикуем через индексатор (Климовский или BM25)
            index_res = self.indexer.index_document(doc_id, fragments)

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

        except Exception as exc:
            logger.exception("Ошибка обработки документа %s в задаче %s", doc_id, job_id)
            self.store.execute(
                "UPDATE documents SET status='failed' WHERE id=?",
                (doc_id,),
            )
            self.queue.complete_job(
                job_id,
                status="failed",
                error=f"Ошибка обработки файла: {str(exc)}",
            )
            return True

    def run_all_jobs(self, max_jobs: int = 100) -> int:
        """Синхронно обрабатывает все накопившиеся задачи в очереди."""
        processed = 0
        while processed < max_jobs and self.process_next_job():
            processed += 1
        return processed

    def get_document(self, doc_id: str) -> dict | None:
        row = self.store.document(doc_id)
        if not row:
            return None
        return self._format_doc_record(row)

    def list_documents(self) -> list[dict]:
        rows = self.store.rows("SELECT * FROM documents ORDER BY created DESC")
        return [self._format_doc_record(dict(r)) for r in rows]

    def patch_metadata(self, doc_id: str, patch: MetadataPatch) -> dict:
        row = self.store.document(doc_id)
        if not row or not row.get("payload"):
            raise ValueError(f"Документ {doc_id} не найден")

        doc = Document.model_validate_json(row["payload"])
        current_meta = doc.metadata.model_dump()

        # Накладываем только переданные поля (учитываем model_fields_set)
        patch_data = patch.model_dump(exclude_unset=True)
        current_meta.update(patch_data)

        updated_meta = MetadataPatch.model_validate(current_meta)
        new_title = (updated_meta.title or doc.title)[:500]

        # Если администратор подтвердил review, проверяем возможность перевода в ready
        new_status = doc.status
        if patch.reviewed is True and doc.status == "needs_review":
            # Если документ был needs_review, теперь после подтверждения можно опубликовать
            frags_rows = self.store.rows("SELECT * FROM fragments WHERE document_id=? ORDER BY ordinal", (doc_id,))
            if frags_rows:
                # Уже есть фрагменты
                new_status = "ready"

        doc.title = new_title
        doc.metadata = updated_meta
        doc.status = new_status

        self.store.execute(
            "UPDATE documents SET title=?, metadata=?, payload=?, status=? WHERE id=?",
            (new_title, updated_meta.model_dump_json(), doc.model_dump_json(), new_status, doc_id),
        )
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
        return {
            "id": row["id"],
            "name": row["name"],
            "title": row["title"],
            "status": row["status"],
            "warnings": warnings,
            "metadata": meta,
            "original_path": row.get("path"),
        }
