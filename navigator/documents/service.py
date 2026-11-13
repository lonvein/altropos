"""Сервис управления жизненным циклом документов, очередью и индексацией."""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path

from navigator.config import Settings, get_settings
from navigator.db import Store
from navigator.documents.queue import JobQueue
from navigator.documents.site_importer import (
    ALLOWED_DOC_EXTENSIONS,
    fetch_mipt_bytes,
    filename_from_url,
    inspect_mipt_page,
    validate_mipt_url,
)
from navigator.documents.storage import DocumentStorage
from navigator.ingestion import parse_file
from navigator.ingestion.common import ParseError, document_ids, sha256_bytes, to_fragments
from navigator.ingestion.metadata_extractor import extract_metadata
from navigator.interfaces import Indexer
from navigator.schemas import Document, MetadataPatch, Relation

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
        http_transport=None,
    ):
        self.store = store
        self.settings = settings or get_settings()
        self.storage = DocumentStorage(
            root=self.store.root,
            max_upload_mb=self.settings.max_upload_mb,
        )
        self.queue = JobQueue(store=self.store)
        self.indexer = _resolve_indexer(self.store, self.settings, indexer)
        self.http_transport = http_transport

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

    def import_from_url(self, url: str, selected_urls: list[str] | None = None) -> dict:
        """Проверяет URL mipt.ru и ставит в очередь предпросмотр или импорт документов."""
        clean_url = validate_mipt_url(url)
        selected = [item.strip() for item in (selected_urls or []) if item and item.strip()]
        if selected:
            job_id = self.queue.enqueue(
                "import_url",
                {"url": clean_url, "selected_urls": selected},
            )
        else:
            job_id = self.queue.enqueue("import_preview", {"url": clean_url})
        return {"job_id": job_id, "status": "queued"}

    def _process_file_payload(self, payload: dict) -> tuple[str, dict]:
        """Парсит сохранённый файл, извлекает метаданные и при готовности публикует в RAG."""
        doc_id = payload["document_id"]
        version = payload.get("version", "v1")
        filename = payload.get("filename", "")
        rel_path = payload["rel_path"]

        full_path = self.storage.get_original_path(rel_path)
        registered = self.store.document(doc_id)
        if registered is None or sha256_bytes(full_path.read_bytes()) != registered["sha"]:
            raise ValueError("Changed original requires a new immutable document version")
        self._require_manual_upload_allowed(registered["sha"])
        if full_path.suffix.lower() == ".pdf":
            from navigator.ingestion.ocr import make_ocr
            parsed = parse_file(
                full_path,
                ocr=make_ocr(
                    tesseract_cmd=self.settings.tesseract_cmd,
                    tessdata_dir=self.settings.tessdata_prefix,
                ),
            )
        else:
            parsed = parse_file(full_path)

        # Сохраняем адресованные блоки для просмотрщика источников
        self.storage.save_extracted_blocks(doc_id, parsed.blocks)

        # Нарезаем черновики на фрагменты
        drafts = parsed.build()
        fragments = to_fragments(doc_id, version, drafts)

        # Метаданные
        meta = parsed.metadata or extract_metadata(parsed.units, title_hint=parsed.title, filename=filename)
        previous_doc: Document | None = None
        if registered.get("payload"):
            previous_doc = Document.model_validate_json(registered["payload"])
            meta = MetadataPatch.model_validate({
                **meta.model_dump(),
                **previous_doc.metadata.model_dump(exclude_none=True),
            })
        title = (meta.title or parsed.title or Path(filename).stem)[:500]
        if title == Path(rel_path).stem:
            # Parsers see an internal storage ID, not the uploaded filename.
            title = Path(filename).stem[:500]
            meta = meta.model_copy(update={"title": title})

        if meta.relations:
            meta = meta.model_copy(update={"relations": self._resolve_relations(meta.relations)})

        from navigator.ingestion.common import filter_user_warnings
        requires_review = parsed.needs_review or not fragments
        target_status = "needs_review" if requires_review else "processing"

        warnings = filter_user_warnings(
            [w[:500] for w in parsed.warnings],
            status="needs_review" if requires_review else "ready",
            needs_review=requires_review,
        )
        source_url = previous_doc.source_url if previous_doc else registered.get("url")
        retrieved_at = previous_doc.retrieved_at if previous_doc else None
        if source_url and not (meta.issued or meta.valid_from):
            dated_warning = "Импортировано с сайта mipt.ru; дата актуальности редакции требует подтверждения."
            if dated_warning not in warnings:
                warnings.append(dated_warning)
        doc = Document(
            id=doc_id,
            version=version,
            name=filename,
            title=title,
            sha256=sha256_bytes(full_path.read_bytes()),
            original_path=rel_path,
            media_type=parsed.media_type,
            status=target_status,
            source_url=source_url,
            retrieved_at=retrieved_at,
            metadata=meta,
            warnings=warnings,
        )

        # Обновляем документ в SQLite
        self.store.execute(
            "UPDATE documents SET title=?, metadata=?, payload=?, status=?, url=? WHERE id=?",
            (title, meta.model_dump_json(), doc.model_dump_json(), target_status, source_url, doc_id),
        )
        if meta.number:
            self._backfill_incoming_relations(doc_id, meta.number)

        if requires_review:
            return (
                "needs_review",
                {
                    "document_id": doc_id,
                    "fragment_count": len(fragments),
                    "warnings": warnings,
                },
            )

        from navigator.documents.snapshots import save_snapshot
        save_snapshot(self.store, doc, fragments, parsed.blocks)
        index_res = self.indexer.index_document(doc_id, fragments)
        self.store.execute("UPDATE documents SET payload=json_set(payload, '$.status', status) WHERE id=?", (doc_id,))
        return (
            "ready",
            {
                "document_id": doc_id,
                "publication_id": index_res.publication_id,
                "mode": index_res.mode,
                "fragment_count": index_res.fragment_count,
                "warnings": index_res.warnings,
            },
        )

    def _run_import_preview_job(self, job_id: str, payload: dict) -> None:
        try:
            preview = inspect_mipt_page(
                payload["url"],
                max_bytes=self.storage.max_bytes,
                timeout=self.settings.request_timeout,
                transport=self.http_transport,
            )
            self.queue.complete_job(job_id, status="ready", result=preview)
        except ValueError as exc:
            self.queue.complete_job(job_id, status="failed", error=str(exc))
        except Exception:
            logger.exception("Ошибка предпросмотра URL в задаче %s", job_id)
            self.queue.complete_job(
                job_id,
                status="failed",
                error="Не удалось загрузить страницу mipt.ru. Подробности в локальном журнале сервера.",
            )

    def _run_import_url_job(self, job_id: str, payload: dict) -> None:
        source_page_url = payload["url"]
        selected_urls = list(payload.get("selected_urls", []))
        created_docs: list[dict] = []
        rejected: list[dict] = []

        try:
            for target_url in selected_urls:
                try:
                    clean_target = validate_mipt_url(target_url)
                    name = filename_from_url(clean_target)
                    if Path(name).suffix.lower() not in ALLOWED_DOC_EXTENSIONS:
                        raise ValueError("Поддерживаются только файлы PDF, DOCX и XLSX.")

                    final_url, content, _content_type = fetch_mipt_bytes(
                        clean_target,
                        max_bytes=self.storage.max_bytes,
                        timeout=self.settings.request_timeout,
                        transport=self.http_transport,
                    )
                    final_name = filename_from_url(final_url, fallback=name)
                    if Path(final_name).suffix.lower() not in ALLOWED_DOC_EXTENSIONS:
                        final_name = name

                    upload_res = self.upload_file(final_name, content)
                    doc_id = upload_res["document_id"]

                    if not upload_res.get("duplicate"):
                        today_iso = date.today().isoformat()
                        self.store.execute(
                            "UPDATE documents SET url=?, payload=json_set(payload, '$.source_url', ?, '$.retrieved_at', ?) WHERE id=?",
                            (final_url, final_url, today_iso, doc_id),
                        )
                        child_job_id = upload_res.get("job_id")
                        child_payload = {
                            "document_id": doc_id,
                            "version": "v1",
                            "filename": final_name,
                            "rel_path": self.store.document(doc_id)["path"],
                        }
                        try:
                            doc_status, doc_result = self._process_file_payload(child_payload)
                            if child_job_id:
                                self.store.execute(
                                    "UPDATE jobs SET status=?, result=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                                    (doc_status, json.dumps(doc_result, ensure_ascii=False), child_job_id),
                                )
                        except Exception:
                            logger.exception("Ошибка обработки импортированного документа %s", doc_id)
                            self.store.execute(
                                "UPDATE documents SET status='failed', payload=json_set(payload, '$.status', 'failed') WHERE id=?",
                                (doc_id,),
                            )
                            if child_job_id:
                                self.store.execute(
                                    "UPDATE jobs SET status='failed', error=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                                    ("Ошибка обработки файла. Подробности в локальном журнале сервера.", child_job_id),
                                )

                    doc_record = self.get_document(doc_id)
                    if doc_record is not None:
                        doc_record.pop("original_path", None)
                        created_docs.append(doc_record)
                except ValueError as exc:
                    rejected.append({"url": target_url, "reason": str(exc)})
                except Exception:
                    logger.exception("Ошибка загрузки выбранного URL %s", target_url)
                    rejected.append({"url": target_url, "reason": "Ошибка загрузки файла с mipt.ru."})

            self.queue.complete_job(
                job_id,
                status="ready",
                result={
                    "url": source_page_url,
                    "selected_urls": selected_urls,
                    "documents": created_docs,
                    "rejected": rejected,
                },
            )
        except Exception:
            logger.exception("Критическая ошибка импорта по URL в задаче %s", job_id)
            self.queue.complete_job(
                job_id,
                status="failed",
                error="Ошибка импорта документов с mipt.ru. Подробности в локальном журнале сервера.",
            )

    def process_next_job(self) -> bool:
        """Выполняет одну задачу из очереди. Возвращает True, если задача была найдена и обработана."""
        job = self.queue.acquire_next_job()
        if not job:
            return False

        job_id = job["id"]
        kind = job["kind"]
        payload = job["payload"]

        if kind == "import_preview":
            self._run_import_preview_job(job_id, payload)
            return True
        if kind == "import_url":
            self._run_import_url_job(job_id, payload)
            return True

        doc_id = payload["document_id"]
        try:
            final_status, result = self._process_file_payload(payload)
            self.queue.complete_job(job_id, status=final_status, result=result)
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

    @staticmethod
    def _normalize_order_number(value: str | None) -> str:
        raw = (value or "").strip()
        raw = re.sub(r"^(?:№|N)\s*", "", raw, flags=re.I)
        return re.sub(r"\s+", "", raw).lower()

    def _resolve_relations(self, relations: list[Relation] | None, *, con=None) -> list[Relation] | None:
        if not relations:
            return relations
        sql = "SELECT id, json_extract(metadata, '$.number') AS num FROM documents"
        if con is not None:
            rows = [dict(r) for r in con.execute(sql).fetchall()]
        else:
            rows = self.store.rows(sql)
        existing_ids = {r["id"] for r in rows}
        by_number: dict[str, str] = {}
        for r in rows:
            norm = self._normalize_order_number(r.get("num"))
            if norm and norm not in by_number:
                by_number[norm] = r["id"]

        resolved: list[Relation] = []
        for rel in relations:
            if rel.target_id in existing_ids:
                resolved.append(rel)
                continue
            matched_id = by_number.get(self._normalize_order_number(rel.target_id))
            if matched_id:
                resolved.append(rel.model_copy(update={"target_id": matched_id}))
            else:
                resolved.append(rel)
        return resolved

    def _backfill_incoming_relations(self, doc_id: str, doc_number: str | None, *, con=None) -> None:
        norm = self._normalize_order_number(doc_number)
        if not norm:
            return

        def _apply(conn) -> None:
            rows = conn.execute(
                "SELECT id, payload FROM documents WHERE id != ? AND payload IS NOT NULL",
                (doc_id,),
            ).fetchall()
            for r in rows:
                other_doc = Document.model_validate_json(r["payload"])
                rels = other_doc.metadata.relations
                if not rels:
                    continue
                changed = False
                new_rels: list[Relation] = []
                for rel in rels:
                    if rel.target_id != doc_id and self._normalize_order_number(rel.target_id) == norm:
                        new_rels.append(rel.model_copy(update={"target_id": doc_id}))
                        changed = True
                    else:
                        new_rels.append(rel)
                if changed:
                    other_meta = other_doc.metadata.model_copy(update={"relations": new_rels})
                    other_doc.metadata = other_meta
                    conn.execute(
                        "UPDATE documents SET metadata=?, payload=? WHERE id=?",
                        (other_meta.model_dump_json(), other_doc.model_dump_json(), r["id"]),
                    )

        if con is not None:
            _apply(con)
        else:
            with self.store.connect() as own_con:
                own_con.execute("BEGIN IMMEDIATE")
                _apply(own_con)

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
            if updated_meta.relations:
                updated_meta = updated_meta.model_copy(
                    update={"relations": self._resolve_relations(updated_meta.relations, con=con)}
                )
            doc.title = (updated_meta.title or doc.title)[:500]
            doc.metadata = updated_meta
            doc.status = row["status"]
            reprocess = patch.reviewed is True and doc.status == "needs_review"
            if reprocess:
                doc.status = "queued"
            con.execute("UPDATE documents SET title=?,metadata=?,payload=?,status=? WHERE id=?",
                        (doc.title, updated_meta.model_dump_json(), doc.model_dump_json(), doc.status, doc_id))
            if updated_meta.number:
                self._backfill_incoming_relations(doc_id, updated_meta.number, con=con)
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
        source_url = row.get("url")
        retrieved_at = None
        media_type = None
        version = None
        if row.get("payload"):
            try:
                payload = json.loads(row["payload"])
                warnings = payload.get("warnings", [])
                source_url = payload.get("source_url") or source_url
                retrieved_at = payload.get("retrieved_at")
                media_type = payload.get("media_type")
                version = payload.get("version")
            except Exception:
                pass
        from navigator.ingestion.common import filter_user_warnings
        warnings = filter_user_warnings(warnings, status=row["status"], needs_review=(row["status"] == "needs_review"))
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
            "source_url": source_url,
            "retrieved_at": retrieved_at,
            "media_type": media_type,
            "version": version,
            "created": row.get("created"),
            "original_path": row.get("path"),
        }
