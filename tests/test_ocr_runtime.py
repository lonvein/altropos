"""Runtime OCR configuration, explicit retries and read-only review endpoints."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from navigator.config import Settings
from navigator.db import Store
from navigator.documents.service import DocumentService
from navigator.ingestion.common import Unit
from navigator.ingestion.model import Parsed


def test_pdf_worker_uses_explicit_ocr_and_retry_does_not_confirm_metadata(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", ml_enabled=False, llm_enabled=False,
                        tesseract_cmd=tmp_path / "tesseract.exe", tessdata_prefix=tmp_path / "tessdata")
    calls = []
    hook = object()

    def make_ocr(tesseract_cmd, tessdata_dir):
        calls.append((tesseract_cmd, tessdata_dir))
        return hook

    readable = False

    def parse(path, *, ocr):
        assert ocr is hook
        return Parsed(media_type="pdf", needs_review=not readable,
                      units=[Unit("ИСКУССТВЕННЫЙ ПРИМЕР: заявление координатору.", {"kind": "pdf", "page": 1})])

    monkeypatch.setattr("navigator.ingestion.ocr.make_ocr", make_ocr)
    monkeypatch.setattr("navigator.documents.service.parse_file", parse)
    service = DocumentService(Store(settings.data_dir), settings)
    uploaded = service.upload_file("synthetic.pdf", b"%PDF-1.4 synthetic fixture")
    assert service.process_next_job()
    doc_id = uploaded["document_id"]
    before = service.get_document(doc_id)
    assert before["status"] == "needs_review"
    assert service.store.active_publications() == []
    retry = service.retry_document(doc_id)
    assert retry["status"] == "queued" and retry["job_id"] != uploaded["job_id"]
    assert service.get_document(doc_id)["metadata"]["reviewed"] == before["metadata"]["reviewed"]
    with pytest.raises(ValueError):
        service.retry_document(doc_id)
    readable = True
    assert service.process_next_job()
    assert service.get_document(doc_id)["status"] == "ready"
    assert calls == [(settings.tesseract_cmd, settings.tessdata_prefix)] * 2
    with pytest.raises(ValueError):
        service.retry_document(doc_id)


@pytest.mark.asyncio
async def test_health_distinguishes_ocr_from_model_availability(tmp_path, monkeypatch):
    from navigator.ui.health import build_health
    async def probe(url):
        return {"status": "ok", "http_status": 200}
    monkeypatch.setattr("navigator.ui.health._probe", probe)
    settings = Settings(_env_file=None, profile="3060ti", llm_enabled=True, ml_enabled=True,
                        llm_revision="synthetic", reranker_revision="synthetic")
    monkeypatch.setattr("navigator.ingestion.ocr.ocr_status",
                        lambda **kwargs: {"available": False, "reason": "Нет rus"})
    health = await build_health(settings, {"data_mode": "real"})
    assert health["status"] == "limited"
    assert health["ml"]["status"] == health["llm"]["status"] == "ok"
    assert not health["ocr"]["available"] and any("OCR" in r for r in health["reasons"])
    monkeypatch.setattr("navigator.ingestion.ocr.ocr_status",
                        lambda **kwargs: {"available": True, "tessdata": "/private/path"})
    health = await build_health(settings, {"data_mode": "real"})
    assert health["status"] == "ok" and "tessdata" not in health["ocr"]


def test_review_pages_are_read_only_and_sandboxed(tmp_path, monkeypatch):
    from navigator.main import create_app
    review = tmp_path / "review"
    review.mkdir()
    (review / "index.html").write_text("<h1>Pending synthetic review</h1>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    settings = Settings(_env_file=None, profile="development", data_dir=tmp_path / "db",
                        review_dir=review, ml_enabled=False, llm_enabled=False)
    monkeypatch.setattr("navigator.main.get_settings", lambda: settings)
    with TestClient(create_app(synthetic_fallback=True)) as client:
        response = client.get("/review")
        assert response.status_code == 200 and "Pending synthetic" in response.text
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert client.post("/review", json={"approved": True}).status_code in {404, 405}
        assert client.get("/review-files/%2e%2e/secret.txt").status_code == 404


@pytest.mark.parametrize("flag", [None, "retry", "reocr", "reprocess"])
def test_initial_source_cannot_bypass_approval_by_upload_or_worker(tmp_path, flag):
    import json
    from navigator.corpus import create_manifest
    from navigator.documents.service import InitialCorpusReviewRequired
    from navigator.ingestion.common import sha256_bytes

    source = tmp_path / "source"
    source.mkdir()
    data = b"<p>SYNTHETIC initial source.</p>"
    (source / "initial.html").write_bytes(data)
    manual = b"<p>SYNTHETIC held-out source.</p>"
    (source / "manual.html").write_bytes(manual)
    manifest = tmp_path / "corpus.json"
    create_manifest(source, ["manual.html"], manifest)
    settings = Settings(_env_file=None, data_dir=tmp_path / "db", corpus_manifest=manifest,
                        ml_enabled=False, llm_enabled=False)
    service = DocumentService(Store(settings.data_dir), settings)
    with pytest.raises(InitialCorpusReviewRequired):
        service.upload_file("renamed.html", data)
    assert service.store.rows("SELECT id FROM documents") == []
    uploaded = service.upload_file("manual.html", manual)
    assert uploaded["status"] == "queued"
    assert service.process_next_job()
    assert service.get_document(uploaded["document_id"])["status"] == "ready"
    # A queued source from an older server must also pass the gate.
    service.settings = settings.model_copy(update={"corpus_manifest": None})
    queued = service.upload_file("initial.html", data)
    service.settings = settings
    if flag:
        queued_payload = service.queue.get_job(queued["job_id"])["payload"]
        queued_payload[flag] = True
        service.store.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(queued_payload), queued["job_id"]))
    assert service.process_next_job()
    assert service.get_document(queued["document_id"])["status"] == "failed"
    assert len(service.store.active_publications()) == 1
    manifest.write_text("broken", encoding="utf-8")
    with pytest.raises(InitialCorpusReviewRequired):
        service._require_manual_upload_allowed(sha256_bytes(b"new source"))
