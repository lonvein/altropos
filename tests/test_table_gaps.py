"""Artificial spreadsheets preserve gaps and anonymized literals, never fill them."""
import openpyxl

from navigator.ingestion.xlsx_parser import parse_xlsx


def test_blank_placeholder_and_merged_cells_remain_distinct(tmp_path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Категория", "Ответственный", "Сумма"])
    sheet.append(["Учебная", None, 0])
    sheet.append(["Вторая", "XXX", "=SUM(C2:C2)"])
    sheet.append(["Объединено", None, None])
    sheet.merge_cells("A4:B4")
    path = tmp_path / "synthetic.xlsx"
    book.save(path)
    parsed = parse_xlsx(path)
    rows = {r["row"]: r["cells"] for r in parsed.blocks[0]["rows"]}
    assert [c["ref"] for c in rows[2]] == ["A2", "B2", "C2"]
    assert rows[2][1]["state"] == "empty" and rows[2][1]["text"] == ""
    assert rows[2][2]["state"] == "value" and rows[2][2]["text"] == "0"
    assert rows[3][1]["state"] == "placeholder" and rows[3][1]["text"] == "XXX"
    assert rows[3][2]["state"] == "formula" and rows[3][2]["cached"] is None
    assert rows[4][1]["state"] == "merged" and rows[4][1]["merged_from"] == "A4"
    text = "\n".join(d.text for d in parsed.build())
    assert "B2): нет значения" in text and "XXX" in text and "Сумма: 0" in text


def test_truncated_sheet_cannot_be_reported_as_complete(tmp_path):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet["A1"] = "ИСКУССТВЕННЫЙ ПРИМЕР"
    sheet["A20001"] = "Дальняя строка"
    path = tmp_path / "synthetic-large.xlsx"
    book.save(path)
    parsed = parse_xlsx(path)
    assert parsed.needs_review
    assert any("20000" in warning and "требуется проверка" in warning for warning in parsed.warnings)
