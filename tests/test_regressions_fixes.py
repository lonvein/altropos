"""Regression tests covering PDF/OCR, Excel formulas, DOCX superscripts, async /api/ask, /review Back nav, and RAG fixes."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date
from io import BytesIO
from pathlib import Path

import httpx
import openpyxl
import pytest
from bs4 import BeautifulSoup
from docx import Document as DocxDocument
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from navigator.config import Settings, get_settings
from navigator.db import Store
from navigator.ingestion import parse_file
from navigator.ingestion.common import filter_user_warnings
from navigator.ingestion.ocr import _layout_groups, _point_number_re
from navigator.rag.indexing import Indexer
from navigator.rag.pipeline import AnswerPipeline
from navigator.rag.retrieval import Searcher
from navigator.rag.router import router as ask_router
from navigator.review import prepare_document, write_index
from navigator.schemas import (
    AskRequest,
    Claim,
    Document,
    Draft,
    EmbeddingResponse,
    Evidence,
    Fragment,
    MetadataPatch,
    Relation,
    RerankResponse,
    RerankScore,
    SearchRequest,
    Source,
    Verification,
)


def _make_docx_with_manual_superscripts() -> bytes:
    doc = DocxDocument()
    p1 = doc.add_paragraph("Показатель планирования")
    r1 = p1.add_run("1")
    r1.font.superscript = True
    p1.add_run(" и направление расходов")
    r2 = p1.add_run("2")
    r2.font.superscript = True

    n1 = doc.add_paragraph()
    rn1 = n1.add_run("1")
    rn1.font.superscript = True
    n1.add_run(" Примечание к показателю планирования.")

    n2 = doc.add_paragraph()
    rn2 = n2.add_run("2")
    rn2.font.superscript = True
    n2.add_run(" Примечание к направлению расходов.")

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_single_and_multilevel_point_numbers_and_dashed_bullets():
    ptn = _point_number_re()
    for token in ("1.", "2.", "3.", "6.", "3.7", "10.2.1"):
        assert ptn.fullmatch(token), token

    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    assert draw is not None

    def w(text, x, y, width, height=28, line=1, conf=90):
        return {"text": text, "box": [x, y, x + width, y + height], "line": line, "conf": conf}

    paragraphs = {
        (1, 1): {
            "words": [w("1.]", 80, 80, 40, conf=60), w("Общее", 190, 80, 120), w("положение.", 320, 80, 140)],
            "heights": [28, 28, 28],
        },
        (2, 1): {
            "words": [w("3.7]", 80, 260, 45, conf=60), w("Порядок:", 190, 260, 140)],
            "heights": [28, 28],
        },
        (2, 2): {
            "words": [w("—", 120, 310, 20), w("первый;", 160, 310, 120)],
            "heights": [28, 28],
        },
        (2, 3): {
            "words": [w("—", 120, 360, 20), w("второй.", 160, 360, 120)],
            "heights": [28, 28],
        },
    }
    answers = iter([("0.", 0), ("1.", 91), ("1.", 90), ("3,7", 92), ("3.7", 90), ("3.7", 90)])
    diag = {"needs_review": False, "warnings": []}
    groups = _layout_groups(paragraphs, image, lambda *args: next(answers, ("", 0)), diag)
    assert groups[0]["text"].startswith("1. Общее положение")
    assert any(g["text"].startswith("3.7 ") for g in groups)
    assert any(g.get("list_item") for g in groups)
    assert not diag["needs_review"]
    assert not diag["warnings"]


def test_xlsx_iferror_and_cross_sheet_without_fake_zeros(tmp_path):
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Смета"
    ws1.append(["Статья", "План", "Факт", "Итог", "Ошибка", "Пусто"])
    ws1.append(["Строка 1", 10, 20, "=SUM(B2:C2)", '=IFERROR((E10+#REF!)*B2,"")', "=SUM(Z1:Z5)"])
    ws2 = wb.create_sheet("Свод")
    ws2.append(["Показатель", "Удвоено"])
    ws2.append(["Всего", "=Смета!D2*2"])
    path = tmp_path / "calc.xlsx"
    wb.save(path)

    parsed = parse_file(path)
    assert not parsed.needs_review
    assert parsed.warnings == []

    all_text = "\n".join(u.text for u in parsed.units)
    assert "30" in all_text
    assert "60" in all_text
    assert "=SUM(Z1:Z5)" in all_text and "значение не сохранено" in all_text
    assert "Пусто (F2): 0" not in all_text
    assert sum(d.get("evaluated_formulas", 0) for d in parsed.diagnostics) >= 3
    assert any(len(d.get("uncached_formulas", ())) >= 1 for d in parsed.diagnostics)


def test_docx_unambiguous_manual_markers_and_warning_filtering(tmp_path):
    path = tmp_path / "markers.docx"
    path.write_bytes(_make_docx_with_manual_superscripts())
    parsed = parse_file(path)
    assert not parsed.needs_review
    assert parsed.warnings == []
    assert any(d.get("markers") == ["1", "2"] or d.get("markers") == ["1"] for d in parsed.diagnostics)

    filtered = filter_user_warnings(
        [
            "Значительная часть страницы представлена изображением; требуется OCR всей страницы.",
            "Номер пункта 1. не подтверждён повторным OCR области.",
            "Номер пункта 3.7 уточнён согласованным повторным OCR области.",
            "Числовой токен уточнён согласованным OCR области без ограничения алфавита.",
            "Страницы 1-5 распознаны OCR; текст может содержать ошибки.",
            "Визуально подтверждён оператором по оригиналу, текст не менялся.",
            "Ручные надстрочные маркеры 1 технически сверены с оригиналом.",
            "Формулы без сохранённого значения (308); значения не пересчитывались.",
            "Повреждённая кодировка текста на странице 2.",
            "Обнаружены дублирующиеся ручные надстрочные маркеры: 2.",
        ],
        status="ready",
    )
    assert filtered == [
        "Повреждённая кодировка текста на странице 2.",
        "Обнаружены дублирующиеся ручные надстрочные маркеры: 2.",
    ]

    kept_for_review = filter_user_warnings(
        ["Номер пункта 6. не подтверждён повторным OCR области."],
        status="needs_review",
        needs_review=True,
    )
    assert kept_for_review == ["Номер пункта 6. не подтверждён повторным OCR области."]

    docs_js = (Path(__file__).resolve().parents[1] / "navigator" / "static" / "js" / "documents.js").read_text(encoding="utf-8")
    assert 'doc.status === "ready"' in docs_js
    assert 'fillDetail(fragment, "reviewed", "пройдена")' in docs_js


@pytest.mark.asyncio
async def test_ask_job_resilience_dedup_and_status_recovery(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    class GoodSlowPipeline:
        async def ask(self, request):
            nonlocal call_count
            call_count += 1
            started.set()
            await release.wait()
            from navigator.schemas import AskResponse
            return AskResponse(
                status="answered",
                mode="hybrid",
                answer_mode="generated",
                summary=[Claim(text="КФО — это источник средств.", evidence=[Evidence(fragment_id="f-1", quote="КФО")])],
                sources=[
                    Source(
                        fragment_id="f-1",
                        document_id="doc-1",
                        title="Порядок ФЛС",
                        locator={"kind": "docx", "block": 0},
                        url="/api/sources/f-1",
                    )
                ],
                verification=Verification(supported=True, complete=True, conflict=False),
            )

    app = FastAPI()
    app.state.settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False, request_timeout=5.0)
    app.state.answer_pipeline = GoodSlowPipeline()
    app.include_router(ask_router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        req1 = asyncio.create_task(
            client.post("/api/ask", json={"question": "Что такое КФО?"}, headers={"X-Request-Id": "req-tab-1"})
        )
        await started.wait()

        # Duplicate concurrent request shares the same running job.
        req2 = asyncio.create_task(
            client.post("/api/ask", json={"question": "Что такое КФО?"}, headers={"X-Request-Id": "req-tab-2"})
        )

        active = await client.get("/api/ask/active")
        assert active.status_code == 200
        assert active.json()["active"]["request_id"] == "req-tab-1"
        assert active.json()["active"]["status"] == "running"

        # Cancel client waiter #1 (simulating tab navigation abort) while job finishes on server.
        req1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await req1

        release.set()
        res2 = await req2
        assert res2.status_code == 200
        assert res2.headers.get("X-Request-Id") == "req-tab-1"
        assert call_count == 1

        status_resp = await client.get("/api/ask/status/req-tab-1")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["status"] == "completed"
        assert body["result"]["summary"][0]["text"] == "КФО — это источник средств."


def test_review_back_navigation_present_and_csp_allows_self_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFILE", "development")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("ML_ENABLED", "false")
    monkeypatch.setenv("REVIEW_DIR", str(tmp_path / "review"))
    get_settings.cache_clear()

    from navigator.main import create_app

    app = create_app(synthetic_fallback=True)
    with TestClient(app) as client:
        empty_review = client.get("/review")
        assert empty_review.status_code == 200
        assert "Назад" in empty_review.text
        assert "data-review-back" in empty_review.text
        assert "/static/js/review-nav.js" in empty_review.text
        csp = empty_review.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp

        nav_js = client.get("/static/js/review-nav.js")
        assert nav_js.status_code == 200

        doc_path = tmp_path / "sample.docx"
        d = DocxDocument()
        d.add_paragraph("Искусственный тестовый документ для проверки навигации.")
        d.save(doc_path)

        info = prepare_document(doc_path, tmp_path / "review")
        write_index(tmp_path / "review")

        index_resp = client.get("/review-files/index.html")
        assert index_resp.status_code == 200
        assert "data-review-back" in index_resp.text and "Назад" in index_resp.text
        pkg_name = Path(info["package_dir"]).name
        pkg_resp = client.get(f"/review-files/{pkg_name}/index.html")
        assert pkg_resp.status_code == 200
        soup = BeautifulSoup(pkg_resp.text, "html.parser")
        assert soup.select_one("[data-review-back]") is not None
        assert "Назад" in pkg_resp.text

    get_settings.cache_clear()


def test_rag_kfo_definition_complete_without_spurious_warnings(tmp_path):
    store = Store(tmp_path)
    cfg = Settings(
        _env_file=None,
        data_dir=tmp_path,
        ml_enabled=True,
        llm_enabled=True,
        llm_revision="test-v1",
        reranker_revision="rerank-v1",
        threshold_calibrated=False,
        context_budget=2200,
        top_fragments=4,
    )

    class FakeEmb:
        async def embed(self, req):
            vec = [1.0] + [0.0] * 1023
            return EmbeddingResponse(fingerprint=cfg.fingerprint, vectors=[vec for _ in req.texts])

    class FakeReranker:
        async def rerank(self, req):
            scores = []
            for f in req.fragments:
                score = 0.95 if "источник средств" in f.text else 0.02
                scores.append(RerankScore(fragment_id=f.id, score=score))
            return RerankResponse(model=cfg.reranker_model, revision=cfg.reranker_revision, scores=scores)

    indexer = Indexer(store, cfg, FakeEmb())

    doc_fls = Document(
        id="doc-fls",
        version="v1",
        name="fls.docx",
        title="Об утверждении порядка присвоения номера ФЛС",
        sha256="a" * 64,
        original_path="originals/fls.docx",
        media_type="docx",
        metadata=MetadataPatch(
            title="Об утверждении порядка присвоения номера ФЛС",
            number="700",
            issued=date(2024, 12, 28),
            valid_from=date(2024, 12, 28),
            reviewed=False,
        ),
    )
    f_fls = Fragment(
        id="f-fls-1",
        document_id=doc_fls.id,
        document_version="v1",
        ordinal=0,
        text="Код финансового обеспечения (КФО) — это источник средств, за счёт которых производятся расходы.",
        locator={"kind": "docx", "block": 1},
    )
    store.register_document(doc_fls)
    indexer.index_document(doc_fls.id, [f_fls])

    doc_borrow = Document(
        id="doc-borrow",
        version="v1",
        name="borrow.pdf",
        title="О введении в действие Регламента о внутреннем заимствовании для реализации научных, образовательных и прочих проектов",
        sha256="b" * 64,
        original_path="originals/borrow.pdf",
        media_type="pdf",
        metadata=MetadataPatch(
            title="О введении в действие Регламента о внутреннем заимствовании для реализации научных, образовательных и прочих проектов",
            number="5990-1",
            issued=date(2026, 11, 3),
            valid_from=date(2026, 11, 3),
            reviewed=False,
        ),
    )
    f_borrow = Fragment(
        id="f-borrow-1",
        document_id=doc_borrow.id,
        document_version="v1",
        ordinal=0,
        text="Таблица источников финансового обеспечения проекта: " + ("текст " * 900),
        locator={"kind": "pdf", "page": 1, "bbox": (0.0, 0.0, 100.0, 100.0)},
    )
    store.register_document(doc_borrow)
    indexer.index_document(doc_borrow.id, [f_borrow])

    # Unrelated order with unconfirmed repeal relation — must NOT pollute KFO search.
    doc_other = Document(
        id="doc-other",
        version="v1",
        name="other.docx",
        title="Приказ об иной процедуре",
        sha256="c" * 64,
        original_path="originals/other.docx",
        media_type="docx",
        metadata=MetadataPatch(
            title="Приказ об иной процедуре",
            number="794-1",
            relations=[Relation(target_id="missing-1469-1", kind="repeals", confirmed=False)],
        ),
    )
    f_other = Fragment(
        id="f-other-1",
        document_id=doc_other.id,
        document_version="v1",
        ordinal=0,
        text="Несвязанный приказ о штатном расписании.",
        locator={"kind": "docx", "block": 0},
    )
    store.register_document(doc_other)
    indexer.index_document(doc_other.id, [f_other])

    class FakeLLM:
        @asynccontextmanager
        async def sequence(self):
            yield

        async def generate(self, req):
            return Draft(
                status="answered",
                summary=[
                    Claim(
                        text="Код финансового обеспечения (КФО) — это источник средств, за счёт которых производятся расходы.",
                        evidence=[Evidence(fragment_id="f-fls-1", quote="Код финансового обеспечения (КФО) — это источник средств, за счёт которых производятся расходы.")],
                    )
                ],
            )

        async def verify(self, req):
            return Verification(
                supported=True,
                complete=False,
                conflict=False,
                issues=["Не описана процедура оформления заявки и разрядов ФЛС."],
            )

    searcher = Searcher(store, cfg, FakeEmb(), FakeReranker())
    pipeline = AnswerPipeline(store, cfg, searcher, FakeLLM())
    response = asyncio.run(pipeline.ask(AskRequest(question="Что такое код финансового обеспечения?", as_of=date(2026, 11, 13))))

    assert response.status == "answered"
    assert response.verification.supported is True
    assert response.verification.complete is True
    assert response.warnings == []

    # Procedural query with partial verifier result must still report partial/uncertainty.
    proc_resp = asyncio.run(pipeline.ask(AskRequest(question="Как оформить внутреннее заимствование?", as_of=date(2026, 11, 13))))
    assert proc_resp.status == "partial"
    assert proc_resp.verification.complete is False
    assert any("полнота процедуры" in w for w in proc_resp.warnings)


def test_ocr_and_visual_confirmation_warnings_do_not_cause_failed_or_unconfirmed_status(tmp_path):
    import json
    from navigator.config import Settings
    from navigator.corpus import create_manifest
    from navigator.documents.service import DocumentService
    from navigator.ingestion.common import filter_user_warnings, is_actionable_warning

    # 1. Verify warning filtering handles page-ranged OCR notices and lowercase operator confirmations.
    noisy = [
        "Страницы распознаны OCR: 1–5; текст может содержать ошибки",
        "Страницы распознаны OCR: 2–3; текст может содержать ошибки",
        "Страница 3: номер 3.7 визуально подтверждён оператором по оригиналу, текст не менялся",
    ]
    assert filter_user_warnings(noisy, status="ready") == []
    assert filter_user_warnings(noisy, status="needs_review", needs_review=True) == []
    assert not any(is_actionable_warning(w) for w in noisy)

    real_problem = "Страницы без текста (OCR недоступен): 4; требуется проверка"
    assert filter_user_warnings(noisy + [real_problem], status="needs_review", needs_review=True) == [real_problem]
    assert is_actionable_warning(real_problem)

    # 2. Verify startup status healing on stale DB records and safe reprocessing on initial-corpus docs.
    store = Store(tmp_path)
    cfg = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    indexer = Indexer(store, cfg)

    doc_reg = Document(
        id="doc-reglament",
        version="v1",
        name="reglament.pdf",
        title="Регламент",
        sha256="d" * 64,
        original_path="originals/reglament.pdf",
        media_type="pdf",
        status="queued",
        metadata=MetadataPatch(title="Регламент", reviewed=True),
        warnings=["Страницы распознаны OCR: 1–5; текст может содержать ошибки"],
    )
    f_reg = Fragment(
        id="doc-reglament-f0000",
        document_id=doc_reg.id,
        document_version="v1",
        ordinal=0,
        text="Регламентный текст документа.",
        locator={"kind": "pdf", "page": 1},
    )
    store.register_document(doc_reg)
    indexer.index_document(doc_reg.id, [f_reg])
    # Simulate historical failure leaving failed status + reviewed=True + active_publication.
    store.execute(
        "UPDATE documents SET status='failed', payload=json_set(payload, '$.status', 'failed') WHERE id=?",
        (doc_reg.id,),
    )

    doc_emp = Document(
        id="doc-settlement",
        version="v1",
        name="settlement.pdf",
        title="Регламент поселения сотрудников",
        sha256="e" * 64,
        original_path="originals/settlement.pdf",
        media_type="pdf",
        status="queued",
        metadata=MetadataPatch(title="Регламент поселения сотрудников", reviewed=False),
        warnings=[
            "Страницы распознаны OCR: 2–3; текст может содержать ошибки",
            "Страница 3: номер 3.7 визуально подтверждён оператором по оригиналу, текст не менялся",
        ],
    )
    f_emp = Fragment(
        id="doc-settlement-f0000",
        document_id=doc_emp.id,
        document_version="v1",
        ordinal=0,
        text="Пункт 3.7. Условия поселения сотрудников.",
        locator={"kind": "pdf", "page": 3},
    )
    store.register_document(doc_emp)
    indexer.index_document(doc_emp.id, [f_emp])
    store.execute(
        "UPDATE documents SET status='failed', payload=json_set(payload, '$.status', 'failed') WHERE id=?",
        (doc_emp.id,),
    )

    healed_service = DocumentService(store, cfg, indexer=indexer)
    rec1 = healed_service.get_document(doc_reg.id)
    assert rec1["status"] == "ready"
    assert rec1["warnings"] == []
    assert rec1["metadata"]["reviewed"] is True

    rec2 = healed_service.get_document(doc_emp.id)
    assert rec2["status"] == "ready"
    assert rec2["warnings"] == []
    assert rec2["metadata"]["reviewed"] is True

    # 3. Verify reprocessing / reocr on an already-published or initial-corpus doc never raises version/initial error.
    sources = tmp_path / "src"
    sources.mkdir()
    sample_html = "<html><body><p>Пункт 1. Обязательное положение регламента.</p></body></html>".encode("utf-8")
    (sources / "initial.html").write_bytes(sample_html)
    manifest = tmp_path / "manifest.json"
    create_manifest(sources, [], manifest)

    orig_rel = "originals/init_doc.html"
    (tmp_path / orig_rel).write_bytes(sample_html)
    from navigator.ingestion.common import sha256_bytes

    init_sha = sha256_bytes(sample_html)
    doc_init = Document(
        id="doc-init-corpus",
        version="v1",
        name="initial.html",
        title="Начальный регламент",
        sha256=init_sha,
        original_path=orig_rel,
        media_type="html",
        status="queued",
        metadata=MetadataPatch(title="Начальный регламент", reviewed=False),
        warnings=[],
    )
    f_init = Fragment(
        id="doc-init-corpus-f0000",
        document_id=doc_init.id,
        document_version="v1",
        ordinal=0,
        text="Пункт 1. Обязательное положение регламента.",
        locator={"kind": "html", "block": 0},
    )
    store.register_document(doc_init)
    indexer.index_document(doc_init.id, [f_init])

    cfg_with_manifest = cfg.model_copy(update={"corpus_manifest": manifest})
    svc_manifest = DocumentService(store, cfg_with_manifest, indexer=indexer)
    svc_manifest.reocr_document(doc_init.id, reviewer="Администратор")
    assert svc_manifest.process_next_job()
    after_reocr = svc_manifest.get_document(doc_init.id)
    assert after_reocr["status"] == "ready"
    assert after_reocr["version"] == "v2"
