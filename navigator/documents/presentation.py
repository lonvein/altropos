"""Escaped canonical content views; search chunks are citation evidence only."""
import json
import re
from html import escape

from navigator.schemas import Fragment

SOURCE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}

_SPACE_RE = re.compile(r"\s+")


def _text(value):
    return escape("" if value is None else str(value))


def _block_matches_fragment(block: dict, index: int, selected_frag: Fragment | None) -> bool:
    if selected_frag is None:
        return False
    loc = selected_frag.locator.model_dump(mode="json")
    kind = loc.get("kind")
    if kind in {"docx", "html"} and loc.get("block") is not None:
        blk_idx = block.get("index", index)
        if blk_idx == loc.get("block"):
            return True
    elif kind == "xlsx" and loc.get("sheet") and block.get("sheet") == loc.get("sheet"):
        return True

    # Fallback: text overlap check scoped to page when PDF.
    if kind == "pdf" and loc.get("page") is not None and block.get("page") != loc.get("page"):
        return False
    blk_text = str(block.get("text") or "").strip()
    if not blk_text:
        rows = block.get("table", block.get("rows"))
        if rows:
            blk_text = " ".join(str(c) for r in rows for c in (r if isinstance(r, list) else []))
    if not blk_text:
        return False
    norm_blk = _SPACE_RE.sub(" ", blk_text).strip().casefold()
    frag_body = selected_frag.text.splitlines()
    if len(frag_body) > 1 and " > " in frag_body[0]:
        frag_body = frag_body[1:]
    norm_frag = _SPACE_RE.sub(" ", " ".join(frag_body)).strip().casefold()
    if len(norm_blk) >= 12 and norm_blk in norm_frag:
        return True
    if len(norm_frag) >= 12 and norm_frag in norm_blk:
        return True
    return False


def _highlighted_text(value: str, selected_frag: Fragment | None) -> str:
    raw = "" if value is None else str(value)
    if selected_frag is None or not raw.strip():
        return escape(raw)
    frag_lines = [ln.strip() for ln in selected_frag.text.splitlines() if ln.strip()]
    if frag_lines and " > " in frag_lines[0] and len(frag_lines) > 1:
        frag_lines = frag_lines[1:]
    for line in sorted(frag_lines, key=len, reverse=True):
        if len(line) < 12:
            continue
        pattern = re.compile(re.escape(line), re.IGNORECASE)
        matches = list(pattern.finditer(raw))
        if matches:
            parts: list[str] = []
            cursor = 0
            for m in matches:
                if m.start() > cursor:
                    parts.append(escape(raw[cursor:m.start()]))
                parts.append(f'<mark class="source-inline-mark">{escape(m.group(0))}</mark>')
                cursor = m.end()
            if cursor < len(raw):
                parts.append(escape(raw[cursor:]))
            return "".join(parts)
    return escape(raw)


def _table(rows, merges=()):
    spans, covered = {}, set()
    for merge in merges:
        r, c = merge["row"], merge["col"]
        width = merge.get("colspan", 1)
        if "rowspan_from" in merge:
            origin = merge["rowspan_from"]
            height, old_width = spans.get((origin, c), (1, width))
            spans[(origin, c)] = (max(height, r - origin + 1), old_width)
            covered.update((r, col) for col in range(c, c + width))
        else:
            height, old_width = spans.get((r, c), (1, 1))
            spans[(r, c)] = (height, max(old_width, width))
        covered.update((r, col) for col in range(c + 1, c + width))
    rendered = []
    for r, row in enumerate(rows):
        cells = []
        for c, value in enumerate(row):
            if (r, c) in covered:
                continue
            height, width = spans.get((r, c), (1, 1))
            cells.append(f'<td rowspan="{height}" colspan="{width}">{_text(value)}</td>')
        rendered.append('<tr>' + ''.join(cells) + '</tr>')
    return '<div class="source-table-wrap"><table>' + ''.join(rendered) + '</table></div>'


def _block(block, selected_frag: Fragment | None = None, *, is_hit: bool = False):
    if "sheet" in block:
        from openpyxl.utils.cell import range_boundaries
        cells = {(row["row"], c["col"]): c for row in block.get("rows", []) for c in row["cells"]}
        width = max((col for _, col in cells), default=0)
        height = max((row for row, _ in cells), default=0)
        bounds_list = [range_boundaries(rng) for rng in block.get("merged_ranges", [])]
        width = max([width] + [bounds[2] for bounds in bounds_list])
        height = max([height] + [bounds[3] for bounds in bounds_list])
        def value(cell):
            if cell.get("formula") is not None:
                if cell.get("cached") is not None:
                    return cell.get("cached")
                if cell.get("evaluated") is not None:
                    return cell.get("evaluated")
                return "нет сохранённого результата (не пересчитывалось)"
            return cell.get("text")
        if width * height > 200_000:
            return ('<p>Разреженный лист: ячейки с точными адресами; пропущенные позиции пусты.</p>' +
                    _table([[c.get("ref", f"R{r}C{col}"), value(c)] for (r, col), c in cells.items()]))
        rows = [[value(cells.get((r, c), {})) for c in range(1, width + 1)] for r in range(1, height + 1)]
        merges = []
        for c1, r1, c2, r2 in bounds_list:
            if r2 <= height and c2 <= width:
                merges.append({"row": r1-1, "col": c1-1, "colspan": c2-c1+1})
                merges.extend({"row": r-1, "col": c1-1, "colspan": c2-c1+1, "rowspan_from": r1-1}
                              for r in range(r1+1, r2+1))
        return _table(rows, merges)
    rows = block.get("table", block.get("rows"))
    if rows is not None:
        return _table(rows, block.get("merges", []))
    tag = "h3" if block.get("kind") == "heading" else "p"
    raw = block.get("text", "")
    rendered_text = _highlighted_text(raw, selected_frag) if is_hit else _text(raw)
    return f'<{tag} class="source-text">{rendered_text}</{tag}>'


def render_publication(title: str, document_id: str, fragments: list[Fragment], selected: str | None = None,
                       *, blocks: list[dict]) -> str:
    """Preserve every published fragment, including exact evidence and locators.

    Canonical blocks preserve document order without chunk overlaps, injected
    headers, or filtering. Exact search evidence appears separately when selected.
    """
    selected_frag = next((f for f in fragments if f.id == selected), None) if selected else None

    canonical = []
    page = None
    first_hit_block_id: int | None = None
    for i, block in enumerate(blocks):
        if block.get("page") is not None and block["page"] != page:
            page = block["page"]
            canonical.append(f'<h2>Страница {_text(page)}</h2>')
        if "sheet" in block:
            canonical.append(f'<h2>Лист «{_text(block["sheet"])}»</h2>')
        is_hit = _block_matches_fragment(block, i, selected_frag)
        if is_hit and first_hit_block_id is None:
            first_hit_block_id = i
        hit_class = " source-block--highlighted" if is_hit else ""
        canonical.append(
            f'<section class="source-block{hit_class}" id="block-{i}">'
            f'{_block(block, selected_frag, is_hit=is_hit)}</section>'
        )

    sections = []
    if selected_frag is not None:
        fragment = selected_frag
        locator = escape(json.dumps(fragment.locator.model_dump(mode="json"), ensure_ascii=False))
        loc = fragment.locator.model_dump(mode="json")
        label = {"pdf": f"Страница {loc.get('page')}",
                 "docx": f"Блок {loc.get('block', loc.get('paragraph', ''))}",
                 "xlsx": f"Лист {loc.get('sheet', '')}, ячейки {loc.get('range', '')}",
                 "html": f"Блок {loc.get('block', '')}"}.get(loc["kind"], "Адрес источника")
        jump_html = (
            f' · <a href="#block-{first_hit_block_id}">Перейти к месту в тексте документа</a>'
            if first_hit_block_id is not None
            else ""
        )
        sections.append(
            f'<section class="source-fragment source-fragment--selected" id="fragment-{escape(fragment.id)}">'
            f'<p class="source-locator">Фрагмент {fragment.ordinal + 1} · {escape(label)}{jump_html}</p>'
            f'<pre><mark>{escape(fragment.text)}</mark></pre>'
            f'<details><summary>Точный адрес источника</summary><pre>{locator}</pre></details></section>'
        )

    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)}</title><link rel="stylesheet" href="/static/css/source.css"></head>'
        f'<body><main class="source-page"><h1>{escape(title)}</h1>'
        '<nav class="source-navigation">'
        f'<a href="/api/documents/{escape(document_id)}/original">Скачать оригинал</a> · '
        '<a href="/">Задать вопрос</a>'
        + (f' · <a href="#fragment-{escape(selected)}">К цитируемому фрагменту</a>' if selected else '')
        + '</nav>'
        '<p class="source-note">Сохранённый снимок распознавания опубликованной версии. '
        'Абзацы, таблицы и порядок блоков сохранены. Полноту и точность распознавания '
        'сверяйте с оригиналом.</p>'
        + ''.join(sections) + '<article class="source-document">' + ''.join(canonical) + '</article></main></body></html>')
