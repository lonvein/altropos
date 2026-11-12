"""Real local-document HTTP API; no URL ingestion or synthetic sources.

Use create_document_router(service), or mount router with app.state.document_service.
Run document_worker once per application lifespan; indexing stays in a worker thread.
"""
from __future__ import annotations

import asyncio
import json
from html import escape

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from navigator.documents.service import DocumentService
from navigator.ingestion.common import ParseError
from navigator.schemas import Fragment, MetadataPatch


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
        except (ParseError, ValueError):
            return _error("invalid_request", "Проверьте формат, имя и размер файла.", 422)
        finally:
            await file.close()

    @api.get("/documents")
    def documents(request: Request):
        return {"documents": [public_document(d) for d in current(request).list_documents()]}

    @api.get("/jobs")
    def jobs(request: Request):
        return {"jobs": [public_job(j) for j in current(request).queue.list_jobs()]}

    @api.get("/jobs/{job_id}")
    def job(request: Request, job_id: str):
        result = current(request).queue.get_job(job_id)
        return public_job(result) if result else _error("not_found", "Задание не найдено.", 404)

    @api.patch("/documents/{document_id}/metadata")
    def metadata(request: Request, document_id: str, patch: MetadataPatch):
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

    @api.get("/sources/{fragment_id}")
    def source(request: Request, fragment_id: str):
        svc = current(request)
        # Read only active ready publication snapshots, never extracted cache or
        # arbitrary file paths. Works for BM25 and hybrid without loading models.
        rows = svc.store.rows("""SELECT d.title,p.fragments FROM documents d
            JOIN publications p ON p.id=d.active_publication WHERE d.status='ready'""")
        for row in rows:
            for data in json.loads(row["fragments"]):
                if data["id"] != fragment_id:
                    continue
                fragment = Fragment.model_validate(data)
                title = escape(row["title"])
                locator = escape(json.dumps(fragment.locator.model_dump(mode="json"), ensure_ascii=False))
                text = escape(fragment.text)
                page = (f'<!doctype html><html lang="ru"><meta charset="utf-8">'
                        f'<title>{title}</title><h1>{title}</h1><p>{locator}</p>'
                        f'<pre style="white-space:pre-wrap">{text}</pre></html>')
                return HTMLResponse(page, headers={
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'; sandbox",
                    "X-Content-Type-Options": "nosniff",
                })
        return _error("not_found", "Опубликованный источник не найден.", 404)

    return api


router = create_document_router()
