"""Tests for review Back navigation, re-review vs re-OCR separation, fullscreen source viewer, and original highlight coordinates."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient
from PIL import Image

from navigator.config import Settings
from navigator.documents.highlighting import resolve_fragment_highlight
from navigator.ingestion import Parsed
from navigator.ingestion import ocr as ocr_mod
from navigator.ingestion.common import Unit
from navigator.main import create_app
from navigator.review.workflow import (
    prepare_document,
    record_decision,
    reocr_package,
    reopen_package_review,
    show_package,
    update_package_extraction,
    write_index,
)
from navigator.schemas import Fragment

_DOCX_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _make_app(monkeypatch, settings: Settings, *, synthetic: bool = False):
    monkeypatch.setattr("navigator.main.get_settings", lambda: settings)
    return create_app(synthetic_fallback=synthetic)


def _wait_processed(client: TestClient, svc, doc_id: str) -> dict:
    svc.process_next_job()
    for _ in range(80):
        res = client.get(f"/api/documents/{doc_id}/review")
        if res.status_code == 200:
            doc = res.json()["document"]
            if doc["status"] not in {"queued", "processing"}:
                return res.json()
        svc.process_next_job()
        time.sleep(0.05)
    raise AssertionError(f"Document {doc_id} did not finish processing")


def _build_text_pdf(pages_lines: list[list[tuple[float, float, str]]]) -> bytes:
    """Build a valid multi-page PDF with Helvetica text placed at exact (x, y) PDF coordinates."""
    objs: list[bytes] = []
    n_pages = len(pages_lines)
    font_obj_id = 3 + n_pages * 2
    kids_refs = " ".join(f"{3 + i * 2} 0 R" for i in range(n_pages))

    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids_refs}] /Count {n_pages} >>".encode("ascii"))

    for i, lines in enumerate(pages_lines):
        page_id = 3 + i * 2
        content_id = page_id + 1
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_obj_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        cmds = ["BT", "/F1 12 Tf"]
        for x, y, text in lines:
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            cmds.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm ({safe}) Tj")
        cmds.append("ET")
        stream = "\n".join(cmds).encode("latin-1", "replace")
        content_dict = f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        objs.append(page_dict)
        objs.append(content_dict)

    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def _build_image_only_pdf(page_count: int = 2, size: tuple[int, int] = (600, 800)) -> bytes:
    """Create a raster-only multi-page PDF without embedded text streams."""
    pages = [Image.new("RGB", size, (255, 255, 255)) for _ in range(page_count)]
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def _build_simple_docx(paragraphs: list[str]) -> bytes:
    """Build a minimal valid DOCX archive containing plain paragraphs."""
    p_xml = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    doc_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{_DOCX_NS}"><w:body>{p_xml}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with ZipFile(buf, "w", ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)
    return buf.getvalue()


def test_review_back_navigation_and_state_restoration(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        review_dir=tmp_path / "review",
        ml_enabled=False,
        llm_enabled=False,
    )
    app = _make_app(monkeypatch, settings, synthetic=False)
    with TestClient(app) as client:
        # 1. Interactive /review without static index opens review workspace with Back button
        res_root = client.get("/review")
        assert res_root.status_code == 200
        assert "data-review-back" in res_root.text
        assert 'data-back-fallback="/documents"' in res_root.text
        assert "← Назад" in res_root.text
        assert "/static/js/review-nav.js" in res_root.text
        csp = res_root.headers.get("content-security-policy", "")
        assert "connect-src 'self'" in csp
        assert "script-src 'self'" in csp

        # 2. Deep link /review?doc=<id> preserves initial selected doc id
        res_doc = client.get("/review?doc=doc-selected-99")
        assert res_doc.status_code == 200
        assert 'data-initial-doc-id="doc-selected-99"' in res_doc.text
        assert "data-review-back" in res_doc.text

        # 3. Verify static JS contains state preservation keys & anchor restore logic
        js_review = client.get("/static/js/review-nav.js").text
        js_docs = client.get("/static/js/documents.js").text
        for key in (
            "altropos.review.returnUrl",
            "altropos.review.selectedDocumentId",
            "altropos.review.lastResult",
        ):
            assert key in js_review
            assert key in js_docs
        assert "document-card--selected" in js_docs

    # 4. Static review package also includes Back navigation and respects ?doc= override
    src_pdf = tmp_path / "pkg-src.pdf"
    src_pdf.write_bytes(_build_text_pdf([[(72.0, 720.0, "Package text for review index check.")]]))
    prepare_document(
        src_pdf,
        settings.review_dir,
        parsed=Parsed(
            media_type="pdf",
            title="Пакет регламента",
            units=[Unit(text="Тестовый блок", locator={"kind": "pdf", "page": 1, "bbox": [10.0, 10.0, 200.0, 30.0]})],
            blocks=[{"page": 1, "kind": "paragraph", "text": "Тестовый блок", "bbox": [10.0, 10.0, 200.0, 30.0]}],
            warnings=["Требует проверки"],
            needs_review=True,
        ),
    )
    write_index(settings.review_dir)

    with TestClient(_make_app(monkeypatch, settings, synthetic=False)) as client:
        redirect_resp = client.get("/review", follow_redirects=False)
        assert redirect_resp.status_code in {302, 307}
        assert redirect_resp.headers["location"].endswith("/review-files/index.html")

        direct_doc_resp = client.get("/review?doc=doc-123")
        assert direct_doc_resp.status_code == 200
        assert 'data-initial-doc-id="doc-123"' in direct_doc_resp.text

        static_index = client.get("/review-files/index.html")
        assert static_index.status_code == 200
        assert "data-review-back" in static_index.text
        assert "/static/js/review-nav.js" in static_index.text


def test_rereview_and_separate_reocr_on_ready_approved_document(tmp_path, monkeypatch):
    pdf_bytes = _build_text_pdf([
        [
            (72.0, 740.0, "Section 1. General provisions for room access."),
            (72.0, 715.0, "Initial OCR phrase line one with token ALPHA111."),
            (72.0, 690.0, "Mandatory check procedure before entering hall."),
        ]
    ])
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=False)

    with TestClient(app) as client:
        up = client.post("/api/documents", files={"file": ("policy.pdf", pdf_bytes, "application/pdf")})
        assert up.status_code == 200
        doc_id = up.json()["document_id"]

        svc = app.state.document_service
        detail_ready = _wait_processed(client, svc, doc_id)
        assert detail_ready["document"]["status"] == "ready"

        orig_first = client.get(f"/api/documents/{doc_id}/original")
        assert orig_first.status_code == 200
        assert orig_first.content == pdf_bytes

        # 1. Confirm initial document -> ready + approved
        approve_1 = client.post(
            f"/api/documents/{doc_id}/review",
            json={"action": "review", "decision": "approved", "reviewer": "Первый эксперт", "comment": "Первое подтверждение"},
        )
        assert approve_1.status_code == 200
        d1 = approve_1.json()
        assert d1["document"]["status"] == "ready"
        assert d1["document"]["metadata"]["reviewed"] is True
        assert len(d1["review_history"]) == 1
        assert d1["review_history"][0]["decision"] == "approved"

        # 2. Re-open already ready & approved document without losing prior confirmation
        reopen = client.post(
            f"/api/documents/{doc_id}/review",
            json={"action": "reopen", "reviewer": "Второй эксперт", "comment": "Нужно сверить термин"},
        )
        assert reopen.status_code == 200
        d_reopen = reopen.json()
        assert d_reopen["document"]["status"] == "ready"
        assert len(d_reopen["review_history"]) == 2
        assert d_reopen["review_history"][1]["action"] == "reopen_review"

        # 3. Edit recognized blocks and re-approve WITHOUT re-running OCR/parser
        from navigator.documents import service as service_mod

        ocr_calls = 0

        def fail_if_reocr(*args, **kwargs):
            nonlocal ocr_calls
            ocr_calls += 1
            raise AssertionError("OCR/parse_file must not run during manual review save")

        monkeypatch.setattr(service_mod, "parse_file", fail_if_reocr)

        edited_blocks = [dict(blk) for blk in d_reopen["blocks"]]
        edited_blocks[0]["text"] = "Verified corrected clause with token XYZ987."
        approve_2 = client.post(
            f"/api/documents/{doc_id}/review",
            json={
                "action": "review",
                "decision": "approved",
                "reviewer": "Второй эксперт",
                "comment": "Исправлена формулировка",
                "blocks": edited_blocks,
            },
        )
        assert approve_2.status_code == 200
        assert ocr_calls == 0
        d2 = approve_2.json()
        assert d2["document"]["status"] == "ready"
        assert d2["document"]["version"] == "v2"
        assert d2["document"]["metadata"]["reviewed"] is True
        assert [h["reviewer"] for h in d2["review_history"]] == [
            "Первый эксперт",
            "Второй эксперт",
            "Второй эксперт",
        ]
        assert d2["review_history"][2]["action"] == "edit_results"

        # Verify original file untouched and search reflects v2 edited text
        assert client.get(f"/api/documents/{doc_id}/original").content == pdf_bytes
        ask_edited = client.post("/api/ask", json={"question": "Какой токен XYZ987 указан в документе?"})
        assert ask_edited.status_code == 200
        ask_json = ask_edited.json()
        assert ask_json["sources"], f"Expected sources for edited text, got {ask_json}"
        v2_frag_id = ask_json["sources"][0]["fragment_id"]
        assert "-v2-" in v2_frag_id
        assert "XYZ987" in client.get(ask_json["sources"][0]["url"]).text

        # 4. Separately trigger Re-OCR on the still-ready & approved document
        reocr_calls = 0

        def stub_reocr(path, **kwargs):
            nonlocal reocr_calls
            reocr_calls += 1
            new_text = "Section 2. Freshly re-OCR extracted regulation rule QWE654."
            return Parsed(
                media_type="pdf",
                title="Обновлённый регламент",
                units=[Unit(text=new_text, locator={"kind": "pdf", "page": 1, "bbox": [72.0, 100.0, 480.0, 118.0]})],
                blocks=[{
                    "page": 1,
                    "kind": "paragraph",
                    "text": new_text,
                    "bbox": [72.0, 100.0, 480.0, 118.0],
                    "page_size": [595.0, 842.0],
                    "lines": [{"text": new_text, "bbox": [72.0, 100.0, 480.0, 118.0]}],
                }],
            )

        monkeypatch.setattr(service_mod, "parse_file", stub_reocr)

        reocr_resp = client.post(
            f"/api/documents/{doc_id}/reocr",
            json={"reviewer": "Оператор OCR", "comment": "Повторный OCR по оригиналу"},
        )
        assert reocr_resp.status_code == 200
        assert reocr_resp.json()["version"] == "v3"
        assert reocr_resp.json()["status"] == "queued"

        detail_v3 = _wait_processed(client, svc, doc_id)
        assert reocr_calls == 1
        assert detail_v3["document"]["status"] == "ready"
        assert detail_v3["document"]["version"] == "v3"
        assert any(h["action"] == "reocr" and h["reviewer"] == "Оператор OCR" for h in detail_v3["review_history"])
        assert detail_v3["review_history"][0]["decision"] == "approved"
        assert client.get(f"/api/documents/{doc_id}/original").content == pdf_bytes

        ask_reocr = client.post("/api/ask", json={"question": "Что говорит правило QWE654?"})
        assert ask_reocr.status_code == 200
        reocr_json = ask_reocr.json()
        assert reocr_json["sources"], f"Expected sources for re-OCR text, got {reocr_json}"
        v3_frag_id = reocr_json["sources"][0]["fragment_id"]
        assert "-v3-" in v3_frag_id
        assert "QWE654" in client.get(reocr_json["sources"][0]["url"]).text


def test_package_workflow_rereview_and_reocr_history(tmp_path):
    orig = tmp_path / "src.pdf"
    orig.write_bytes(_build_text_pdf([[(72.0, 720.0, "Original clause one.")]]))
    prep = prepare_document(
        orig,
        tmp_path / "review",
        parsed=Parsed(
            media_type="pdf",
            title="Приказ о закупках",
            units=[Unit(text="Старый текст пункта 1.", locator={"kind": "pdf", "page": 1, "bbox": [10.0, 20.0, 200.0, 40.0]})],
            blocks=[{"page": 1, "kind": "paragraph", "text": "Старый текст пункта 1.", "bbox": [10.0, 20.0, 200.0, 40.0]}],
            warnings=[],
            needs_review=False,
        ),
    )
    pkg_dir = Path(prep["package_dir"])

    r1 = record_decision(pkg_dir, "approved", reviewer="Эксперт 1", comment="Утверждено")
    assert r1["status"] == "approved" and r1["history"] == []

    r2 = reopen_package_review(pkg_dir, reviewer="Эксперт 2", comment="Перепроверка")
    assert r2["status"] == "pending" and len(r2["history"]) == 2
    assert r2["history"][0]["status"] == "approved"
    assert r2["history"][1]["action"] == "reopen"

    r3 = update_package_extraction(
        pkg_dir,
        blocks=[{"page": 1, "kind": "paragraph", "text": "Исправленный текст пункта 1.", "bbox": [10.0, 20.0, 200.0, 40.0]}],
        warnings=[],
        needs_review=False,
        reviewer="Эксперт 2",
        comment="Опечатка исправлена",
    )
    assert len(r3["review"]["history"]) == 3

    fresh_parsed = Parsed(
        media_type="pdf",
        title="Приказ о закупках",
        units=[Unit(text="Повторно распознанный текст пункта 1.", locator={"kind": "pdf", "page": 1})],
    )
    r4 = reocr_package(pkg_dir, parsed=fresh_parsed, reviewer="Оператор OCR", comment="Перезапуск OCR")
    assert len(r4["review"]["history"]) == 4
    saved = show_package(pkg_dir)
    assert [h.get("action") or h.get("status") for h in saved["review"]["history"]] == [
        "approved",
        "reopen",
        "edit_results",
        "reocr",
    ]
    assert (pkg_dir / saved["manifest"]["original"]).read_bytes() == orig.read_bytes()


def test_fullscreen_source_viewer_and_synthetic_routes(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=True)
    with TestClient(app) as client:
        for path in ("/", "/documents"):
            html = client.get(path).text
            assert 'id="source-dialog"' in html
            assert "dialog--fullscreen" in html
            assert 'id="source-page-controls"' in html
            assert 'id="source-zoom-controls"' in html
            assert 'id="source-presentation-btn"' in html
            assert 'id="source-original"' in html
            assert 'data-close-dialog' in html
            assert "/static/js/source-viewer.js" in html

        ask_res = client.post("/api/ask", json={"question": "Что нужно для прохода в корпус?"})
        assert ask_res.status_code == 200
        src = ask_res.json()["sources"][0]
        doc_id = src["document_id"]
        frag_id = src["fragment_id"]

        info = client.get(f"/api/documents/{doc_id}/original-info")
        assert info.status_code == 200
        assert info.json()["page_count"] >= 1
        assert info.json()["supports_bbox"] is False

        hl = client.get(f"/api/sources/{frag_id}/highlight")
        assert hl.status_code == 200
        hl_data = hl.json()
        assert hl_data["exact"] is False
        assert hl_data["boxes"] == []
        assert hl_data["notice"]

        # Uploaded PDF in synthetic mode also renders original PDF PNG page
        demo_pdf = _build_text_pdf([[(72.0, 720.0, "Synthetic uploaded PDF page one.")]])
        up_pdf = client.post("/api/documents", files={"file": ("demo.pdf", demo_pdf, "application/pdf")})
        assert up_pdf.status_code == 200
        pdf_id = up_pdf.json()["document_id"]
        page_png = client.get(f"/api/documents/{pdf_id}/original-page/1")
        assert page_png.status_code == 200
        assert page_png.headers["content-type"].startswith("image/png")
        assert page_png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_multiline_native_pdf_highlight_coordinates_and_original_page_render(tmp_path, monkeypatch):
    pdf_bytes = _build_text_pdf([
        [
            (72.0, 720.0, "Page 1 overview sentence describing the administrative order and general terms."),
            (72.0, 695.0, "This opening page establishes basic scope and responsibilities for staff."),
        ],
        [
            (72.0, 700.0, "MultiLineTarget First line stating safety compliance rule ALPHA-1."),
            (72.0, 678.0, "MultiLineTarget Second line stating mandatory registration BETA-2."),
            (72.0, 656.0, "MultiLineTarget Third line stating archival storage GAMMA-3."),
        ],
    ])
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=False)
    with TestClient(app) as client:
        up = client.post("/api/documents", files={"file": ("multiline.pdf", pdf_bytes, "application/pdf")})
        assert up.status_code == 200
        doc_id = up.json()["document_id"]
        svc = app.state.document_service
        _wait_processed(client, svc, doc_id)

        frags = svc.store.rows("SELECT id, locator FROM fragments WHERE document_id=? ORDER BY ordinal", (doc_id,))
        page2_frags = [f for f in frags if json.loads(f["locator"]).get("page") == 2]
        assert page2_frags
        frag_id = page2_frags[0]["id"]

        info = client.get(f"/api/documents/{doc_id}/original-info").json()
        assert info["page_count"] == 2
        assert info["supports_bbox"] is True

        png_res = client.get(f"/api/documents/{doc_id}/original-page/2")
        assert png_res.status_code == 200
        assert png_res.content[:8] == b"\x89PNG\r\n\x1a\n"
        with Image.open(io.BytesIO(png_res.content)) as rendered:
            assert rendered.width > 300 and rendered.height > 400
            extrema = rendered.convert("L").getextrema()
            assert extrema[0] < extrema[1]

        quote = (
            "MultiLineTarget First line stating safety compliance rule ALPHA-1. "
            "MultiLineTarget Second line stating mandatory registration BETA-2."
        )
        hl_res = client.get(f"/api/sources/{frag_id}/highlight", params={"quote": quote})
        assert hl_res.status_code == 200
        data = hl_res.json()
        assert data["page"] == 2
        assert data["page_count"] == 2
        assert data["exact"] is True
        assert data["notice"] is None
        assert len(data["boxes"]) >= 2
        assert data["boxes"][0]["bbox"][1] < data["boxes"][1]["bbox"][1]
        for box in data["boxes"]:
            norm = box["norm"]
            assert 0.0 <= norm["left"] < 1.0
            assert 0.0 <= norm["top"] < 1.0
            assert 0.0 < norm["width"] <= 1.0 - norm["left"] + 1e-6
            assert 0.0 < norm["height"] <= 1.0 - norm["top"] + 1e-6


def test_multiline_ocr_pdf_highlight_coordinates(tmp_path, monkeypatch):
    scanned_pdf = _build_image_only_pdf(page_count=2, size=(600, 800))

    monkeypatch.setattr(ocr_mod, "ocr_status", lambda *a: {"available": True, "tessdata": None})
    monkeypatch.setattr(ocr_mod, "find_tesseract", lambda *a: "synthetic-tesseract")
    monkeypatch.setattr(ocr_mod, "_deskew_angle", lambda image: 0.0)

    import pytesseract

    monkeypatch.setattr(pytesseract, "image_to_osd", lambda *a, **kw: {"rotate": 0, "orientation_conf": 20.0})

    call_idx = {"n": 0}

    def fake_image_to_data(*args, **kwargs):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            return {
                "text": ["First", "page", "cover", "title", "and", "overview", "of", "the", "regulation", "document."],
                "conf": ["95"] * 10,
                "left": [40 + i * 45 for i in range(10)],
                "top": [80] * 10,
                "width": [38] * 10,
                "height": [20] * 10,
                "block_num": [1] * 10,
                "par_num": [1] * 10,
                "line_num": [1] * 10,
            }
        words = ["Clause", "one", "recognized", "on", "top", "line.", "Clause", "two", "recognized", "on", "lower", "line."]
        return {
            "text": words,
            "conf": ["94"] * len(words),
            "left": [50 + (i % 6) * 70 for i in range(len(words))],
            "top": [120] * 6 + [170] * 6,
            "width": [60] * len(words),
            "height": [22] * len(words),
            "block_num": [1] * len(words),
            "par_num": [1] * len(words),
            "line_num": [1] * 6 + [2] * 6,
        }

    monkeypatch.setattr(pytesseract, "image_to_data", fake_image_to_data)

    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=False)
    with TestClient(app) as client:
        up = client.post("/api/documents", files={"file": ("scan.pdf", scanned_pdf, "application/pdf")})
        assert up.status_code == 200
        doc_id = up.json()["document_id"]
        svc = app.state.document_service
        _wait_processed(client, svc, doc_id)

        frags = svc.store.rows("SELECT id, locator FROM fragments WHERE document_id=? ORDER BY ordinal", (doc_id,))
        page2_frags = [f for f in frags if json.loads(f["locator"]).get("page") == 2]
        assert page2_frags
        frag_id = page2_frags[0]["id"]

        hl = client.get(f"/api/sources/{frag_id}/highlight")
        assert hl.status_code == 200
        data = hl.json()
        assert data["page"] == 2
        assert data["exact"] is True
        assert data["notice"] is None
        assert len(data["boxes"]) == 2
        assert data["boxes"][0]["norm"]["top"] < data["boxes"][1]["norm"]["top"]


def test_honest_no_highlight_when_coordinates_unavailable(tmp_path, monkeypatch):
    frag_no_coords = Fragment(
        id="doc-noc-f0000",
        document_id="doc-noc",
        document_version="v1",
        ordinal=0,
        text="Фрагмент текста без координат.",
        locator={"kind": "pdf", "page": 3},
    )
    res_pdf = resolve_fragment_highlight(
        None,
        "pdf",
        frag_no_coords,
        blocks=[{"page": 3, "kind": "paragraph", "text": "Фрагмент текста без координат."}],
        quote="Фрагмент текста без координат.",
    )
    assert res_pdf["page"] == 3
    assert res_pdf["exact"] is False
    assert res_pdf["boxes"] == []
    assert "3" in (res_pdf["notice"] or "")

    frag_docx_missing = Fragment(
        id="doc-noc-d0000",
        document_id="doc-noc-d",
        document_version="v1",
        ordinal=0,
        text="Фрагмент DOCX без исходного файла.",
        locator={"kind": "docx", "block": 0},
    )
    res_docx_none = resolve_fragment_highlight(
        None,
        "docx",
        frag_docx_missing,
        blocks=[{"index": 0, "kind": "paragraph", "text": "Фрагмент DOCX без исходного файла."}],
        quote="Фрагмент DOCX без исходного файла.",
    )
    assert res_docx_none["exact"] is False
    assert res_docx_none["boxes"] == []

    # HTML documents do not have paginated PDF/DOCX bbox coordinates.
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=False)
    with TestClient(app) as client:
        up = client.post(
            "/api/documents",
            files={"file": ("rule.html", "<p>Пункт 1.1. Тестовый HTML документ без координат.</p>".encode("utf-8"), "text/html")},
        )
        assert up.status_code == 200
        doc_id = up.json()["document_id"]
        svc = app.state.document_service
        _wait_processed(client, svc, doc_id)

        frags = svc.store.rows("SELECT id FROM fragments WHERE document_id=? ORDER BY ordinal", (doc_id,))
        assert frags
        hl = client.get(f"/api/sources/{frags[0]['id']}/highlight")
        assert hl.status_code == 200
        data = hl.json()
        assert data["exact"] is False
        assert data["boxes"] == []
        assert data["notice"]


def test_docx_original_page_render_and_multiline_highlight(tmp_path, monkeypatch):
    from docx import Document as DocxDocument

    d = DocxDocument()
    d.add_heading("Об утверждении порядка присвоения номера ФЛС", level=1)
    long_clause = (
        "Пункт 1.1. Код финансового обеспечения (КФО) определяется автоматически в соответствии "
        "с установленным классификатором источников финансирования проекта и подлежит обязательному "
        "согласованию с планово-финансовым управлением при открытии нового лицевого счёта."
    )
    for i in range(55):
        d.add_paragraph(
            f"Предварительный пункт {i + 1}: общие положения порядка учёта и документооборота подразделений института."
        )
    d.add_paragraph(long_clause)
    tbl = d.add_table(rows=2, cols=2)
    tbl.rows[0].cells[0].text = "Разряд"
    tbl.rows[0].cells[1].text = "Назначение кода"
    tbl.rows[1].cells[0].text = "ФЛС-01"
    tbl.rows[1].cells[1].text = "Целевые научные гранты и субсидии"
    buf = io.BytesIO()
    d.save(buf)
    docx_bytes = buf.getvalue()

    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=False)
    with TestClient(app) as client:
        up = client.post(
            "/api/documents",
            files={
                "file": (
                    "fls-order.docx",
                    docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert up.status_code == 200
        doc_id = up.json()["document_id"]
        svc = app.state.document_service
        _wait_processed(client, svc, doc_id)

        orig_resp = client.get(f"/api/documents/{doc_id}/original")
        assert orig_resp.status_code == 200
        assert orig_resp.content == docx_bytes

        info_resp = client.get(f"/api/documents/{doc_id}/original-info")
        assert info_resp.status_code == 200
        info = info_resp.json()
        assert info["media_type"] == "docx"
        assert info["supports_bbox"] is True
        assert info["page_count"] >= 2

        for p in range(1, info["page_count"] + 1):
            png = client.get(f"/api/documents/{doc_id}/original-page/{p}")
            assert png.status_code == 200
            assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
            with Image.open(io.BytesIO(png.content)) as im:
                assert im.width > 300 and im.height > 400
                ext = im.convert("L").getextrema()
                assert ext[0] < ext[1]

        frags = svc.store.rows("SELECT id, text FROM fragments WHERE document_id=? ORDER BY ordinal", (doc_id,))
        target_frag = next(f for f in frags if "классификатором источников финансирования" in f["text"])

        hl_resp = client.get(f"/api/sources/{target_frag['id']}/highlight", params={"quote": long_clause})
        assert hl_resp.status_code == 200
        hl = hl_resp.json()
        assert hl["media_type"] == "docx"
        assert hl["exact"] is True
        assert hl["page_count"] == info["page_count"]
        assert 1 <= hl["page"] <= info["page_count"]
        assert len(hl["boxes"]) >= 2
        assert hl["boxes"][0]["norm"]["top"] < hl["boxes"][1]["norm"]["top"]
        for box in hl["boxes"]:
            norm = box["norm"]
            assert 0.0 <= norm["left"] < 1.0
            assert 0.0 <= norm["top"] < 1.0
            assert 0.0 < norm["width"] <= 1.0 - norm["left"] + 1e-6
            assert 0.0 < norm["height"] <= 1.0 - norm["top"] + 1e-6

        # Recognized text presentation highlights matching block inside article.source-document.
        view_resp = client.get(f"/api/sources/{target_frag['id']}")
        assert view_resp.status_code == 200
        assert "source-block--highlighted" in view_resp.text
        assert "source-inline-mark" in view_resp.text


def test_fullscreen_viewer_proportions_and_recognized_text_layout(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    app = _make_app(monkeypatch, settings, synthetic=True)
    with TestClient(app) as client:
        app_css = client.get("/static/css/app.css").text
        assert "width: 98vw" in app_css
        assert "height: 96vh" in app_css
        assert "min-height: 80vh" in app_css
        assert ".source-viewport--presentation" in app_css

        src_css = client.get("/static/css/source.css").text
        assert "max-width: min(1240px, 100%)" in src_css
        assert ".source-block--highlighted" in src_css

        viewer_js = client.get("/static/js/source-viewer.js").text
        assert "supportsOriginalPages" in viewer_js
        assert 'state.mediaType === "pdf" || state.mediaType === "docx"' in viewer_js
        assert "source-viewport--presentation" in viewer_js
