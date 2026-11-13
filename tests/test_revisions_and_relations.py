"""Тесты извлечения связей редакций, разрешения по номеру приказа, фильтрации as_of и аудита (ИСКУССТВЕННЫЙ ПРИМЕР)."""
from __future__ import annotations

import io
from datetime import date

import docx
import openpyxl
import pytest

from navigator.config import Settings
from navigator.db import Store
from navigator.documents import DocumentService
from navigator.ingestion.common import Unit
from navigator.ingestion.metadata_extractor import extract_metadata
from navigator.rag.retrieval import Searcher
from navigator.schemas import MetadataPatch, Relation, SearchRequest
from scripts.audit_corpus import audit_directory, render_markdown_report


def _make_docx_bytes(lines: list[str]) -> bytes:
    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


@pytest.fixture
def store_and_service(tmp_path):
    root = tmp_path / "runtime"
    store = Store(root)
    settings = Settings(data_dir=root, profile="development", ml_enabled=False, llm_enabled=False)
    service = DocumentService(store=store, settings=settings)
    return store, service, settings


def test_extract_metadata_with_target_section():
    units = [
        Unit(text="ПРИКАЗ от 15.05.2026 № 794-1", locator={"kind": "docx", "block": 0}),
        Unit(
            text="1. Внести изменения в пункт 2.1 приказа от 19.09.2019г. № 1469-1.",
            locator={"kind": "docx", "block": 1},
            section="1",
        ),
        Unit(
            text="2. Признать утратившим силу раздел 3 приказа от 01.02.2020 № 100-1.",
            locator={"kind": "docx", "block": 2},
            section="2",
        ),
    ]
    meta = extract_metadata(units, filename="order_794.docx")
    assert meta.number == "794-1"
    assert meta.relations is not None
    by_kind = {rel.kind: rel for rel in meta.relations}
    assert by_kind["amends"].target_id == "1469-1"
    assert by_kind["amends"].target_section == "2.1"
    assert by_kind["amends"].confirmed is False
    assert by_kind["repeals"].target_id == "100-1"
    assert by_kind["repeals"].target_section == "3"
    assert by_kind["repeals"].confirmed is False


def test_relation_target_id_resolved_by_order_number_both_directions(store_and_service):
    _, service, _ = store_and_service

    # Направление А: базовый приказ загружен ДО изменяющего приказа
    base_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.01.2026 № 100-1",
        "ИСКУССТВЕННЫЙ ПРИМЕР: Базовый порядок командировок",
        "1.1. Заявка подаётся за 5 дней.",
    ])
    amend_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.03.2026 № 200-1",
        "О внесении изменений",
        "1. Внести изменения в пункт 1.1 приказа № 100-1.",
    ])
    base_id = service.upload_file("order_100.docx", base_bytes)["document_id"]
    amend_id = service.upload_file("order_200.docx", amend_bytes)["document_id"]
    service.run_all_jobs()

    amend_doc = service.get_document(amend_id)
    assert amend_doc["metadata"]["relations"][0]["target_id"] == base_id
    assert amend_doc["metadata"]["relations"][0]["target_section"] == "1.1"
    assert amend_doc["metadata"]["relations"][0]["confirmed"] is False

    # Направление Б: отменяющий приказ загружен РАНЬШЕ целевого приказа № 299-1
    repeal_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.06.2026 № 300-1",
        "Об отмене старых правил",
        "1. Отменить действие приказа № 299-1 полностью.",
    ])
    repeal_id = service.upload_file("order_300.docx", repeal_bytes)["document_id"]
    service.run_all_jobs()
    assert service.get_document(repeal_id)["metadata"]["relations"][0]["target_id"] == "299-1"

    old_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.01.2025 № 299-1",
        "ИСКУССТВЕННЫЙ ПРИМЕР: Старые правила пропуска",
        "1. Пропуск выдаётся по бумажному заявлению.",
    ])
    old_id = service.upload_file("order_299.docx", old_bytes)["document_id"]
    service.run_all_jobs()

    repeal_after = service.get_document(repeal_id)
    assert repeal_after["metadata"]["relations"][0]["target_id"] == old_id


@pytest.mark.asyncio
async def test_unconfirmed_repeal_does_not_hide_document_in_search(store_and_service):
    store, service, settings = store_and_service
    old_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.01.2026 № 401-1",
        "ИСКУССТВЕННЫЙ ПРИМЕР: Старый тариф проезда",
        "1.1. Уникальныйтерминбазовый составляет 500 единиц.",
    ])
    new_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.06.2026 № 402-1",
        "Об отмене тарифа",
        "1. Отменить действие приказа № 401-1.",
        "1.1. Уникальныйтерминновый составляет 900 единиц.",
    ])
    old_id = service.upload_file("old_401.docx", old_bytes)["document_id"]
    new_id = service.upload_file("new_402.docx", new_bytes)["document_id"]
    service.run_all_jobs()

    # Связь repeals пока неподтверждена (confirmed=False): старый документ НЕ скрыт
    new_doc = service.get_document(new_id)
    assert new_doc["metadata"]["relations"][0]["target_id"] == old_id
    assert new_doc["metadata"]["relations"][0]["confirmed"] is False

    searcher = Searcher(store=store, settings=settings)
    res = await searcher.search(SearchRequest(question="Уникальныйтерминбазовый", as_of=date(2026, 7, 1)))
    assert any(hit.fragment.document_id == old_id for hit in res.hits)
    assert any("неподтверждённые связи редакций" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_confirmed_repeal_and_as_of_filtering_end_to_end(store_and_service):
    store, service, settings = store_and_service
    old_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.01.2026 № 501-1",
        "ИСКУССТВЕННЫЙ ПРИМЕР: Старый порядок отчётности",
        "1.1. Старыймаркерсдачи направляется факсом.",
    ])
    new_bytes = _make_docx_bytes([
        "ПРИКАЗ от 01.06.2026 № 502-1",
        "ИСКУССТВЕННЫЙ ПРИМЕР: Новый порядок отчётности",
        "1. Отменить действие приказа № 501-1.",
        "1.1. Новыймаркерсдачи направляется через портал.",
    ])
    old_id = service.upload_file("old_501.docx", old_bytes)["document_id"]
    new_id = service.upload_file("new_502.docx", new_bytes)["document_id"]
    service.run_all_jobs()

    # Администратор подтверждает даты и связь отмены с 1 июня 2026 года
    service.patch_metadata(
        old_id,
        MetadataPatch(reviewed=True, valid_from=date(2026, 1, 1)),
    )
    service.patch_metadata(
        new_id,
        MetadataPatch(
            reviewed=True,
            valid_from=date(2026, 6, 1),
            relations=[Relation(target_id="501-1", kind="repeals", confirmed=True)],
        ),
    )

    searcher = Searcher(store=store, settings=settings)

    # До вступления отмены в силу (1 мая 2026) старый порядок действует
    before = await searcher.search(SearchRequest(question="Старыймаркерсдачи", as_of=date(2026, 5, 1)))
    assert any(hit.fragment.document_id == old_id for hit in before.hits)

    # После вступления отмены в силу (15 июня 2026) отменённый документ исключён из выдачи
    after = await searcher.search(SearchRequest(question="Старыймаркерсдачи", as_of=date(2026, 6, 15)))
    assert not any(hit.fragment.document_id == old_id for hit in after.hits)

    after_new = await searcher.search(SearchRequest(question="Новыймаркерсдачи", as_of=date(2026, 6, 15)))
    assert any(hit.fragment.document_id == new_id for hit in after_new.hits)


def test_audit_corpus_script_on_synthetic_dir(tmp_path):
    corpus_dir = tmp_path / "synthetic_docs"
    corpus_dir.mkdir()

    (corpus_dir / "sample.docx").write_bytes(
        _make_docx_bytes(["ПРИКАЗ от 01.02.2026 № 10-1", "ИСКУССТВЕННЫЙ ПРИМЕР. Текст для аудита."])
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Смета"
    ws.append(["Статья", "Сумма"])
    ws.append(["ИСКУССТВЕННЫЙ ПРИМЕР", 100])
    wb.save(corpus_dir / "sample.xlsx")

    (corpus_dir / "broken.docx").write_bytes(b"corrupted-zip")

    report = audit_directory(corpus_dir, ocr_enabled=False)
    assert report["total_files"] == 3
    assert report["counts"]["ready_candidate"] == 2
    assert report["counts"]["error"] == 1
    md = render_markdown_report(report)
    assert "sample.docx" in md
    assert "sample.xlsx" in md
    assert "broken.docx" in md
