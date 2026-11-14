from __future__ import annotations

import logging
import os
import asyncio
from contextlib import asynccontextmanager
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from navigator.config import get_settings
from navigator.ui.health import build_health
from navigator.ui.integration import (
    RouterResolution,
    integration_summary,
    load_feature_router,
)

log = logging.getLogger("navigator.main")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)
SOURCE_CSP = (
    "default-src 'none'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def _include_feature(
    app: FastAPI,
    *,
    key: str,
    explicit_router: Any | None,
    allow_synthetic: bool,
    synthetic_builder: Any,
    synthetic_state: Any | None,
) -> tuple[RouterResolution, Any | None]:
    if explicit_router is not None:
        app.include_router(explicit_router)
        return (
            RouterResolution(key=key, mode="real", module="explicit", reason=""),
            synthetic_state,
        )

    resolution = (RouterResolution(key=key, reason="Явно выбран демонстрационный режим.")
                  if allow_synthetic else load_feature_router(key))
    if resolution.router is not None:
        app.include_router(resolution.router)
        return resolution, synthetic_state

    if not allow_synthetic:
        return resolution, synthetic_state

    from navigator.ui.synthetic import SyntheticState

    if synthetic_state is None:
        synthetic_state = SyntheticState(app.state.settings)
    app.include_router(synthetic_builder(synthetic_state))
    original_reason = resolution.reason
    resolution.mode = "synthetic"
    resolution.reason = (
        f"{original_reason} Включена явно обозначенная демонстрационная заглушка."
    ).strip()
    log.warning("Synthetic %s router is enabled: %s", key, resolution.reason)
    return resolution, synthetic_state


def create_app(
    *,
    ask_router: Any | None = None,
    documents_router: Any | None = None,
    synthetic_fallback: bool | None = None,
    document_service: Any | None = None,
) -> FastAPI:
    settings = document_service.settings if document_service is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app):
        stop = asyncio.Event()
        worker = None
        if app.state.integration["documents"]["mode"] == "real" and documents_router is None:
            from navigator.documents.router import document_worker
            from navigator.documents.service import DocumentService
            if document_service is None:
                try:
                    from navigator.rag.router import initialize_rag
                except ModuleNotFoundError as exc:
                    if exc.name != "navigator.rag.router":
                        raise
                    from navigator.db import Store
                    app.state.store = Store(settings.data_dir)
                    app.state.document_service = DocumentService(app.state.store, settings)
                else:
                    initialize_rag(app)
                    app.state.document_service = DocumentService(app.state.store, settings, app.state.indexer)
            else:
                app.state.document_service = document_service
            worker = asyncio.create_task(document_worker(app.state.document_service, stop))
        try:
            yield
        finally:
            stop.set()
            if worker is not None:
                await worker

    app = FastAPI(
        lifespan=lifespan,
        title="Altropos Navigator",
        description="Навигатор по регламентам с проверяемыми источниками.",
        version="0.1.0",
    )
    app.state.settings = settings
    if document_service is not None:
        app.state.store = document_service.store
    app.state.health_cache = None

    # No development-profile implicit fallback. Environment is an explicit opt-in.
    raw_synthetic = os.getenv("UI_SYNTHETIC_FALLBACK", "").strip().lower()
    allow_synthetic = (synthetic_fallback if synthetic_fallback is not None
                       else raw_synthetic in {"1", "true", "yes", "on", "force"})
    if app.state.settings.profile != "development" and allow_synthetic:
        log.warning(
            "Synthetic UI fallback is enabled explicitly for profile %s; "
            "the UI will mark this mode.",
            settings.profile,
        )

    from navigator.ui.synthetic import build_ask_router, build_documents_router

    synthetic_state = None
    ask_resolution, synthetic_state = _include_feature(
        app,
        key="ask",
        explicit_router=ask_router,
        allow_synthetic=allow_synthetic,
        synthetic_builder=build_ask_router,
        synthetic_state=synthetic_state,
    )
    documents_resolution, synthetic_state = _include_feature(
        app,
        key="documents",
        explicit_router=documents_router,
        allow_synthetic=allow_synthetic and ask_resolution.mode != "real",
        synthetic_builder=build_documents_router,
        synthetic_state=synthetic_state,
    )
    app.state.synthetic_state = synthetic_state
    app.state.integration = integration_summary(
        {"ask": ask_resolution, "documents": documents_resolution}
    )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.mount("/review-files", StaticFiles(directory=str(settings.review_dir), check_dir=False), name="review-files")

    REVIEW_CSP = (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )

    @app.get("/review")
    def review_page(request: Request, doc: str = ""):
        has_static = (settings.review_dir / "index.html").is_file()
        if has_static and not doc.strip():
            return RedirectResponse("/review-files/index.html")
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context={
                "selected_doc_id": doc.strip(),
                "has_static_packages": has_static,
            },
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        path = request.url.path
        if path.startswith(("/review-files/", "/review")):
            response.headers.setdefault("Content-Security-Policy", REVIEW_CSP)
        elif path.startswith("/api/sources/"):
            response.headers.setdefault("Content-Security-Policy", SOURCE_CSP)
        elif response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", PAGE_CSP)
        return response

    def page_context(request: Request, page: str) -> dict[str, Any]:
        return {
            "request": request,
            "page": page,
            "integration": app.state.integration,
            "profile": settings.profile,
            "max_upload_mb": settings.max_upload_mb,
            "ask_timeout_seconds": int(settings.request_timeout * 4) + 30,
            "max_question_length": 4000,
        }

    @app.get("/", response_class=HTMLResponse)
    async def ask_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="ask.html",
            context=page_context(request, "ask"),
        )

    @app.get("/documents", response_class=HTMLResponse)
    async def documents_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="documents.html",
            context=page_context(request, "documents"),
        )

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        now = time.monotonic()
        cache = app.state.health_cache
        if cache and now - cache["at"] < 5.0:
            return JSONResponse(cache["data"])
        data = await build_health(settings, app.state.integration)
        app.state.health_cache = {"at": now, "data": data}
        return JSONResponse(data)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("navigator.main:app", host="127.0.0.1", port=8000, reload=False)
