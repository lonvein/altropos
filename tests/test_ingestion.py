"""Тесты парсеров и разбиения. Все документы здесь искусственные, собираются на лету."""
from datetime import date
import pytest

from navigator.ingestion import parse_file
from navigator.ingestion.common import (
    MAX_CHARS, ParseError, Unit, build_fragments, document_ids, estimate_tokens, to_fragments)


def fragments_of(parsed, doc="doc-test", version="v1"):
    return to_fragments(doc, version, parsed.build())


# ---------- разбиение ----------

def test_long_paragraph_is_split_with_overlap_under_limit():
    sentence = "Работник обязан предоставить заявление руководителю подразделения заранее. "
    text = sentence * 120
    drafts = build_fragments([Unit(text=text, locator={"kind": "docx", "block": 0}, section="2.1")])
    assert len(drafts) > 1
    assert all(len(d.text) <= MAX_CHARS for d in drafts)
    # перекрытие: конец предыдущего фрагмента повторяется в начале следующего
    assert drafts[0].text[-60:].split()[-3] in drafts[1].text


def test_short_points_stay_whole_and_lead_in_is_not_detached():
    units = [
        Unit(text="Для оформления необходимо представить:", locator={"kind": "docx", "block": 0}, section="3.1"),
        Unit(text="- заявление;", locator={"kind": "docx", "block": 1}),
        Unit(text="- копию договора.", locator={"kind": "docx", "block": 2}),
    ]
    drafts = build_fragments(units)
    assert len(drafts) == 1
    assert "представить:" in drafts[0].text and "копию договора" in drafts[0].text


def test_table_split_repeats_header_and_covers_all_rows():
    rows = [f"Строка {i} | значение {i} " + "х" * 200 for i in range(40)]
    unit = Unit(text="\n".join(rows), locator={"kind": "docx", "block": 3}, kind="table",
                table_header="Название | Сумма", rows=rows)
    drafts = build_fragments([unit])
    assert len(drafts) > 1
    assert all(d.text.startswith("Название | Сумма") for d in drafts)
    joined = "\n".join(d.text for d in drafts)
    assert all(f"Строка {i} |" in joined for i in range(40))


def test_ids_are_stable_for_same_content():
    sha = "ab" * 32
    assert document_ids(sha) == document_ids(sha)
    assert document_ids(sha) != document_ids("cd" * 32)


# ---------- DOCX ----------

@pytest.fixture
def docx_file(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("ПРИКАЗ")
    d.add_paragraph("")
    d.add_paragraph("1. Общие положения")
    d.add_paragraph("1.1.\u00a0\u00a0 Искусственный пример: заявка подаётся до начала события.")
    d.add_paragraph("1.2. Координатор отвечает в течение двух дней.")
    table = d.add_table(rows=3, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Категория", "Лимит"
    table.cell(1, 0).text, table.cell(1, 1).text = "А", "10"
    table.cell(2, 0).text, table.cell(2, 1).text = "Б", "20"
    d.add_paragraph("Приложение № 1")
    d.add_paragraph("Порядок учебный")
    d.add_paragraph("2.1. Пункт приложения.")
    path = tmp_path / "t.docx"
    d.save(path)
    return path


def test_docx_blocks_sections_tables(docx_file):
    parsed = parse_file(docx_file)
    frags = fragments_of(parsed)
    assert not parsed.needs_review
    assert [f.ordinal for f in frags] == list(range(len(frags)))
    assert len({f.id for f in frags}) == len(frags)
    assert all(f.locator.kind == "docx" for f in frags)
    # пустые абзацы не создают блоков: индексы идут подряд
    assert [b["index"] for b in parsed.blocks] == list(range(len(parsed.blocks)))
    text = "\n".join(f.text for f in frags)
    assert "1.1. Искусственный пример" in text  # неразрывные пробелы нормализованы
    table_frag = next(f for f in frags if "Категория | Лимит" in f.text and "Б | 20" in f.text)
    assert table_frag.locator.block == next(b["index"] for b in parsed.blocks if b["kind"] == "table")
    appendix = next(f for f in frags if "Пункт приложения" in f.text)
    assert appendix.text.startswith("Приложение № 1. Порядок учебный")
    assert appendix.section == "2.1"


def test_corrupted_docx_gives_readable_error(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ParseError) as err:
        parse_file(bad)
    assert str(tmp_path) not in str(err.value)


# ---------- XLSX ----------

@pytest.fixture
def xlsx_file(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Демо"
    ws.append(["ИСКУССТВЕННАЯ СМЕТА"])
    ws.append(["Показатель", "Код", "Сумма"])
    ws.append(["Доходы", "100", 500])
    ws.append(["Расходы", "200", "=SUM(C3:C3)"])  # сохранённого значения нет
    path = tmp_path / "t.xlsx"
    wb.save(path)
    return path


def test_xlsx_formula_without_cached_value_is_explicit_not_zero(xlsx_file):
    parsed = parse_file(xlsx_file)
    frags = fragments_of(parsed)
    assert not any("без сохранённого значения" in w for w in parsed.warnings)
    table = next(f for f in frags if "Расходы" in f.text)
    assert "Сумма (C4): 500" in table.text
    import openpyxl, tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Демо"
        ws.append(["ИСКУССТВЕННАЯ СМЕТА"])
        ws.append(["Показатель", "Код", "Сумма"])
        ws.append(["Пустой расчёт", "300", "=SUM(Z1:Z2)"])
        uncomputed = Path(td) / "uncomputed.xlsx"
        wb.save(uncomputed)
        parsed_u = parse_file(uncomputed)
        assert not any("без сохранённого значения" in w for w in parsed_u.warnings)
        assert any(len(d.get("uncached_formulas", ())) >= 1 for d in parsed_u.diagnostics)
        table_u = next(f for f in fragments_of(parsed_u) if "Пустой расчёт" in f.text)
        assert "=SUM(Z1:Z2)" in table_u.text and "значение не сохранено" in table_u.text
        assert "Сумма (C3): 0" not in table_u.text
    assert "Сумма (C4): 0" not in table.text
    assert table.locator.kind == "xlsx" and table.locator.sheet == "Демо"
    assert table.locator.range == "A3:C4"
    assert "Колонки: Показатель | Код | Сумма" in table.text


def test_xlsx_fragment_range_is_real_cells(xlsx_file):
    import re
    for f in fragments_of(parse_file(xlsx_file)):
        assert re.fullmatch(r"[A-Z]+\d+(:[A-Z]+\d+)?", f.locator.range)


def test_corrupted_xlsx_gives_readable_error(tmp_path):
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"garbage")
    with pytest.raises(ParseError):
        parse_file(bad)


# ---------- PDF ----------

def make_pdf(pages: list[str | None]) -> bytes:
    """Минимальный PDF: текст Helvetica (латиница) или пустая страница (имитация скана)."""
    objs = []
    n = len(pages)
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>")
    font_id = 3 + 2 * n
    for i, text in enumerate(pages):
        content_id = 4 + 2 * i
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        if text is None:
            stream = ""
        else:
            lines = text.split("\n")
            stream = "BT /F1 12 Tf 72 760 Td 16 TL " + " ".join(f"({l}) Tj T*" for l in lines) + " ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def test_pdf_pages_start_at_one_and_have_bbox(tmp_path):
    path = tmp_path / "t.pdf"
    path.write_bytes(make_pdf(["1. Synthetic rule: submit the request before the event starts.",
                               "2. Second page rule: the coordinator replies within two days."]))
    parsed = parse_file(path)
    frags = fragments_of(parsed)
    assert not parsed.needs_review
    assert [f.locator.page for f in frags] == [1, 2]
    assert all(f.locator.bbox for f in frags)
    assert frags[0].section == "1"
    assert "submit the request" in frags[0].text


def test_scanned_page_is_reported_not_silently_skipped(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf(["1. Text page with normal content for the extraction layer.", None, None]))
    parsed = parse_file(path)
    assert parsed.needs_review
    assert any("2–3" in w for w in parsed.warnings)


def test_pdf_ocr_hook_is_used_for_pages_without_text(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(make_pdf([None]))
    calls = []

    def fake_ocr(image, number):
        calls.append(number)
        return [{"text": "OCR text of the scanned page", "bbox": [10.0, 10.0, 200.0, 40.0]}]

    parsed = parse_file(path, ocr=fake_ocr)
    assert calls == [1]
    assert not parsed.needs_review
    assert not any("Страницы распознаны OCR" in w for w in parsed.warnings)
    frag = fragments_of(parsed)[0]
    assert frag.locator.page == 1 and frag.locator.bbox == (10.0, 10.0, 200.0, 40.0)


def test_corrupted_pdf_gives_readable_error(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 broken")
    with pytest.raises(ParseError):
        parse_file(bad)


def test_token_estimate_is_positive():
    assert estimate_tokens("а") >= 1


# ---------- HTML & Metadata ----------

def test_html_parsing_and_locators(tmp_path):
    html_content = """<!DOCTYPE html>
<html>
<head><title>Регламент работы сервиса МФТИ</title></head>
<body>
    <nav><a href="/home">Главная</a></nav>
    <main>
        <h1>1. Общие положения</h1>
        <p>1.1. Настоящий регламент регулирует порядок подачи заявок.</p>
        <p>1.2. Дополнительные бланки можно скачать по ссылкам ниже:</p>
        <p><a href="/files/form1.pdf">Скачать форму 1 (PDF)</a></p>
        <p><a href="https://mipt.ru/docs/table.xlsx">Бланк сметы (XLSX)</a></p>
        <table>
            <tr><th>Услуга</th><th>Срок</th></tr>
            <tr><td>Консультация</td><td>3 дня</td></tr>
        </table>
    </main>
    <footer>Подвал сайта</footer>
</body>
</html>"""
    path = tmp_path / "page.html"
    path.write_text(html_content, encoding="utf-8")

    parsed = parse_file(path, base_url="https://mipt.ru")
    frags = fragments_of(parsed)

    assert parsed.media_type == "html"
    assert "Регламент" in parsed.title
    assert not parsed.needs_review
    assert all(f.locator.kind == "html" for f in frags)
    assert all(isinstance(f.locator.block, int) for f in frags)
    assert len(parsed.links) == 2
    assert any(link["url"].endswith("form1.pdf") for link in parsed.links)
    assert any(link["url"].endswith("table.xlsx") for link in parsed.links)
    # Проверяем, что навигация и подвал не попали в блоки
    all_texts = " ".join(f.text for f in frags)
    assert "Главная" not in all_texts
    assert "Подвал сайта" not in all_texts


def test_metadata_extraction_order_and_relations():
    from navigator.ingestion.common import Unit
    from navigator.ingestion.metadata_extractor import extract_metadata

    units = [
        Unit(text="ПРИКАЗ от 28.03.2023 № 794-1", locator={"kind": "docx", "block": 0}),
        Unit(text="О внесении изменений в приказ от 19.09.2019г. № 1469-1", locator={"kind": "docx", "block": 1}),
        Unit(text="1. Отменить действие приказа от 19.09.2019г. № 1469-1 «О введении новых бланков».", locator={"kind": "docx", "block": 2}, section="1"),
        Unit(text="2. Утвердить новый Регламент согласно Приложению № 1.", locator={"kind": "docx", "block": 3}, section="2"),
    ]
    meta = extract_metadata(units, filename="Приказ N 794-1 от 28.03.2023.docx")

    assert meta.number == "794-1"
    assert meta.issued == date(2023, 3, 28)
    assert meta.valid_from == date(2023, 3, 28)
    assert meta.reviewed is False
    assert meta.relations is not None
    kinds = {r.kind for r in meta.relations}
    targets = {r.target_id for r in meta.relations}
    assert "amends" in kinds
    assert "repeals" in kinds
    assert "1469-1" in targets
    assert all(r.confirmed is False for r in meta.relations)
