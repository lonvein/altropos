"""Real document HTTP API: uploads, restricted mipt.ru import, metadata, and safe source viewer.

Use create_document_router(service), or mount router with app.state.document_service.
Run document_worker once per application lifespan; indexing stays in a worker thread.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import Field

from navigator.auth import admin_denied, is_admin_request
from navigator.documents.highlighting import (
    get_original_info,
    render_original_pdf_page,
    resolve_fragment_highlight,
)
from navigator.documents.presentation import SOURCE_HEADERS, render_publication
from navigator.documents.service import DocumentService, InitialCorpusReviewRequired
from navigator.documents.snapshots import load_snapshot
from navigator.ingestion.common import ParseError
from navigator.schemas import Document, Fragment, ImportRequest, MetadataPatch, Schema


class ReviewSubmitRequest(Schema):
    action: str = "review"
    decision: str = "approved"
    reviewer: str = Field(default="Оператор", max_length=200)
    comment: str = Field(default="", max_length=2000)
    blocks: list[dict] | None = None
    title: str | None = Field(default=None, max_length=500)


class ReocrRequest(Schema):
    reviewer: str = Field(default="Оператор", max_length=200)
    comment: str = Field(default="", max_length=2000)


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "retryable": False}}, status_code=status)


async def document_worker(service: DocumentService, stop: asyncio.Event) -> None:
    """Single loop. Shutdown waits for the current sync publication to finish."""
    while not stop.is_set():
        processed = await asyncio.to_thread(service.process_next_job)
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                pass


def create_document_router(service: DocumentService | None = None) -> APIRouter:
    api = APIRouter(prefix="/api", tags=["documents"])

    def current(request: Request) -> DocumentService:
        return service if service is not None else request.app.state.document_service

    def public_document(doc):
        return {key: value for key, value in doc.items() if key != "original_path"}

    def public_job(job):
        return {key: value for key, value in job.items() if key != "payload"}

    @api.post("/documents")
    async def upload(request: Request, file: UploadFile = File(...)):
        svc = current(request)
        try:
            # Bound memory even when Content-Length is absent or untrusted.
            content = await file.read(svc.storage.max_bytes + 1)
            return await asyncio.to_thread(svc.upload_file, file.filename or "", content)
        except InitialCorpusReviewRequired:
            return _error("not_ready", "Стартовый документ добавляется после одобрения распознанного пакета.", 409)
        except (ParseError, ValueError):
            return _error("invalid_request", "Проверьте формат, имя и размер файла.", 422)
        finally:
            await file.close()

    @api.get("/documents")
    def documents(request: Request):
        return {"documents": [public_document(d) for d in current(request).list_documents()]}

    @api.post("/documents/{document_id}/retry")
    def retry(request: Request, document_id: str):
        svc = current(request)
        if svc.get_document(document_id) is None:
            return _error("not_found", "Документ не найден.", 404)
        try:
            return svc.retry_document(document_id)
        except ValueError:
            return _error("not_ready", "Документ уже обрабатывается или опубликован.", 409)

    @api.get("/jobs")
    def jobs(request: Request):
        return {"jobs": [public_job(j) for j in current(request).queue.list_jobs()]}

    @api.get("/jobs/{job_id}")
    def job(request: Request, job_id: str):
        result = current(request).queue.get_job(job_id)
        return public_job(result) if result else _error("not_found", "Задание не найдено.", 404)

    @api.patch("/documents/{document_id}/metadata")
    def metadata(request: Request, document_id: str, patch: MetadataPatch):
        # ``reviewed`` approves a doubtful extraction (a needs_review document is published once it
        # is set), so it is part of OCR approval and is accepted from the administrator only.
        if "reviewed" in patch.model_fields_set and not is_admin_request(request):
            return admin_denied(request)
        svc = current(request)
        if svc.get_document(document_id) is None:
            return _error("not_found", "Документ не найден.", 404)
        try:
            return public_document(svc.patch_metadata(document_id, patch))
        except ValueError:
            return _error("invalid_request", "Некорректные метаданные.", 422)

    @api.get("/documents/{document_id}/original")
    def original(request: Request, document_id: str):
        svc = current(request)
        row = svc.store.document(document_id)
        if row is None:
            return _error("not_found", "Документ не найден.", 404)
        try:
            path = svc.storage.get_original_path(row["path"])
        except (ValueError, OSError):
            return _error("not_found", "Оригинал недоступен.", 404)
        # Always download untrusted originals, including HTML.
        return FileResponse(path, media_type="application/octet-stream", filename=row["name"],
                            headers={"X-Content-Type-Options": "nosniff"})

    @api.post("/import-url")
    def import_url(request: Request, body: ImportRequest):
        svc = current(request)
        try:
            return svc.import_from_url(body.url, body.selected_urls)
        except ValueError as exc:
            return _error("invalid_request", str(exc) or "Недопустимый URL для импорта.", 422)

    @api.get("/documents/{document_id}/view")
    def document_view(request: Request, document_id: str):
        rows = current(request).store.rows("""SELECT d.id,d.title,d.sha,d.payload,p.manifest,p.fragments FROM documents d
            JOIN publications p ON p.id=d.active_publication
            WHERE d.status='ready' AND d.id=?""", (document_id,))
        if not rows:
            return _error("not_found", "Опубликованный документ не найден.", 404)
        row = rows[0]
        fragments = [Fragment.model_validate(data) for data in json.loads(row["fragments"])]
        try:
            blocks = load_snapshot(current(request), row, fragments)
        except (ValueError, OSError, KeyError, TypeError):
            return _error("not_ready", "Полный подтверждённый снимок недоступен. Откройте оригинал.", 409)
        return HTMLResponse(render_publication(row["title"], row["id"], fragments, blocks=blocks), headers=SOURCE_HEADERS)

    @api.get("/sources/{fragment_id}")
    def source(request: Request, fragment_id: str):
        # Both routes use only active ready snapshots; never a mutable extraction cache.
        rows = current(request).store.rows("""SELECT d.id,d.title,d.sha,d.payload,p.manifest,p.fragments FROM documents d
            JOIN publications p ON p.id=d.active_publication WHERE d.status='ready'""")
        for row in rows:
            data = json.loads(row["fragments"])
            if not any(item["id"] == fragment_id for item in data):
                continue
            fragments = [Fragment.model_validate(item) for item in data]
            try:
                blocks = load_snapshot(current(request), row, fragments)
            except (ValueError, OSError, KeyError, TypeError):
                return _error("not_ready", "Полный подтверждённый снимок недоступен. Откройте оригинал.", 409)
            return HTMLResponse(render_publication(row["title"], row["id"], fragments, fragment_id, blocks=blocks),
                                headers=SOURCE_HEADERS)
        return _error("not_found", "Опубликованный источник не найден.", 404)

    @api.get("/documents/{document_id}/review")
    def get_review(request: Request, document_id: str):
        if (denied := admin_denied(request)) is not None:
            return denied
        svc = current(request)
        try:
            return svc.get_review_detail(document_id)
        except ValueError as exc:
            return _error("not_found", str(exc) or "Документ не найден.", 404)

    @api.post("/documents/{document_id}/review")
    def post_review(request: Request, document_id: str, body: ReviewSubmitRequest):
        if (denied := admin_denied(request)) is not None:
            return denied
        svc = current(request)
        if svc.get_document(document_id) is None:
            return _error("not_found", "Документ не найден.", 404)
        try:
            if body.action == "reopen":
                return svc.reopen_review(document_id, reviewer=body.reviewer, comment=body.comment)
            return svc.submit_review(
                document_id,
                decision=body.decision,
                reviewer=body.reviewer,
                comment=body.comment,
                corrected_blocks=body.blocks,
                title=body.title,
            )
        except ValueError as exc:
            return _error("invalid_request", str(exc) or "Не удалось сохранить результаты проверки.", 422)

    @api.post("/documents/{document_id}/reocr")
    async def post_reocr(request: Request, document_id: str, body: ReocrRequest | None = None):
        if (denied := admin_denied(request)) is not None:
            return denied
        svc = current(request)
        if svc.get_document(document_id) is None:
            return _error("not_found", "Документ не найден.", 404)
        req_body = body or ReocrRequest()
        try:
            return await asyncio.to_thread(
                svc.reocr_document,
                document_id,
                reviewer=req_body.reviewer,
                comment=req_body.comment,
            )
        except ValueError as exc:
            return _error("not_ready", str(exc) or "Не удалось запустить повторный OCR.", 409)

    @api.get("/documents/{document_id}/original-info")
    def original_info(request: Request, document_id: str):
        svc = current(request)
        row = svc.store.document(document_id)
        if row is None:
            return _error("not_found", "Документ не найден.", 404)
        doc_rec = svc.get_document(document_id) or {}
        try:
            path = svc.storage.get_original_path(row["path"])
        except (ValueError, OSError, FileNotFoundError):
            return _error("not_found", "Оригинал недоступен.", 404)
        media_type = doc_rec.get("media_type") or "pdf"
        info = get_original_info(path, media_type)
        return {
            "document_id": document_id,
            "title": row["title"],
            "name": row["name"],
            "download_url": f"/api/documents/{document_id}/original",
            **info,
        }

    @api.get("/documents/{document_id}/original-page/{page_number}")
    def original_page(request: Request, document_id: str, page_number: int, scale: float = 1.75):
        svc = current(request)
        row = svc.store.document(document_id)
        if row is None:
            return _error("not_found", "Документ не найден.", 404)
        doc_rec = svc.get_document(document_id) or {}
        media_type = doc_rec.get("media_type") or "pdf"
        if media_type not in {"pdf", "docx"}:
            return _error("not_found", "Постраничное изображение доступно только для PDF и DOCX.", 404)
        try:
            path = svc.storage.get_original_path(row["path"])
            clamped_scale = max(0.5, min(3.5, float(scale)))
            if media_type == "docx":
                from navigator.documents.highlighting import render_original_docx_page
                png_bytes = render_original_docx_page(path, page_number, scale=clamped_scale)
            else:
                png_bytes = render_original_pdf_page(path, page_number, scale=clamped_scale)
        except ValueError as exc:
            return _error("not_found", str(exc) or "Страница не найдена.", 404)
        except (OSError, FileNotFoundError):
            return _error("not_found", "Оригинал недоступен.", 404)
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @api.get("/sources/{fragment_id}/highlight")
    def source_highlight(request: Request, fragment_id: str, quote: str | None = None):
        svc = current(request)
        rows = svc.store.rows(
            """SELECT d.id,d.title,d.sha,d.path,d.payload,p.manifest,p.fragments FROM documents d
            JOIN publications p ON p.id=d.active_publication WHERE d.status='ready'"""
        )
        for row in rows:
            data = json.loads(row["fragments"])
            if not any(item["id"] == fragment_id for item in data):
                continue
            fragments = [Fragment.model_validate(item) for item in data]
            target = next(f for f in fragments if f.id == fragment_id)
            try:
                blocks = load_snapshot(svc, row, fragments)
            except (ValueError, OSError, KeyError, TypeError):
                return _error("not_ready", "Полный подтверждённый снимок недоступен.", 409)
            try:
                orig_path = svc.storage.get_original_path(row["path"])
            except (ValueError, OSError, FileNotFoundError):
                orig_path = None
            document = Document.model_validate_json(row["payload"])
            result = resolve_fragment_highlight(orig_path, document.media_type, target, blocks, quote=quote)
            result["title"] = row["title"]
            result["download_url"] = f"/api/documents/{row['id']}/original"
            return result
        return _error("not_found", "Опубликованный источник не найден.", 404)

    return api


router = create_document_router()
