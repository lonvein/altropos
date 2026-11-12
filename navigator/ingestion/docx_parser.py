"""DOCX: порядок абзацев и таблиц, пункты и заголовки, адресуемые блоки.

Блок = непустой абзац или таблица, нумерация с нуля в порядке документа (locator.block).
Тот же список блоков (`Parsed.blocks`) использует просмотрщик источников, поэтому
индексы в локаторах и в HTML совпадают.
"""
from __future__ import annotations

import re
from pathlib import Path

from navigator.ingestion.common import ParseError, Unit
from navigator.ingestion.model import Parsed
from navigator.ingestion.outline import Outline

_SPACES = re.compile(r"[ \t\r\f\v\u00a0\u2007\u202f]+")


def clean(text: str) -> str:
    text = _SPACES.sub(" ", text.replace("\u200b", ""))
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _table_rows(table) -> list[list[str]]:
    rows = []
    for row in table.rows:
        cells: list[str] = []
        previous = None
        for cell in row.cells:
            if cell._tc is previous:  # объединённая ячейка повторяется в python-docx
                continue
            previous = cell._tc
            cells.append(clean(cell.text).replace("\n", " "))
        if any(cells):
            rows.append(cells)
    return rows


def parse_docx(path: Path) -> Parsed:
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        document = docx.Document(str(path))
    except Exception as exc:  # повреждённый zip, не docx
        raise ParseError("Не удалось прочитать DOCX: файл повреждён или имеет неверный формат") from exc

    units: list[Unit] = []
    blocks: list[dict] = []
    warnings: list[str] = []
    outline = Outline()

    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = clean(paragraph.text)
            if not text:
                continue
            index = len(blocks)
            blocks.append({"index": index, "kind": "paragraph", "text": text})
            style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
            info = outline.feed(text, style_heading=style.startswith(("heading", "заголовок", "title")))
            units.append(Unit(text=text, locator={"kind": "docx", "block": index}, section=info.section,
                              path=info.path, kind=info.kind, atomic=len(text) < 900))
        elif tag == "tbl":
            rows = _table_rows(Table(element, document))
            if not rows:
                continue
            index = len(blocks)
            blocks.append({"index": index, "kind": "table", "rows": rows})
            header = " | ".join(rows[0]) if len(rows) > 1 else ""
            body_rows = [" | ".join(r) for r in (rows[1:] if len(rows) > 1 else rows)]
            units.append(Unit(text="\n".join(" | ".join(r) for r in rows),
                              locator={"kind": "docx", "block": index}, kind="table", path=outline.path,
                              table_header=header, rows=body_rows))

    if not any(u.kind != "heading" for u in units):
        warnings.append("В документе не найдено текста")
    return Parsed(media_type="docx", title=_guess_title(units), units=units, blocks=blocks,
                  warnings=warnings, needs_review=not units)


def _guess_title(units: list[Unit]) -> str:
    for unit in units[:30]:
        low = unit.text.lower()
        if low.startswith(("о ", "об ", "по ", "регламент", "порядок", "положение", "инструкция")) \
                and len(unit.text) < 300:
            return unit.text
    return ""
