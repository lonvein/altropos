"""Public request validation, runtime lifetime and real storage wiring."""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from navigator.config import Settings
from navigator.interfaces import ComponentError
from navigator.rag.router import initialize_rag, router
from navigator.schemas import (
    AskResponse, Document, ErrorDetail, Fragment, SearchRequest,
)


def application(tmp_path, pipeline=None):
    app = FastAPI()
    app.state.settings = Settings(_env_file=None, data_dir=tmp_path,
                                  ml_enabled=False, llm_enabled=False)
    if pipeline is not None:
        app.state.answer_pipeline = pipeline
    app.include_router(router)
    return app


def test_public_validation_does_not_echo_input(tmp_path):
    with TestClient(application(tmp_path)) as client:
        for body in ({"question": " "}, {"question": "Как подать заявку?", "as_of": "bad-date"},
                     {"question": "Как подать заявку?", "secret": "private-value"}):
            result = client.post("/api/ask", json=body)
            assert result.status_code == 422
            assert result.json()["error"]["code"] == "invalid_request"
            assert "private-value" not in result.text
        broken = client.post("/api/ask", content="{", headers={"Content-Type": "application/json"})
        assert broken.status_code == 422
        assert broken.json()["error"]["code"] == "invalid_request"


def test_explicit_pipeline_and_date_reach_endpoint(tmp_path):
    class Pipeline:
        async def ask(self, request):
            assert request.as_of == date(2026, 11, 13)
            return AskResponse(status="not_found", mode="bm25", answer_mode="sources_only",
                               warnings=["Нет достаточных оснований."])

    with TestClient(application(tmp_path, Pipeline())) as client:
        response = client.post("/api/ask", json={"question": "Как подать заявку?", "as_of": "2026-11-13"})
        assert response.status_code == 200
        assert response.json()["status"] == "not_found"


def test_runtime_reuses_store_indexer_and_pipeline(tmp_path):
    app = application(tmp_path)
    initialize_rag(app)
    previous = (app.state.store, app.state.indexer, app.state.answer_pipeline)
    initialize_rag(app)
    assert previous == (app.state.store, app.state.indexer, app.state.answer_pipeline)
    assert app.state.llm_client is None
    assert app.state.embeddings_client is None


def test_real_bm25_endpoint_has_snapshot_sources(tmp_path):
    app = application(tmp_path)
    initialize_rag(app)
    store = app.state.store
    document = Document(id="synthetic-ask-doc", version="v1", name="synthetic.docx",
                        title="Искусственный документ API", sha256="a" * 64,
                        original_path="originals/synthetic.docx", media_type="docx")
    store.register_document(document)
    fragment = Fragment(id="synthetic-ask-f1", document_id=document.id, document_version="v1",
                        ordinal=0, text="Искусственный пример: заявление направляется координатору.",
                        locator={"kind": "docx", "block": 0})
    app.state.indexer.index_document(document.id, [fragment])
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "Кому направляется заявление?"})
        assert response.status_code == 200
        answer = AskResponse.model_validate(response.json())
        assert answer.answer_mode in {"sources_only", "extracts"}
        assert answer.status != "answered"
        assert answer.sources[0].title == document.title
        assert answer.sources[0].url == "/api/sources/synthetic-ask-f1"
        assert answer.warnings


@pytest.mark.parametrize("error,status", [("busy", 503), ("context_exceeded", 422)])
def test_component_errors_are_sanitized(tmp_path, error, status):
    class Pipeline:
        async def ask(self, request):
            raise ComponentError(ErrorDetail(code=error, message="private /runtime/path", retryable=True))

    with TestClient(application(tmp_path, Pipeline())) as client:
        response = client.post("/api/ask", json={"question": "Как подать заявку?"})
        assert response.status_code == status
        assert response.json()["error"]["code"] == error
        assert "private" not in response.text


def test_request_deadline_includes_pipeline_waiting(tmp_path):
    class Pipeline:
        async def ask(self, request):
            await asyncio.sleep(1)

    app = application(tmp_path, Pipeline())
    app.state.settings = Settings(_env_file=None, data_dir=tmp_path, request_timeout=0.01)
    with TestClient(app) as client:
        response = client.post("/api/ask", json={"question": "Как подать заявку?"})
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "model_timeout"


def test_search_during_indexing_keeps_both_transports_usable(tmp_path, monkeypatch):
    from navigator.rag.clients import LocalTransport
    entered, release = threading.Event(), threading.Event()
    app = application(tmp_path)
    app.state.settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=True,
                                  reranker_revision="synthetic", request_timeout=5)
    settings = app.state.settings

    def synthetic_post(self, path, payload):
        if path == "/embed":
            if payload["purpose"] == "document" and "NEW" in payload["texts"][0]:
                entered.set()
                assert release.wait(4)
            return {"fingerprint": settings.fingerprint,
                    "vectors": [[1.0] + [0.0] * 1023 for _ in payload["texts"]]}
        assert path == "/rerank"
        return {"model": settings.reranker_model, "revision": settings.reranker_revision,
                "scores": [{"fragment_id": f["id"], "score": 1.0} for f in payload["fragments"]]}

    monkeypatch.setattr(LocalTransport, "_post", synthetic_post)
    initialize_rag(app)

    def register(index, text):
        document = Document(id=f"synthetic-concurrent-{index}", version="v1", name="demo.docx",
                            title="Искусственный пример", sha256=str(index) * 64,
                            original_path=f"originals/{index}.docx", media_type="docx")
        app.state.store.register_document(document)
        fragment = Fragment(id=f"synthetic-concurrent-f{index}", document_id=document.id,
                            document_version="v1", ordinal=0, text=text,
                            locator={"kind": "docx", "block": 0})
        return document, fragment

    old, old_fragment = register(1, "ИСКУССТВЕННЫЙ ПРИМЕР: заявка координатору.")
    app.state.indexer.index_document(old.id, [old_fragment])
    new, new_fragment = register(2, "NEW ИСКУССТВЕННЫЙ ПРИМЕР: другая заявка.")
    with ThreadPoolExecutor(max_workers=1) as pool:
        indexing = pool.submit(app.state.indexer.index_document, new.id, [new_fragment])
        try:
            assert entered.wait(3)
            result = asyncio.run(app.state.answer_pipeline.searcher.search(
                SearchRequest(question="заявка координатору")))
            assert result.mode == "hybrid"
        finally:
            release.set()
        assert indexing.result(timeout=5).mode == "hybrid"
    assert app.state.store.document(new.id)["status"] == "ready"
