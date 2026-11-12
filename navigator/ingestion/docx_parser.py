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


def _element_text(element) -> str:
    parts = []
    for node in element.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "p" and parts:
            parts.append("\n")
        elif tag == "t":
            parts.append(node.text or "")
        elif tag == "footnoteReference":
            parts.append(f"[сноска:{node.get(_W+'id')}]")
        elif tag in ("br", "tab"):
            parts.append(" ")
    return clean("".join(parts))


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _refs(element):
    return tuple(dict.fromkeys(n.get(_W+'id') for n in element.iter(_W+'footnoteReference')))


def _table_rows(table) -> list[list[str]]:
    # Read the physical grid; python-docx duplicates merged cells and omits grid gaps.
    width = len(table._tbl.tblGrid)
    rows = []
    for tr in table._tbl.findall(_W+'tr'):
        props = tr.find(_W+'trPr')
        before = props.find(_W+'gridBefore') if props is not None else None
        cells = [""] * (int(before.get(_W+'val', '0')) if before is not None else 0)
        for tc in tr.findall(_W+'tc'):
            props = tc.find(_W+'tcPr')
            span = props.find(_W+'gridSpan') if props is not None else None
            n = int(span.get(_W+'val', '1')) if span is not None else 1
            cells.extend([_element_text(tc).replace("\n", " ")] + [""] * (n-1))
        rows.append(cells + [""] * max(0, width-len(cells)))
    return rows


def _footnotes(path):
    from zipfile import ZipFile
    from lxml import etree
    with ZipFile(path) as archive:
        if 'word/footnotes.xml' not in archive.namelist():
            return {}
        root = etree.fromstring(archive.read('word/footnotes.xml'),
                                parser=etree.XMLParser(resolve_entities=False, no_network=True))
    return {n.get(_W+'id'): _element_text(n) for n in root.findall(_W+'footnote')
            if n.get(_W+'type', 'normal') == 'normal'}


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
    try:
        notes = _footnotes(path)
    except Exception:
        notes = {}
        warnings.append("Не удалось прочитать footnotes.xml; требуется проверка сносок")
    referenced = set()

    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = _element_text(element)
            if not text:
                continue
            index = len(blocks)
            blocks.append({"index": index, "kind": "paragraph", "text": text})
            style = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
            info = outline.feed(text, style_heading=style.startswith(("heading", "заголовок", "title")))
            units.append(Unit(text=text, locator={"kind": "docx", "block": index}, section=info.section,
                              path=info.path, kind=info.kind, atomic=len(text) < 900, note_refs=_refs(element)))
            blocks[-1]["note_refs"] = list(_refs(element))
            referenced.update(_refs(element))
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
                              table_header=header, rows=body_rows, note_refs=_refs(element)))
            blocks[-1]["note_refs"] = list(_refs(element))
            referenced.update(_refs(element))

    for note_id, text in notes.items():
        index = len(blocks)
        marked = f"[сноска:{note_id}] {text}"
        blocks.append({"index": index, "kind": "note", "text": marked, "note_id": note_id})
        units.append(Unit(text=marked, locator={"kind": "docx", "block": index},
                          kind="note", atomic=True, note_id=note_id))
        if note_id not in referenced:
            warnings.append(f"Сноска {note_id}: связь с текстом не найдена")
    for note_id in sorted(referenced - notes.keys()):
        warnings.append(f"Сноска {note_id}: текст сноски не найден")

    if not any(u.kind != "heading" for u in units):
        warnings.append("В документе не найдено текста")
    return Parsed(media_type="docx", title=_guess_title(units), units=units, blocks=blocks,
                  warnings=warnings, needs_review=not units or bool(warnings))


def _guess_title(units: list[Unit]) -> str:
    for unit in units[:30]:
        low = unit.text.lower()
        if low.startswith(("о ", "об ", "по ", "регламент", "порядок", "положение", "инструкция")) \
                and len(unit.text) < 300:
            return unit.text
    return ""
