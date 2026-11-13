"""Real ask endpoint and application-scoped RAG dependencies.

Importing this module does not open storage or contact a model. The application
lifespan initializes the shared runtime; lazy initialization also supports ASGI
clients which do not drive lifespan events.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from navigator.config import get_settings
from navigator.db import Store
from navigator.interfaces import ComponentError
from navigator.schemas import AskRequest, AskResponse, ErrorDetail, ErrorResponse

log = logging.getLogger(__name__)


def initialize_rag(app) -> None:
    """Create exactly one client set and pipeline per application instance."""
    if getattr(app.state, "answer_pipeline", None) is not None:
        return
    from navigator.rag.clients import EmbeddingsClient, LLMClient, RerankerClient
    from navigator.rag.indexing import Indexer
    from navigator.rag.pipeline import AnswerPipeline
    from navigator.rag.retrieval import Searcher

    settings = getattr(app.state, "settings", None) or get_settings()
    store = getattr(app.state, "store", None) or Store(settings.data_dir)
    embeddings = EmbeddingsClient(settings) if settings.ml_enabled else None
    # The synchronous indexer uses its own event loop in the document worker.
    # Its transport must not share in-flight asyncio Tasks with request handlers.
    index_embeddings = EmbeddingsClient(settings) if settings.ml_enabled else None
    reranker = RerankerClient(settings) if settings.ml_enabled else None
    llm = LLMClient(settings) if settings.llm_enabled else None
    app.state.settings = settings
    app.state.store = store
    app.state.embeddings_client = embeddings
    app.state.reranker_client = reranker
    app.state.llm_client = llm
    app.state.indexer = Indexer(store, settings, index_embeddings)
    app.state.answer_pipeline = AnswerPipeline(
        store=store, settings=settings,
        searcher=Searcher(store, settings, embeddings, reranker), llm=llm,
    )


def _error(status: int, code: str, message: str, retryable: bool = False):
    body = ErrorResponse(error=ErrorDetail(code=code, message=message, retryable=retryable))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


class AskRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return _error(422, "invalid_request", "Проверьте вопрос и дату применимости.")

        return validated


import time
import uuid

router = APIRouter(route_class=AskRoute)

_REQUEST_ID_RE = None


def _valid_request_id(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if 1 <= len(cleaned) <= 64 and all(ch.isalnum() or ch in "-_." for ch in cleaned):
        return cleaned
    return None


class _AskJobManager:
    """Keeps /api/ask execution running on the server across client navigations."""

    def __init__(self, max_jobs: int = 50) -> None:
        self._jobs: dict[str, dict] = {}
        self._in_flight: dict[tuple[str, str], str] = {}
        self._max_jobs = max_jobs

    def _prune(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        completed = [
            (job_id, job["updated_at"])
            for job_id, job in self._jobs.items()
            if job["status"] != "running"
        ]
        completed.sort(key=lambda item: item[1])
        while len(self._jobs) > self._max_jobs and completed:
            old_id, _ = completed.pop(0)
            self._jobs.pop(old_id, None)

    def get(self, request_id: str) -> dict | None:
        return self._jobs.get(request_id)

    def active(self) -> dict | None:
        running = [job for job in self._jobs.values() if job["status"] == "running"]
        if not running:
            return None
        running.sort(key=lambda job: job["updated_at"], reverse=True)
        return running[0]

    def submit(self, app, payload: AskRequest, supplied_id: str | None) -> dict:
        if supplied_id and supplied_id in self._jobs:
            return self._jobs[supplied_id]
        dedup_key = (payload.question.strip(), payload.as_of.isoformat() if payload.as_of else "")
        existing_id = self._in_flight.get(dedup_key)
        if existing_id and existing_id in self._jobs and self._jobs[existing_id]["status"] == "running":
            return self._jobs[existing_id]

        request_id = supplied_id or uuid.uuid4().hex
        now = time.time()
        job: dict = {
            "request_id": request_id,
            "dedup_key": dedup_key,
            "question": payload.question,
            "as_of": dedup_key[1] or None,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "response": None,
            "error": None,
            "http_status": 200,
            "task": None,
        }

        async def _execute() -> None:
            statuses = {
                "invalid_request": 422,
                "context_exceeded": 422,
                "model_timeout": 504,
                "model_unavailable": 503,
                "busy": 503,
                "index_mismatch": 409,
                "not_ready": 409,
                "not_found": 404,
                "invalid_model_output": 502,
            }
            try:
                initialize_rag(app)
                timeout_budget = max(0.001, float(app.state.settings.request_timeout) * 4)
                async with asyncio.timeout(timeout_budget):
                    result = await app.state.answer_pipeline.ask(payload)
                job["result"] = result
                job["response"] = result.model_dump(mode="json")
                job["status"] = "completed"
                job["http_status"] = 200
            except TimeoutError:
                job["status"] = "error"
                job["http_status"] = 504
                job["error"] = {
                    "code": "model_timeout",
                    "message": "Истекло время обработки вопроса. Повторите запрос.",
                    "retryable": True,
                }
            except ComponentError as exc:
                job["status"] = "error"
                job["http_status"] = statuses.get(exc.detail.code, 500)
                job["error"] = {
                    "code": exc.detail.code,
                    "message": "Не удалось обработать запрос. Проверьте состояние компонентов.",
                    "retryable": bool(exc.detail.retryable),
                }
            except Exception:
                log.exception("Ask pipeline failed")
                job["status"] = "error"
                job["http_status"] = 500
                job["error"] = {
                    "code": "internal_error",
                    "message": "Не удалось обработать запрос.",
                    "retryable": False,
                }
            finally:
                job["updated_at"] = time.time()
                if self._in_flight.get(dedup_key) == request_id:
                    self._in_flight.pop(dedup_key, None)

        job["task"] = asyncio.create_task(_execute())
        self._jobs[request_id] = job
        self._in_flight[dedup_key] = request_id
        self._prune()
        return job


def _ask_jobs(app) -> _AskJobManager:
    manager = getattr(app.state, "ask_jobs", None)
    if manager is None:
        manager = _AskJobManager()
        app.state.ask_jobs = manager
    return manager


def _serialize_job(job: dict) -> dict:
    return {
        "request_id": job["request_id"],
        "status": job["status"],
        "question": job["question"],
        "as_of": job["as_of"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "http_status": job["http_status"],
        "result": job["response"] if job["status"] == "completed" else None,
        "error": job["error"] if job["status"] == "error" else None,
    }


@router.get("/api/ask/status/{request_id}")
async def ask_status(request_id: str, request: Request):
    job = _ask_jobs(request.app).get(request_id)
    if job is None:
        return _error(404, "not_found", "Запрос не найден.")
    return _serialize_job(job)


@router.get("/api/ask/active")
async def ask_active(request: Request):
    job = _ask_jobs(request.app).active()
    return {"active": _serialize_job(job) if job is not None else None}


@router.post("/api/ask", response_model=AskResponse, responses={422: {"model": ErrorResponse}})
async def ask(payload: AskRequest, request: Request):
    manager = _ask_jobs(request.app)
    supplied_id = _valid_request_id(request.headers.get("X-Request-Id"))
    job = manager.submit(request.app, payload, supplied_id)
    if job["task"] is not None:
        await asyncio.shield(job["task"])
    if job["status"] == "completed":
        from fastapi.responses import JSONResponse
        return JSONResponse(content=job["response"], headers={"X-Request-Id": job["request_id"]})
    err = job["error"] or {"code": "internal_error", "message": "Не удалось обработать запрос.", "retryable": False}
    err_response = _error(job["http_status"], err["code"], err["message"], err["retryable"])
    err_response.headers["X-Request-Id"] = job["request_id"]
    return err_response
