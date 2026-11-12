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


router = APIRouter(route_class=AskRoute)


@router.post("/api/ask", response_model=AskResponse, responses={422: {"model": ErrorResponse}})
async def ask(payload: AskRequest, request: Request):
    try:
        initialize_rag(request.app)
        # One request-wide deadline includes search, lock waiting and all model
        # calls. The UI allows this budget plus a small transport margin.
        async with asyncio.timeout(request.app.state.settings.request_timeout * 4):
            return await request.app.state.answer_pipeline.ask(payload)
    except TimeoutError:
        return _error(504, "model_timeout", "Истекло время обработки вопроса. Повторите запрос.", True)
    except ComponentError as exc:
        statuses = {"invalid_request": 422, "context_exceeded": 422,
                    "model_timeout": 504, "model_unavailable": 503, "busy": 503,
                    "index_mismatch": 409, "not_ready": 409, "not_found": 404,
                    "invalid_model_output": 502}
        return _error(statuses.get(exc.detail.code, 500), exc.detail.code,
                      "Не удалось обработать запрос. Проверьте состояние компонентов.",
                      exc.detail.retryable)
    except Exception:
        log.exception("Ask pipeline failed")
        return _error(500, "internal_error", "Не удалось обработать запрос.")
