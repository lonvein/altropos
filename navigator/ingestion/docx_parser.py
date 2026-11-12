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
        elif tag == "r" and _manual_marker(node):
            parts.append(f"[надстрочный:{_manual_marker(node)}]")
        elif tag == "t":
            if not _manual_marker(node.getparent()):
                parts.append(node.text or "")
        elif tag == "footnoteReference":
            parts.append(f"[сноска:{node.get(_W+'id')}]")
        elif tag in ("br", "tab"):
            parts.append(" ")
    return clean("".join(parts))


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _manual_marker(run):
    if run is None or run.tag != _W+'r':
        return ""
    align = run.find('./'+_W+'rPr/'+_W+'vertAlign')
    text = ''.join(n.text or '' for n in run.findall(_W+'t')).strip()
    return text if (align is not None and align.get(_W+'val') == 'superscript'
                    and re.fullmatch(r'\d+|[*†‡]+', text)) else ""


def _manual_refs(element):
    return tuple(dict.fromkeys(_manual_marker(r) for r in element.iter(_W+'r') if _manual_marker(r)))


def _refs(element):
    return tuple(dict.fromkeys(n.get(_W+'id') for n in element.iter(_W+'footnoteReference')))


def _table_grid(table):
    # Read the physical grid; python-docx duplicates merged cells and omits grid gaps.
    width = len(table._tbl.tblGrid)
    rows = []
    effective = []
    merges = []
    active = {}
    trs = table._tbl.findall(_W+'tr')
    for row_index, tr in enumerate(trs):
        props = tr.find(_W+'trPr')
        before = props.find(_W+'gridBefore') if props is not None else None
        cells = [""] * (int(before.get(_W+'val', '0')) if before is not None else 0)
        resolved = list(cells)
        next_active = {}
        for tc in tr.findall(_W+'tc'):
            props = tc.find(_W+'tcPr')
            span = props.find(_W+'gridSpan') if props is not None else None
            n = int(span.get(_W+'val', '1')) if span is not None else 1
            col = len(cells)
            text = _element_text(tc).replace("\n", " ")
            merge = props.find(_W+'vMerge') if props is not None else None
            continuation = merge is not None and merge.get(_W+'val', 'continue') == 'continue'
            parent = active.get(col) if continuation else None
            display = text
            if continuation and parent is not None:
                origin_row, origin_col, parent_text = parent
                display = (text+' ' if text else '') + f"[объединено с R{origin_row+1}C{origin_col+1}: {parent_text}]"
                merges.append({"row": row_index, "col": col, "rowspan_from": origin_row,
                               "colspan": n, "text": parent_text})
            elif continuation:
                merges.append({"row": row_index, "col": col, "unresolved": True})
            elif n > 1:
                merges.append({"row": row_index, "col": col, "colspan": n})
            if merge is not None:
                for offset in range(n):
                    next_active[col+offset] = parent or (row_index, col, text)
            cells.extend([text] + [""] * (n-1))
            resolved.extend([display] + [""] * (n-1))
        rows.append(cells + [""] * max(0, width-len(cells)))
        effective.append(resolved + [""] * max(0, width-len(resolved)))
        active = next_active
    return rows, effective, merges, trs


def _table_rows(table) -> list[list[str]]:
    return _table_grid(table)[0]


def _header_count(rows, trs):
    explicit = []
    for i, tr in enumerate(trs):
        header = tr.find('./'+_W+'trPr/'+_W+'tblHeader')
        if header is not None and header.get(_W+'val', '1') not in ('0', 'false', 'off'):
            explicit.append(i)
        else:
            break
    if explicit:
        return len(explicit), 'explicit'
    # Unmarked title/blank/multilevel preamble followed by a complete column
    # label row. Never fill blank data cells by carrying a previous value.
    width = max(map(len, rows), default=0)
    for i, row in enumerate(rows[:8]):
        if width and sum(bool(c.strip()) for c in row) == width:
            return i+1, 'inferred'
    return min(1, len(rows)), 'fallback'


def _table_body(rows, start):
    """Keep preceding sparse labels as quoted context, never as filled cells."""
    context = {}
    result = []
    for index in range(start, len(rows)):
        row = rows[index]
        occupied = [col for col, text in enumerate(row) if text.strip()]
        first = min(occupied, default=len(row))
        preceding = [(r, text) for col, (r, text) in sorted(context.items()) if col < first]
        prefix = '\n'.join(f'[Предшествующая строка R{r+1}; не значение пустых ячеек: {text}]'
                           for r, text in preceding)
        line = ' | '.join(row)
        result.append((prefix+'\n' if prefix else '') + f'R{index+1}: '+line)
        if occupied:
            context = {col: value for col, value in context.items() if col < first}
            if len(occupied) == 1:
                context[first] = (index, line)
    return result


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
    diagnostics: list[dict] = []
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
            manual = _manual_refs(element)
            definition = bool(manual and text.startswith(f'[надстрочный:{manual[0]}]'))
            info = None if definition else outline.feed(
                text, style_heading=style.startswith(("heading", "заголовок", "title")))
            manual_ids = tuple('manual:'+ref for ref in manual)
            if manual:
                warnings.append(f"Блок {index}: ручные надстрочные маркеры; требуется проверка связи с пояснениями")
                diagnostics.append({"block": index, "source": "manual_superscript",
                                    "markers": list(manual), "candidate_definition": definition})
            units.append(Unit(text=text, locator={"kind": "docx", "block": index}, section=info.section if info else '',
                              path=() if definition else info.path, kind='note' if definition else info.kind,
                              atomic=len(text) < 900,
                              note_refs=_refs(element) + (() if definition else manual_ids),
                              note_id=manual_ids[0] if definition else ''))
            blocks[-1]["note_refs"] = list(units[-1].note_refs)
            if definition:
                blocks[-1].update(kind='note', note_id=manual_ids[0])
            referenced.update(_refs(element))
        elif tag == "tbl":
            rows, resolved, merges, trs = _table_grid(Table(element, document))
            if not rows:
                continue
            index = len(blocks)
            header_count, header_source = _header_count(resolved, trs)
            blocks.append({"index": index, "kind": "table", "rows": rows,
                           "merges": merges, "header_rows": header_count,
                           "header_source": header_source})
            header = '\n'.join(' | '.join(r) for r in resolved[:header_count])
            body_rows = _table_body(resolved, header_count) or ['']
            if any(m.get('unresolved') for m in merges):
                warnings.append(f"Таблица {index}: продолжение объединённой ячейки без начала")
            manual = _manual_refs(element)
            if manual:
                warnings.append(f"Таблица {index}: ручные надстрочные маркеры; требуется проверка")
            diagnostics.append({"block": index, "source": "table_structure", "header_rows": header_count,
                                "header_source": header_source, "context": "preceding_rows_not_cell_values"})
            units.append(Unit(text="\n".join(" | ".join(r) for r in rows),
                              locator={"kind": "docx", "block": index}, kind="table", path=(),
                              table_header=header, rows=body_rows,
                              note_refs=_refs(element)+tuple('manual:'+ref for ref in manual)))
            blocks[-1]["note_refs"] = list(units[-1].note_refs)
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

    manual_ids = [u.note_id for u in units if u.note_id.startswith('manual:')]
    duplicates = {ref for ref in manual_ids if manual_ids.count(ref) > 1}
    if duplicates:
        warnings.append("Ручные надстрочные маркеры: неоднозначные повторные пояснения")
        for unit in units:
            if unit.note_id in duplicates:
                unit.note_id = ''  # never silently pick the last candidate

    if not any(u.kind != "heading" for u in units):
        warnings.append("В документе не найдено текста")
    return Parsed(media_type="docx", title=_guess_title(units), units=units, blocks=blocks,
                  warnings=warnings, needs_review=not units or bool(warnings), diagnostics=diagnostics)


def _guess_title(units: list[Unit]) -> str:
    for unit in units[:30]:
        low = unit.text.lower()
        if low.startswith(("о ", "об ", "по ", "регламент", "порядок", "положение", "инструкция")) \
                and len(unit.text) < 300:
            return unit.text
    return ""
