"""Безопасный HTML-просмотрщик опубликованных фрагментов источника."""
from __future__ import annotations

import json
from html import escape
from typing import Any

from navigator.documents.presentation import _inline_source_css_tag
from navigator.schemas import Fragment, MetadataPatch


def format_locator_human(locator: dict[str, Any]) -> str:
    """Форматирует адрес фрагмента (locator) в человекочитаемый вид."""
    kind = locator.get("kind")
    if kind == "pdf":
        page = locator.get("page")
        base = f"PDF, стр. {page}" if page else "PDF"
        bbox = locator.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            x0, y0, x1, y1 = (float(v) for v in bbox)
            return f"{base}, область [{x0:.0f}, {y0:.0f}, {x1:.0f}, {y1:.0f}]"
        return base
    if kind == "docx":
        block = int(locator.get("block", 0))
        return f"DOCX, блок {block + 1}"
    if kind == "xlsx":
        sheet = locator.get("sheet", "")
        cell_range = locator.get("range", "")
        return f"XLSX, лист «{sheet}», диапазон {cell_range}"
    if kind == "html":
        block = int(locator.get("block", 0))
        return f"HTML, блок {block + 1}"
    return "Источник"


def _extract_meta_dict(metadata: MetadataPatch | dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {}
    if isinstance(metadata, MetadataPatch):
        return metadata.model_dump(mode="json")
    if isinstance(metadata, dict):
        return metadata
    return {}


def render_source_html(
    title: str,
    target_fragment: Fragment,
    all_fragments: list[Fragment],
    metadata: MetadataPatch | dict[str, Any] | None = None,
) -> str:
    """Рендерит безопасную HTML-страницу опубликованного документа с подсветкой фрагмента."""
    safe_title = escape(title or "Документ")
    locator_dict = target_fragment.locator.model_dump(mode="json")
    human_locator = escape(format_locator_human(locator_dict))
    locator_json = escape(json.dumps(locator_dict, ensure_ascii=False))

    ordered = sorted(all_fragments or [target_fragment], key=lambda item: item.ordinal)
    if not any(item.id == target_fragment.id for item in ordered):
        ordered.append(target_fragment)

    section_html = ""
    if target_fragment.section.strip():
        parts = [p.strip() for p in target_fragment.section.split("/") if p.strip()]
        breadcrumb = " / ".join(parts) if parts else target_fragment.section.strip()
        section_html = f'<p class="source-locator">Раздел: {escape(breadcrumb)}</p>\n'

    meta = _extract_meta_dict(metadata)
    has_dates = bool(meta.get("valid_from") or meta.get("issued"))
    if has_dates:
        meta_parts: list[str] = []
        if meta.get("number"):
            meta_parts.append(f"№ {meta['number']}")
        if meta.get("issued"):
            meta_parts.append(f"издан {meta['issued']}")
        if meta.get("valid_from"):
            meta_parts.append(f"действует с {meta['valid_from']}")
        if meta.get("valid_to"):
            meta_parts.append(f"по {meta['valid_to']}")
        if meta.get("reviewed") is False or meta.get("reviewed") is None:
            meta_parts.append("метаданные не подтверждены администратором")
        note_html = f'<p class="source-note">Редакция: {escape(", ".join(meta_parts))}.</p>\n'
    else:
        note_html = (
            '<p class="source-note">'
            "Дата вступления в силу или редакция не указаны в метаданных документа."
            "</p>\n"
        )

    blocks_html: list[str] = []
    last_section = None
    for frag in ordered:
        frag_id = escape(frag.id, quote=True)
        sec = frag.section.strip()
        if sec and sec != last_section:
            blocks_html.append(f'<p class="source-note"><strong>{escape(sec)}</strong></p>')
            last_section = sec
        frag_text = escape(frag.text)
        if frag.id == target_fragment.id:
            blocks_html.append(f'<p id="fragment-{frag_id}"><mark>{frag_text}</mark></p>')
        else:
            blocks_html.append(f'<p id="fragment-{frag_id}">{frag_text}</p>')

    target_attr = escape(target_fragment.id, quote=True)
    body_blocks = "\n      ".join(blocks_html)

    return (
        "<!doctype html>\n"
        '<html lang="ru">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '  <meta name="color-scheme" content="light dark">\n'
        f"  <title>{safe_title}</title>\n"
        '  <link rel="stylesheet" href="/static/css/source.css">\n'
        f"  {_inline_source_css_tag()}\n"
        "</head>\n"
        "<body>\n"
        '  <main class="source-page">\n'
        f"    <h1>{safe_title}</h1>\n"
        f'    <p class="source-locator">{human_locator} · <code>{locator_json}</code></p>\n'
        f"    {section_html}"
        f"    {note_html}"
        f'    <article class="source-document" data-fragment-id="{target_attr}">\n'
        f"      {body_blocks}\n"
        "    </article>\n"
        "  </main>\n"
        "</body>\n"
        "</html>"
    )
