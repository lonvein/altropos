"""Only artificial sources; no OCR executables, network, models or publication."""
import json
from pathlib import Path

import pytest

from navigator.ingestion.common import Unit
from navigator.ingestion.model import Parsed
from navigator.review import (
    ReviewError, load_approved_package, prepare_document, record_decision,
    show_package, write_index,
)
from navigator.review import workflow


@pytest.fixture
def package(tmp_path, monkeypatch):
    source = tmp_path / "synthetic.docx"
    source.write_bytes(b"artificial original; supplied Parsed fixture")
    parsed = Parsed(media_type="docx", title="Synthetic <script>",
                    units=[Unit("Artificial <script>alert(1)</script>", {"kind": "docx", "block": 0}),
                           Unit("Synthetic footnote", {"kind": "docx", "block": 1}, kind="note")],
                    blocks=[{"index": 0, "kind": "table", "rows": [["A", "", "C"], ["", "", ""]]}],
                    warnings=["Artificial warning <script>"])
    def forbidden(*args, **kwargs):
        pytest.fail("Supplied Parsed must not parse or OCR again")
    monkeypatch.setattr(workflow, "parse_file", forbidden)
    result = prepare_document(source, tmp_path / "packages", parsed=parsed)
    return Path(result["package_dir"])


def approve(package):
    return record_decision(package, "approved", reviewer="Synthetic test reviewer")


def test_pending_refuses_load(package):
    assert show_package(package)["review"]["status"] == "pending"
    with pytest.raises(ReviewError, match="approval"):
        load_approved_package(package)


def test_approved_exact_snapshot(package):
    approve(package)
    loaded = load_approved_package(package)
    assert loaded["source_original"].read_bytes().startswith(b"artificial original")
    assert loaded["document"]["status"] == "processing"
    assert loaded["document"]["metadata"]["reviewed"] is None
    assert len(loaded["fragments"]) == 2
    assert loaded["fragments"][1].text == "Synthetic footnote"
    assert loaded["fragments"][0].locator.block == 0


@pytest.mark.parametrize("filename", ["snapshot.json", "original.docx", "fragments.json", "index.html", "readable.txt"])
def test_artifact_tamper(package, filename):
    approve(package)
    target = package / filename
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(ReviewError, match="changed"):
        load_approved_package(package)


@pytest.mark.parametrize("field", ["diagnostics", "parser_fingerprint", "document", "blocks_digest", "group"])
def test_manifest_tamper(package, field):
    approve(package)
    path = package / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = "changed"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ReviewError, match="Manifest changed"):
        load_approved_package(package)


def test_current_parser_changed(package, monkeypatch):
    approve(package)
    monkeypatch.setattr(workflow, "parser_fingerprint", lambda: {"changed": True})
    with pytest.raises(ReviewError, match="fingerprint"):
        load_approved_package(package)


@pytest.mark.parametrize("target,field", [
    ("snapshot.json", "diagnostics"), ("snapshot.json", "blocks"),
    ("snapshot.json", "units"), ("snapshot.json", "warnings"),
    ("fragments.json", "text"),
])
def test_semantic_snapshot_tamper(package, target, field):
    approve(package)
    path = package / target
    content = json.loads(path.read_text(encoding="utf-8"))
    if target == "fragments.json":
        content[0][field] = "Changed artificial fragment"
    else:
        content[field] = ["Changed artificial content"]
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ReviewError, match="changed"):
        load_approved_package(package)


def test_preview_error_prevents_approval(tmp_path):
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"artificial invalid PDF")
    parsed = Parsed(media_type="pdf", units=[Unit("Artificial text", {"kind": "pdf", "page": 1})])
    path = Path(prepare_document(source, tmp_path / "review", parsed=parsed)["package_dir"])
    assert show_package(path)["manifest"]["diagnostics"][0]["error"]
    with pytest.raises(ReviewError, match="preview errors"):
        approve(path)


def test_html_escape_blanks_notes_and_index(package):
    content = (package / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in content
    assert "&lt;script&gt;" in content
    assert "<td>A</td><td></td><td>C</td>" in content
    assert "<td></td><td></td><td></td>" in content
    assert "Synthetic footnote" in content
    assert "Content-Security-Policy" in content
    index = write_index(package.parent).read_text(encoding="utf-8")
    assert "pending" in index and f"{package.name}/index.html" in index


@pytest.mark.parametrize("extension", ["docx", "xlsx"])
def test_table_content_visible_once_and_search_context_available_separately(tmp_path, extension):
    from bs4 import BeautifulSoup
    source = tmp_path / ("synthetic." + extension)
    labels = [f"ARTIFICIAL_UNIQUE_ROW_{i:03d}" for i in range(60)]
    headers = ["Synthetic category", "Synthetic detail", "Synthetic amount"]
    if extension == "docx":
        from docx import Document
        doc = Document()
        table = doc.add_table(rows=1, cols=3)
        for cell, text in zip(table.rows[0].cells, headers):
            cell.text = text
        for label in labels:
            cells = table.add_row().cells
            for cell, text in zip(cells, [label, "Artificial description " * 8, "0"]):
                cell.text = text
        doc.save(source)
    else:
        from openpyxl import Workbook
        book = Workbook()
        sheet = book.active
        sheet.append(headers)
        for label in labels:
            sheet.append([label, "Artificial description " * 8, 0])
        book.save(source)
        book.close()
    original_bytes = source.read_bytes()
    info = prepare_document(source, tmp_path / "review")
    package = Path(info["package_dir"])
    soup = BeautifulSoup((package / "index.html").read_text(encoding="utf-8"), "html.parser")
    main = soup.find("main", id="source-content")
    assert len(main.find_all("table")) == 1
    text = main.get_text()
    assert all(text.count(label) == 1 for label in labels)
    technical = soup.find("details", id="technical-units")
    search = soup.find("details", id="search-fragments")
    assert not technical.has_attr("open") and not search.has_attr("open")
    assert not any(label in technical.get_text() for label in labels)
    assert search.find("a", href="fragments.json")
    fragments = json.loads((package / "fragments.json").read_text(encoding="utf-8"))
    assert len(fragments) > 2
    assert all(all(header in f["text"] for header in headers) for f in fragments)
    record_decision(package, "approved", reviewer="Synthetic reviewer")
    loaded = load_approved_package(package)
    assert loaded["source_original"].read_bytes() == original_bytes
    assert [f.model_dump(mode="json") for f in loaded["fragments"]] == fragments


@pytest.mark.parametrize("decision", ["rejected", "excluded"])
def test_negative_decisions(package, decision):
    record_decision(package, decision, reviewer="Synthetic reviewer", comment="Explicit test choice")
    with pytest.raises(ReviewError):
        load_approved_package(package)


def test_reviewer_and_decision_required(package):
    with pytest.raises(ReviewError, match="reviewer"):
        record_decision(package, "approved", reviewer=" ")
    with pytest.raises(ReviewError, match="Decision"):
        record_decision(package, "pending", reviewer="test")


def test_needs_review_cannot_be_approved(tmp_path):
    source = tmp_path / "synthetic.docx"
    source.write_bytes(b"synthetic")
    parsed = Parsed(media_type="docx", needs_review=True,
                    units=[Unit("Artificial partial extraction", {"kind": "docx", "block": 0})])
    package = Path(prepare_document(source, tmp_path / "review", parsed=parsed)["package_dir"])
    with pytest.raises(ReviewError, match="needs_review"):
        approve(package)
    assert show_package(package)["review"]["status"] == "pending"
    record_decision(package, "excluded", reviewer="Synthetic reviewer")


def test_existing_package_not_overwritten(package):
    approve(package)
    with pytest.raises(FileExistsError):
        prepare_document(package / "original.docx", package.parent, parsed=Parsed(media_type="docx"))
    assert load_approved_package(package)


def test_xlsx_sparse_grid(tmp_path):
    source = tmp_path / "synthetic.xlsx"
    source.write_bytes(b"artificial workbook fixture")
    parsed = Parsed(media_type="xlsx", blocks=[{
        "sheet": "Synthetic <sheet>", "rows": [{"row": 2, "cells": [
            {"col": 1, "ref": "A2", "text": "A"},
            {"col": 3, "ref": "C2", "formula": "=1+1", "cached": "2"}]}]}])
    path = Path(prepare_document(source, tmp_path / "review", parsed=parsed)["package_dir"])
    content = (path / "index.html").read_text(encoding="utf-8")
    assert "<tr><td></td><td></td><td></td></tr>" in content
    assert "<td>A</td><td></td><td>2</td>" in content
    with pytest.raises(ReviewError):
        approve(path)  # empty fragments are not publishable


def test_formula_missing_cache_is_readable_without_evaluating_or_replacing_zero(tmp_path):
    from openpyxl import Workbook
    from bs4 import BeautifulSoup
    book = Workbook()
    book.active.append(['Synthetic field', 'Synthetic total', 'Synthetic comment'])
    book.active.append(['Artificial data', '=SUM(Z1:Z2)', 0])
    source = tmp_path / 'synthetic-formulas.xlsx'
    book.save(source)
    book.close()
    package = Path(prepare_document(source, tmp_path / 'review')['package_dir'])
    soup = BeautifulSoup((package / 'index.html').read_text('utf-8'), 'html.parser')
    main = soup.find('main')
    assert 'нет сохранённого результата' in main.get_text()
    assert 'cached:' not in main.get_text() and '=SUM(Z1:Z2)' not in main.get_text()
    assert main.find_all('tr')[1].find_all('td')[2].text == '0'
    snapshot = json.loads((package / 'snapshot.json').read_text('utf-8'))
    formula = snapshot['blocks'][0]['rows'][1]['cells'][1]
    assert formula['formula'] == '=SUM(Z1:Z2)' and formula['cached'] is None
    assert formula.get('evaluated') is None
    assert not any('без сохранённого значения' in w for w in snapshot['warnings'])
    assert any(len(d.get('uncached_formulas', ())) >= 1 for d in snapshot['diagnostics'])


def test_pdf_previews_rotation_side_by_side(tmp_path):
    import pypdfium2 as pdfium
    source = tmp_path / "synthetic.pdf"
    pdf = pdfium.PdfDocument.new()
    try:
        page = pdf.new_page(300, 400)
        page.set_rotation(90)
        page.close()
        pdf.new_page(300, 400).close()
        pdf.save(str(source))
    finally:
        pdf.close()
    parsed = Parsed(media_type="pdf", units=[Unit("Artificial text", {"kind": "pdf", "page": 1})],
                    blocks=[{"page": 1, "text": "Artificial text", "ocr": True}])
    path = Path(prepare_document(source, tmp_path / "review", parsed=parsed)["package_dir"])
    manifest = show_package(path)["manifest"]
    assert len(manifest["diagnostics"]) == 2
    assert manifest["diagnostics"][0]["rotation"] == 90
    assert (path / "page-0001.png").is_file()
    content = (path / "index.html").read_text(encoding="utf-8")
    assert 'class="pair"' in content and "Artificial text" in content
    approve(path)
    (path / "page-0001.png").write_bytes(b"changed preview")
    with pytest.raises(ReviewError):
        load_approved_package(path)


def test_cli_prepare_show_and_explicit_decision(tmp_path, capsys, monkeypatch):
    from scripts.corpus_review import main
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "synthetic.html"
    source.write_text("<html><body><p>Artificial review document for testing.</p></body></html>", encoding="utf-8")
    root = tmp_path / "review"
    main(["prepare", str(source), "--out", str(root)])
    package = next(root.glob("*/review.json")).parent
    main(["show", str(package)])
    with pytest.raises(SystemExit):
        main(["decision", str(package), "--decision", "approved"])
    main(["decision", str(package), "--decision", "approved", "--reviewer", "Synthetic reviewer"])
    assert load_approved_package(package)
