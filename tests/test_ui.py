from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter
from fastapi.testclient import TestClient

from navigator.main import create_app


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        last = response.json()
        if last["status"] in {"ready", "needs_review", "error", "failed"}:
            return last
        time.sleep(0.1)
    raise AssertionError(f"Job did not finish: {last}")


@pytest.fixture()
def client() -> TestClient:
    app = create_app(synthetic_fallback=True)
    with TestClient(app) as test_client:
        yield test_client


def test_pages_health_and_security_headers(client: TestClient) -> None:
    ask_page = client.get("/")
    assert ask_page.status_code == 200
    assert "Задать вопрос" in ask_page.text
    assert "script-src 'self'" in ask_page.headers["content-security-policy"]
    assert "Демонстрационный режим" in ask_page.text

    documents_page = client.get("/documents")
    assert documents_page.status_code == 200
    assert "Загрузить документ" in documents_page.text
    assert "Требует проверки" in documents_page.text

    health = client.get("/health")
    assert health.status_code == 200
    payload = health.json()
    assert payload["profile"] == "development"
    assert payload["application"]["data_mode"] == "synthetic"
    assert payload["status"] == "limited"
    assert any("заглушк" in reason for reason in payload["reasons"])

    for path in (
        "/static/css/app.css",
        "/static/css/source.css",
        "/static/js/common.js",
        "/static/js/ask.js",
        "/static/js/documents.js",
    ):
        assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Как направить учебную заявку?", "answered"),
        ("Нужен частичный ответ", "partial"),
        ("Здесь нет ответа", "not_found"),
        ("Нужно уточнение", "ambiguous"),
        ("Покажи конфликт редакций", "conflict"),
        ("Сервис недоступен", "unavailable"),
    ],
)
def test_ask_statuses(client: TestClient, question: str, expected: str) -> None:
    response = client.post("/api/ask", json={"question": question, "as_of": "2026-11-12"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == expected
    assert payload["warnings"]
    assert any("ДЕМО-ЗАГЛУШКА" in warning for warning in payload["warnings"])

    if payload["sources"]:
        source = client.get(payload["sources"][0]["url"])
        assert source.status_code == 200
        assert source.headers["content-type"].startswith("text/html")
        assert "ДЕМО-ПРЕДСТАВЛЕНИЕ" in source.text


def test_ask_validation(client: TestClient) -> None:
    empty = client.post("/api/ask", json={"question": "  "})
    assert empty.status_code == 422
    assert "вопрос" in empty.json()["detail"].lower()

    bad_date = client.post("/api/ask", json={"question": "Есть ли приказ?", "as_of": "12.11.2026"})
    assert bad_date.status_code == 422


def test_upload_wait_ask_open_source(client: TestClient) -> None:
    upload = client.post(
        "/api/documents",
        files={"file": ("demo-regulation.pdf", b"%PDF-1.4\n% synthetic demo\n", "application/pdf")},
    )
    assert upload.status_code == 200
    started = upload.json()
    assert started["demo"] is True
    assert started["job_id"]

    finished = wait_for_job(client, started["job_id"])
    assert finished["status"] == "ready"
    assert finished["result"]["document_id"] == started["document_id"]

    documents = client.get("/api/documents").json()["documents"]
    document = next(item for item in documents if item["id"] == started["document_id"])
    assert document["status"] == "ready"
    assert document["fragment_id"]

    answer = client.post("/api/ask", json={"question": "Как направить заявку?"}).json()
    assert answer["sources"][0]["document_id"] == started["document_id"]
    source_url = answer["sources"][0]["url"]
    source = client.get(source_url)
    assert source.status_code == 200
    assert "ДЕМО-ФРАГМЕНТ" in source.text

    original = client.get(f"/api/documents/{started['document_id']}/original")
    assert original.status_code == 200
    assert original.headers["content-type"].startswith("application/pdf")
    assert original.content.startswith(b"%PDF")


def test_upload_statuses_and_duplicate(client: TestClient) -> None:
    ready = client.post(
        "/api/documents",
        files={"file": ("ready-demo.pdf", b"%PDF-1.4\n% ready demo\n", "application/pdf")},
    ).json()
    first = client.get(f"/api/jobs/{ready['job_id']}").json()
    assert first["status"] == "queued"
    time.sleep(0.2)
    second = client.get(f"/api/jobs/{ready['job_id']}").json()
    assert second["status"] == "processing"
    finished = wait_for_job(client, ready["job_id"])
    assert finished["status"] == "ready"

    review = client.post(
        "/api/documents",
        files={"file": ("review-me.pdf", b"%PDF-1.4\n% review demo\n", "application/pdf")},
    ).json()
    review_finished = wait_for_job(client, review["job_id"])
    assert review_finished["status"] == "needs_review"
    listed = {item["id"]: item for item in client.get("/api/documents").json()["documents"]}
    assert listed[review["document_id"]]["status"] == "needs_review"

    failed = client.post(
        "/api/documents",
        files={"file": ("error-demo.pdf", b"%PDF-1.4\n% error demo\n", "application/pdf")},
    ).json()
    failed_finished = wait_for_job(client, failed["job_id"])
    assert failed_finished["status"] == "error"
    assert failed_finished["error"]

    duplicate = client.post(
        "/api/documents",
        files={"file": ("ready-demo.pdf", b"%PDF-1.4\n% ready demo\n", "application/pdf")},
    ).json()
    assert duplicate["duplicate"] is True
    assert duplicate["document_id"] == ready["document_id"]
    assert len(client.get("/api/documents").json()["documents"]) >= 4


def test_source_reload_keeps_fragment(client: TestClient) -> None:
    answer = client.post("/api/ask", json={"question": "Как направить заявку?"}).json()
    source_url = answer["sources"][0]["url"]
    first = client.get(source_url)
    second = client.get(source_url)
    assert first.status_code == second.status_code == 200
    assert "ДЕМО-ПРЕДСТАВЛЕНИЕ" in first.text
    assert first.text == second.text


def test_metadata_patch_and_manual_relations(client: TestClient) -> None:
    upload = client.post(
        "/api/documents",
        files={"file": ("review-me.docx", b"PK\x03\x04 synthetic", "application/octet-stream")},
    ).json()
    wait_for_job(client, upload["job_id"])
    document_id = upload["document_id"]

    response = client.patch(
        f"/api/documents/{document_id}/metadata",
        json={
            "number": "ДЕМО-42",
            "issued": "2026-01-10",
            "valid_from": "2026-02-01",
            "valid_to": "2026-12-31",
            "reviewed": True,
            "relations": [
                {
                    "target_id": "demo-doc-regulation",
                    "kind": "amends",
                    "target_section": "2",
                    "confirmed": False,
                }
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["metadata"]["number"] == "ДЕМО-42"
    assert payload["metadata"]["relations"][0]["confirmed"] is False
    # The synthetic backend deliberately does not fake publication from reviewed=true.
    assert payload["status"] == "needs_review"

    bad_dates = client.patch(
        f"/api/documents/{document_id}/metadata",
        json={"valid_from": "2026-12-31", "valid_to": "2026-01-01"},
    )
    assert bad_dates.status_code == 422


def test_import_preview_selection_and_domain_guard(client: TestClient) -> None:
    preview = client.post(
        "/api/import-url",
        json={"url": "https://mipt.ru/demo-page", "selected_urls": []},
    )
    assert preview.status_code == 200
    preview_job = wait_for_job(client, preview.json()["job_id"])
    assert preview_job["status"] == "ready"
    links = preview_job["result"]["links"]
    assert len(links) == 3
    assert all(link["allowed"] for link in links)
    assert preview_job["result"]["rejected"]

    imported = client.post(
        "/api/import-url",
        json={"url": "https://mipt.ru/demo-page", "selected_urls": [links[0]["url"]]},
    )
    assert imported.status_code == 200
    import_job = wait_for_job(client, imported.json()["job_id"])
    assert import_job["status"] == "ready"
    assert len(import_job["result"]["documents"]) == 1

    forbidden = client.post(
        "/api/import-url",
        json={"url": "https://example.org/document.pdf", "selected_urls": []},
    )
    assert forbidden.status_code == 400
    assert "mipt.ru" in forbidden.json()["detail"]


def test_source_escapes_metadata_title(client: TestClient) -> None:
    upload = client.post(
        "/api/documents",
        files={"file": ("demo.pdf", b"%PDF-1.4\n% synthetic demo\n", "application/pdf")},
    ).json()
    wait_for_job(client, upload["job_id"])
    document_id = upload["document_id"]
    patched = client.patch(
        f"/api/documents/{document_id}/metadata",
        json={"title": "<script>alert(1)</script>"},
    )
    assert patched.status_code == 200
    fragment_id = patched.json()["fragment_id"]
    source = client.get(f"/api/sources/{fragment_id}")
    assert source.status_code == 200
    assert "<script>alert(1)</script>" not in source.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in source.text


def test_production_without_routers_does_not_mount_stubs() -> None:
    app = create_app(synthetic_fallback=False)
    with TestClient(app) as client:
        assert app.state.integration["data_mode"] == "unavailable"
        assert client.get("/").status_code == 200
        assert client.get("/documents").status_code == 200
        assert client.get("/api/documents").status_code == 404
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert any("недоступен" in reason for reason in health["reasons"])


def test_explicit_real_routers_are_used() -> None:
    ask_router = APIRouter()

    @ask_router.post("/api/ask")
    async def fake_ask() -> dict:
        return {"status": "not_found", "mode": "bm25", "answer_mode": "sources_only"}

    documents_router = APIRouter()

    @documents_router.get("/api/documents")
    async def fake_documents() -> dict:
        return {"documents": []}

    app = create_app(
        ask_router=ask_router,
        documents_router=documents_router,
        synthetic_fallback=False,
    )
    with TestClient(app) as client:
        assert app.state.integration["data_mode"] == "real"
        assert client.post("/api/ask", json={"question": "test"}).json()["status"] == "not_found"
        assert client.get("/api/documents").json() == {"documents": []}
