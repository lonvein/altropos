"""Escaped canonical content views; search chunks are citation evidence only."""
import json
from html import escape

from navigator.schemas import Fragment


SOURCE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def _text(value):
    return escape("" if value is None else str(value))


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


def _block(block):
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
    return f'<{tag} class="source-text">{_text(block.get("text", ""))}</{tag}>'


def render_publication(title: str, document_id: str, fragments: list[Fragment], selected: str | None = None,
                       *, blocks: list[dict]) -> str:
    """Preserve every published fragment, including exact evidence and locators.

    Canonical blocks preserve document order without chunk overlaps, injected
    headers, or filtering. Exact search evidence appears separately when selected.
    """
    sections = []
    for fragment in fragments:
        if fragment.id != selected:
            continue
        locator = escape(json.dumps(fragment.locator.model_dump(mode="json"), ensure_ascii=False))
        selected_class = " source-fragment--selected" if fragment.id == selected else ""
        loc = fragment.locator.model_dump(mode="json")
        label = {"pdf": f"Страница {loc.get('page')}",
                 "docx": f"Блок {loc.get('block', loc.get('paragraph', ''))}",
                 "xlsx": f"Лист {loc.get('sheet', '')}, ячейки {loc.get('range', '')}",
                 "html": f"Блок {loc.get('block', '')}"}.get(loc["kind"], "Адрес источника")
        sections.append(
            f'<section class="source-fragment{selected_class}" id="fragment-{escape(fragment.id)}">'
            f'<p class="source-locator">Фрагмент {fragment.ordinal + 1} · {escape(label)}</p>'
            f'<details><summary>Точный адрес источника</summary><pre>{locator}</pre></details>'
            f'<pre><mark>{escape(fragment.text)}</mark></pre></section>')
    canonical = []
    page = None
    for i, block in enumerate(blocks):
        if block.get("page") is not None and block["page"] != page:
            page = block["page"]
            canonical.append(f'<h2>Страница {_text(page)}</h2>')
        if "sheet" in block:
            canonical.append(f'<h2>Лист «{_text(block["sheet"])}»</h2>')
        canonical.append(f'<section class="source-block" id="block-{i}">{_block(block)}</section>')
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)}</title><link rel="stylesheet" href="/static/css/source.css"></head>'
        f'<body><main class="source-page"><h1>{escape(title)}</h1>'
        '<nav class="source-navigation">'
        f'<a href="/api/documents/{escape(document_id)}/original">Скачать оригинал</a> · '
        '<a href="/">Задать вопрос</a></nav>'
        + (f'<p><a href="#fragment-{escape(selected)}">Перейти к цитируемому фрагменту</a></p>' if selected else '') +
        '<p class="source-note">Сохранённый снимок распознавания опубликованной версии. '
        'Абзацы, таблицы и порядок блоков сохранены. Полноту и точность распознавания '
        'сверяйте с оригиналом.</p>'
        + ''.join(sections) + '<article class="source-document">' + ''.join(canonical) + '</article></main></body></html>')
