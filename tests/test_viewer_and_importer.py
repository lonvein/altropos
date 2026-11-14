"""Тесты безопасного HTML-просмотрщика источников и защищённого импорта с mipt.ru (ИСКУССТВЕННЫЙ ПРИМЕР)."""
from __future__ import annotations

import io
import json
import time
from html import escape

import docx
import httpx
import pytest
from fastapi.testclient import TestClient

from navigator.config import Settings
from navigator.db import Store
from navigator.documents import (
    DocumentService,
    fetch_mipt_bytes,
    format_locator_human,
    inspect_mipt_page,
    render_source_html,
    validate_mipt_url,
)
from navigator.main import create_app
from navigator.schemas import (
    DOCXLocator,
    Fragment,
    HTMLLocator,
    MetadataPatch,
    PDFLocator,
    XLSXLocator,
)


def _make_artificial_docx_bytes() -> bytes:
    document = docx.Document()
    document.add_heading("ИСКУССТВЕННЫЙ ПРИМЕР: Учебный регламент", level=1)
    document.add_paragraph("ИСКУССТВЕННЫЙ ПРИМЕР. Заявление на пропуск подаётся за три рабочих дня.")
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _wait_job(client: TestClient, job_id: str) -> dict:
    end = time.monotonic() + 10.0
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"ready", "failed", "needs_review"}:
            return job
        time.sleep(0.02)
    pytest.fail(f"Задание {job_id} не завершилось вовремя")


# ---------------------------------------------------------------------------
# Тесты HTML-просмотрщика (viewer.py)
# ---------------------------------------------------------------------------


def test_format_locator_human_all_kinds():
    assert "PDF, стр. 3" in format_locator_human({"kind": "pdf", "page": 3, "bbox": [10.2, 20.8, 300.1, 400.9]})
    assert "область [10, 21, 300, 401]" in format_locator_human({"kind": "pdf", "page": 3, "bbox": [10.2, 20.8, 300.1, 400.9]})
    assert format_locator_human({"kind": "docx", "block": 4}) == "DOCX, блок 5"
    assert format_locator_human({"kind": "xlsx", "sheet": "Тарифы", "range": "A1:C5"}) == "XLSX, лист «Тарифы», диапазон A1:C5"
    assert format_locator_human({"kind": "html", "block": 0}) == "HTML, блок 1"


def test_render_source_html_highlights_target_and_escapes_xss():
    f1 = Fragment(
        id="frag-1",
        document_id="doc-1",
        document_version="v1",
        ordinal=0,
        text="ИСКУССТВЕННЫЙ ПРИМЕР 1: <img src=x onerror=alert(1)>",
        section="Раздел <script>1</script>",
        locator=DOCXLocator(block=0),
    )
    f2 = Fragment(
        id="frag-2",
        document_id="doc-1",
        document_version="v1",
        ordinal=1,
        text="ИСКУССТВЕННЫЙ ПРИМЕР 2: целевой пункт <b>важно</b>",
        section="Раздел 2",
        locator=XLSXLocator(sheet="Лист1", range="A2:B4"),
    )
    f3 = Fragment(
        id="frag-3",
        document_id="doc-1",
        document_version="v1",
        ordinal=2,
        text="ИСКУССТВЕННЫЙ ПРИМЕР 3: заключительный абзац",
        section="",
        locator=PDFLocator(page=2, bbox=(1.0, 2.0, 50.0, 60.0)),
    )

    html = render_source_html(
        title="ИСКУССТВЕННЫЙ ПРИМЕР <script>xss()</script>",
        target_fragment=f2,
        all_fragments=[f3, f1, f2],  # намеренно перемешанный порядок
        metadata=MetadataPatch(),  # без дат — должна быть пометка о неизвестной актуальности
    )

    assert "<script>" not in html
    assert "&lt;script&gt;xss()&lt;/script&gt;" in html
    assert "&lt;script&gt;1&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert '<p id="fragment-frag-2"><mark>ИСКУССТВЕННЫЙ ПРИМЕР 2: целевой пункт &lt;b&gt;важно&lt;/b&gt;</mark></p>' in html
    assert '<p id="fragment-frag-1">' in html
    assert '<p id="fragment-frag-3">' in html
    # Соседние фрагменты идут в порядке ordinal (frag-1 до frag-2 до frag-3)
    assert html.index("fragment-frag-1") < html.index("fragment-frag-2") < html.index("fragment-frag-3")
    # Сохранена точная строка JSON-локатора и человекочитаемое описание
    assert escape(json.dumps(f2.locator.model_dump(mode="json"), ensure_ascii=False)) in html
    assert "XLSX, лист «Лист1», диапазон A2:B4" in html
    assert "Дата вступления в силу или редакция не указаны" in html
    assert 'href="/static/css/source.css"' in html


def test_render_source_html_shows_metadata_dates_when_present():
    frag = Fragment(
        id="frag-html",
        document_id="doc-html",
        document_version="v1",
        ordinal=0,
        text="ИСКУССТВЕННЫЙ ПРИМЕР: текст HTML-фрагмента.",
        section="",
        locator=HTMLLocator(block=0),
    )
    html = render_source_html(
        title="Искусственный приказ",
        target_fragment=frag,
        all_fragments=[frag],
        metadata={"number": "ПР-10", "issued": "2026-02-01", "valid_from": "2026-02-10", "reviewed": True},
    )
    assert "Дата вступления в силу или редакция не указаны" not in html
    assert "№ ПР-10" in html
    assert "издан 2026-02-01" in html
    assert "действует с 2026-02-10" in html


# ---------------------------------------------------------------------------
# Тесты валидации URL и защищённого импорта (site_importer.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "valid_url",
    [
        "https://mipt.ru/docs/prikaz.pdf",
        "http://pk.mipt.ru/upload/rules.docx#section",
        "https://abitur.mipt.ru/page/",
        "  https://MIpT.RU./info  ",
    ],
)
def test_validate_mipt_url_allows_mipt_domains(valid_url):
    cleaned = validate_mipt_url(valid_url)
    assert cleaned.startswith(("http://", "https://"))
    assert "#" not in cleaned


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "https://example.org/doc.pdf",
        "https://evil-mipt.ru/doc.pdf",
        "https://mipt.ru.evil.org/doc.pdf",
        "https://mipt.ru@evil.org/doc.pdf",
        "https://user:pass@mipt.ru/doc.pdf",
        "http://127.0.0.1/doc.pdf",
        "http://localhost/doc.pdf",
        "file:///etc/passwd",
        "ftp://mipt.ru/doc.pdf",
        "javascript:alert(1)",
    ],
)
def test_validate_mipt_url_rejects_unsafe_urls(bad_url):
    with pytest.raises(ValueError):
        validate_mipt_url(bad_url)


def test_fetch_mipt_bytes_blocks_redirect_outside_mipt():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://mipt.ru/redirect-out":
            return httpx.Response(302, headers={"Location": "https://evil.example.org/secret.pdf"})
        if str(request.url) == "https://mipt.ru/redirect-ok":
            return httpx.Response(302, headers={"Location": "https://docs.mipt.ru/file.pdf"})
        if str(request.url) == "https://docs.mipt.ru/file.pdf":
            return httpx.Response(200, content=b"%PDF-ARTIFICIAL")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="mipt.ru"):
        fetch_mipt_bytes("https://mipt.ru/redirect-out", max_bytes=1024 * 1024, transport=transport)

    final_url, data, _ = fetch_mipt_bytes("https://mipt.ru/redirect-ok", max_bytes=1024 * 1024, transport=transport)
    assert final_url == "https://docs.mipt.ru/file.pdf"
    assert data == b"%PDF-ARTIFICIAL"


def test_fetch_mipt_bytes_enforces_max_bytes():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"X" * 2048)
    )
    with pytest.raises(ValueError, match="лимит"):
        fetch_mipt_bytes("https://mipt.ru/large.pdf", max_bytes=512, transport=transport)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "224.0.0.1"])
def test_import_transport_blocks_private_dns_before_connection(monkeypatch, address):
    from navigator.documents import site_importer as module
    monkeypatch.setattr(module.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", (address, 443))])
    monkeypatch.setattr(module.httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(
        lambda request: pytest.fail("Private address must never be requested")))
    with pytest.raises(ValueError, match="непубличных"):
        fetch_mipt_bytes("https://internal.mipt.ru/file", max_bytes=100)


def test_import_transport_pins_dns_preserves_tls_identity_and_checks_redirect(monkeypatch):
    from navigator.documents import site_importer as module
    lookups, requests = [], []
    def resolve(host, port, **kw):
        lookups.append(host)
        return [(0, 0, 0, "", ("93.184.216.34" if host == "mipt.ru" else "127.0.0.1", port))]
    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "mipt.ru"
        assert request.extensions["sni_hostname"] == "mipt.ru"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(302, headers={"location": "https://internal.mipt.ru/private"})
    monkeypatch.setattr(module.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(module.httpx, "HTTPTransport", lambda **kw: httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="непубличных"):
        fetch_mipt_bytes("https://mipt.ru/file", max_bytes=100)
    assert lookups == ["mipt.ru", "internal.mipt.ru"]
    assert len(requests) == 1


@pytest.mark.parametrize("url", ["https://mipt.ru:22/", "https://mipt.ru:bad/", "https://mi\npt.ru/", "https://mipt.ru/\rsecret"])
def test_import_rejects_control_characters_and_ports(url):
    with pytest.raises(ValueError):
        validate_mipt_url(url)


def test_import_refuses_compressed_body_before_decoding():
    import gzip
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"content-encoding": "gzip"}, content=gzip.compress(b"x" * 10000)))
    with pytest.raises(ValueError, match="сжатый"):
        fetch_mipt_bytes("https://mipt.ru/file", max_bytes=100, transport=transport)


def test_import_rejects_malformed_angular_sections():
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        text="<app-root></app-root>" if request.url.path != "/api/v1/" else '[{"sectionId": [], "code": "ru", "id": 1}]'))
    with pytest.raises(ValueError, match="дерево разделов"):
        inspect_mipt_page("https://mipt.ru/documents", max_bytes=1000, transport=transport)


@pytest.mark.parametrize("status", ["failed", "needs_review"])
@pytest.mark.parametrize("reviewed", [True, False])
@pytest.mark.parametrize("active_publication", [True, False])
def test_startup_never_approves_from_metadata_or_warning_prose(tmp_path, status, reviewed, active_publication):
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    store = Store(tmp_path)
    service = DocumentService(store, settings)
    uploaded = service.upload_file("artificial.html", b"<p>SYNTHETIC pending review document.</p>")
    assert service.process_next_job()
    doc_id = uploaded["document_id"]
    row = store.document(doc_id)
    payload = json.loads(row["payload"])
    payload["status"] = status
    payload["metadata"]["reviewed"] = reviewed
    payload["warnings"] = ["Страница 1: номер визуально подтверждён оператором по оригиналу"]
    store.execute("UPDATE documents SET status=?,metadata=?,payload=?,active_publication=? WHERE id=?",
        (status, json.dumps(payload["metadata"]), json.dumps(payload),
         row["active_publication"] if active_publication else None, doc_id))
    before = store.document(doc_id)
    restarted = DocumentService(store, settings)
    assert store.document(doc_id) == before
    assert restarted.get_document(doc_id)["metadata"]["reviewed"] is reviewed
    assert store.active_publications() == []


def test_inspect_mipt_page_separates_allowed_and_rejected_links():
    page_html = """<!doctype html>
    <html lang="ru">
      <head><title>ИСКУССТВЕННАЯ СТРАНИЦА МФТИ</title></head>
      <body>
        <h1>Нормативные документы (ИСКУССТВЕННЫЙ ПРИМЕР)</h1>
        <p>Выберите документы для загрузки.</p>
        <a href="/upload/reglament.pdf">Приказ (PDF)</a>
        <a href="https://pk.mipt.ru/docs/pravila.docx">Правила (DOCX)</a>
        <a href="https://mipt.ru/files/archive.zip">Архив (ZIP)</a>
        <a href="https://external.example.org/evil.pdf">Внешний PDF</a>
        <a href="javascript:void(0)">JS ссылка</a>
      </body>
    </html>"""
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=page_html, headers={"content-type": "text/html; charset=utf-8"})
    )

    preview = inspect_mipt_page("https://mipt.ru/docs/", max_bytes=1024 * 1024, transport=transport)
    assert preview["page_title"] == "ИСКУССТВЕННАЯ СТРАНИЦА МФТИ"
    assert len(preview["links"]) == 2
    assert {item["url"] for item in preview["links"]} == {
        "https://mipt.ru/upload/reglament.pdf",
        "https://pk.mipt.ru/docs/pravila.docx",
    }
    assert {item["kind"] for item in preview["links"]} == {"pdf", "docx"}
    assert len(preview["rejected"]) == 3
    rejected_urls = {item["url"] for item in preview["rejected"]}
    assert "https://external.example.org/evil.pdf" in rejected_urls
    assert "https://mipt.ru/files/archive.zip" in rejected_urls


def test_import_mipt_dynamic_page_and_download_endpoint(tmp_path):
    page = "https://mipt.ru/it/documents"
    url = "https://mipt.ru/docs/download.php?code=artificial-rule"
    unsupported = "https://mipt.ru/docs/download.php?code=artificial-html"
    filename = "Учебный регламент.docx"
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if str(request.url) == page:
            return httpx.Response(200, text="<html><app-root></app-root></html>")
        if request.url.params.get("action") == "blockPage.getList":
            return httpx.Response(200, json=[
                {"id": 1, "sectionId": None, "code": "ru"},
                {"id": 2, "sectionId": 1, "code": "it"},
                {"id": 3, "sectionId": 2, "code": "documents"},
                {"id": 4, "sectionId": 1, "code": "documents"},
            ])
        if request.url.params.get("action") == "blockPage.getItems":
            assert request.url.params["sectionId"] == "3"
            return httpx.Response(200, json=[
                {"previewText": "<h1>Документы</h1>"},
                {"detailText": [{"html": (
                    f'<a href="{url}"></a><a href="{url}">Учебный регламент</a>'
                    f'<a href="{unsupported}">Другой файл</a>'
                )}]},
            ])
        if str(request.url) == url:
            return httpx.Response(200, content=_make_artificial_docx_bytes(), headers=[
                (b"content-type", b"application/octet-stream"),
                (b"content-disposition", f'attachment; filename="{filename}"'.encode()),
            ])
        if str(request.url) == unsupported:
            return httpx.Response(200, text="<p>Файл отсутствует</p>")
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    service = DocumentService(Store(tmp_path), settings, http_transport=httpx.MockTransport(handler))
    with TestClient(create_app(document_service=service)) as client:
        def import_job(source, selected=()):
            result = client.post("/api/import-url", json={"url": source, "selected_urls": selected})
            assert result.status_code == 200
            return _wait_job(client, result.json()["job_id"])

        preview = import_job(page)["result"]
        assert preview["page_title"] == "Документы"
        assert [link["url"] for link in preview["links"]] == [url, unsupported]
        assert preview["links"][0]["title"] == "Учебный регламент"
        assert url not in requested and unsupported not in requested

        direct = import_job(url)["result"]
        assert direct["links"] == [{"url": url, "title": filename, "kind": "docx", "allowed": True}]
        result = import_job(page, [url, unsupported])["result"]
        assert len(result["documents"]) == len(result["rejected"]) == 1
        document = result["documents"][0]
        assert document["name"] == filename and document["source_url"] == url
        assert document["status"] == "ready"
        persisted = service.store.document(document["id"])
        assert persisted["url"] == url
        assert json.loads(persisted["payload"])["source_url"] == url
        assert json.loads(persisted["payload"])["retrieved_at"]
        assert client.get(f'/api/documents/{document["id"]}/review').status_code == 200


# ---------------------------------------------------------------------------
# Интеграционный тест POST /api/import-url -> queue -> ask -> viewer
# ---------------------------------------------------------------------------


def test_import_url_preview_and_selected_end_to_end(tmp_path):
    docx_bytes = _make_artificial_docx_bytes()
    page_html = """<!doctype html>
    <html lang="ru">
      <head><title>ИСКУССТВЕННАЯ СТРАНИЦА: Документы</title></head>
      <body>
        <a href="/docs/artificial-reg.docx">Учебный регламент (DOCX)</a>
        <a href="https://evil.example.org/bad.pdf">Внешний файл</a>
      </body>
    </html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://mipt.ru/docs":
            return httpx.Response(200, text=page_html, headers={"content-type": "text/html; charset=utf-8"})
        if url == "https://mipt.ru/docs/artificial-reg.docx":
            return httpx.Response(
                200,
                content=docx_bytes,
                headers={"content-type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
            )
        return httpx.Response(404)

    settings = Settings(data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    service = DocumentService(Store(tmp_path), settings, http_transport=httpx.MockTransport(handler))
    app = create_app(document_service=service)

    with TestClient(app) as client:
        # 1. Внешний URL сразу отклоняется с 422
        bad = client.post("/api/import-url", json={"url": "https://evil-mipt.ru/docs"})
        assert bad.status_code == 422

        # 2. Предпросмотр страницы mipt.ru
        preview_resp = client.post("/api/import-url", json={"url": "https://mipt.ru/docs", "selected_urls": []})
        assert preview_resp.status_code == 200
        preview_job = _wait_job(client, preview_resp.json()["job_id"])
        assert preview_job["status"] == "ready"
        assert len(preview_job["result"]["links"]) == 1
        assert preview_job["result"]["links"][0]["url"] == "https://mipt.ru/docs/artificial-reg.docx"
        assert len(preview_job["result"]["rejected"]) == 1

        # 3. Импорт выбранного документа (+ одна подброшенная запрещённая ссылка)
        import_resp = client.post(
            "/api/import-url",
            json={
                "url": "https://mipt.ru/docs",
                "selected_urls": [
                    "https://mipt.ru/docs/artificial-reg.docx",
                    "https://evil.example.org/bad.pdf",
                ],
            },
        )
        assert import_resp.status_code == 200
        import_job = _wait_job(client, import_resp.json()["job_id"])
        assert import_job["status"] == "ready"
        assert len(import_job["result"]["documents"]) == 1
        assert len(import_job["result"]["rejected"]) == 1

        imported_doc = import_job["result"]["documents"][0]
        assert imported_doc["status"] == "ready"
        assert imported_doc["source_url"] == "https://mipt.ru/docs/artificial-reg.docx"
        assert any("mipt.ru" in w for w in imported_doc["warnings"])

        # 4. Документ появился в списке и ищется через /api/ask, источник открывается через viewer
        docs_list = client.get("/api/documents").json()["documents"]
        assert any(d["id"] == imported_doc["id"] and d["source_url"] == "https://mipt.ru/docs/artificial-reg.docx" for d in docs_list)

        ask_res = client.post("/api/ask", json={"question": "Заявление на пропуск"}).json()
        assert len(ask_res["sources"]) >= 1
        source_url = ask_res["sources"][0]["url"]
        viewer_resp = client.get(source_url)
        assert viewer_resp.status_code == 200
        assert "<mark>" in viewer_resp.text
        assert "Заявление на пропуск подаётся за три рабочих дня." in viewer_resp.text
