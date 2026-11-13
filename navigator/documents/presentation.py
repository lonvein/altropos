"""Escaped, complete text views of immutable published fragments."""
import json
from html import escape

from navigator.schemas import Fragment


SOURCE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


def render_publication(title: str, document_id: str, fragments: list[Fragment], selected: str | None = None) -> str:
    """Preserve every published fragment, including exact evidence and locators.

    Indexed fragments may overlap. Keep them separate and labelled, rather than
    heuristically deleting repeated text that could be meaningful in the original.
    """
    sections = []
    for fragment in sorted(fragments, key=lambda f: f.ordinal):
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
            f'<pre>{escape(fragment.text)}</pre></section>')
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)}</title><link rel="stylesheet" href="/static/css/source.css"></head>'
        f'<body><main class="source-page"><h1>{escape(title)}</h1>'
        '<nav class="source-navigation">'
        f'<a href="/api/documents/{escape(document_id)}/original">Скачать оригинал</a> · '
        '<a href="/">Задать вопрос</a></nav>'
        + (f'<p><a href="#fragment-{escape(selected)}">Перейти к цитируемому фрагменту</a></p>' if selected else '') +
        '<p class="source-note">Полный опубликованный распознанный текст. '
        'Фрагменты поиска могут перекрываться; исходное оформление доступно в оригинале.</p>'
        '<article class="source-document">' + ''.join(sections) + '</article></main></body></html>')
