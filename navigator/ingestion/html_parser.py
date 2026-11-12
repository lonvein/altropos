"""HTML: основной текст веб-страниц регламентов, адресуемые блоки и ссылки на документы.

Блок = смысловой элемент (заголовок, абзац, элемент списка или таблица),
нумерация с нуля (locator.block), как требует схема HTMLLocator.
Извлекаются ссылки на скачиваемые нормативные документы (.pdf, .docx, .xlsx)
с нормализацией относительно base_url.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from navigator.ingestion.common import ParseError, Unit
from navigator.ingestion.model import Parsed
from navigator.ingestion.outline import Outline

_SPACES = re.compile(r"[ \t\r\f\v\u00a0\u2007\u202f]+")
_DOC_EXTS = {".pdf", ".docx", ".xlsx", ".doc", ".xls"}


def _clean_text(text: str) -> str:
    text = _SPACES.sub(" ", text.replace("\u200b", ""))
    return "\n".join(line.strip() for line in text.split("\n") if line.strip()).strip()


def _is_doc_url(url: str) -> bool:
    clean_url = url.split("?")[0].split("#")[0].lower()
    return any(clean_url.endswith(ext) for ext in _DOC_EXTS)


def parse_html(source: Path | str, base_url: str | None = None) -> Parsed:
    """Разбирает HTML-файл или строку HTML в Parsed объект."""
    if isinstance(source, Path):
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = source.read_text(encoding="cp1251")
            except Exception as exc:
                raise ParseError("Не удалось прочитать HTML-файл: неверная кодировка") from exc
        except Exception as exc:
            raise ParseError("Не удалось открыть HTML-файл") from exc
        title_hint = source.stem
    else:
        content = source
        title_hint = ""

    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    # Извлекаем title страницы
    page_title = ""
    if soup.title and soup.title.string:
        page_title = _clean_text(soup.title.string)

    # Собираем ссылки на документы до удаления тегов
    links: list[dict] = []
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        if not raw_href or raw_href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        full_url = urljoin(base_url or "", raw_href) if base_url else raw_href
        if _is_doc_url(full_url) and full_url not in seen_hrefs:
            seen_hrefs.add(full_url)
            link_text = _clean_text(a.get_text()) or Path(full_url.split("?")[0]).name
            ext = Path(full_url.split("?")[0]).suffix.lower()
            links.append({"url": full_url, "text": link_text, "extension": ext})

    # Удаляем служебные теги
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    # Ищем основной контейнер содержания
    main_container = soup.find(["main", "article"]) or soup.find(attrs={"role": "main"}) or soup.body or soup

    units: list[Unit] = []
    blocks: list[dict] = []
    warnings: list[str] = []
    outline = Outline()

    # Обходим блочные элементы
    block_tags = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "blockquote", "pre"]
    elements = main_container.find_all(block_tags)

    # Если специальных блочных тегов нет, берем параграфы и текст напрямую
    if not elements:
        elements = [main_container]

    for el in elements:
        # Пропускаем вложенные теги, если родительский уже обработан (например, li внутри ul)
        tag_name = el.name.lower()
        if tag_name == "table":
            # Разбор таблицы
            rows = []
            for tr in el.find_all("tr"):
                cells = [_clean_text(td.get_text()) for td in tr.find_all(["td", "th"])]
                if any(cells):
                    rows.append(cells)
            if not rows:
                continue
            index = len(blocks)
            blocks.append({"index": index, "kind": "table", "rows": rows, "html": str(el)})
            header = " | ".join(rows[0]) if len(rows) > 1 else ""
            body_rows = [" | ".join(r) for r in (rows[1:] if len(rows) > 1 else rows)]
            units.append(Unit(
                text="\n".join(" | ".join(r) for r in rows),
                locator={"kind": "html", "block": index},
                kind="table",
                path=outline.path,
                table_header=header,
                rows=body_rows
            ))
            continue

        text = _clean_text(el.get_text())
        if not text:
            continue

        index = len(blocks)
        is_heading = tag_name.startswith("h")
        blocks.append({"index": index, "kind": "heading" if is_heading else "paragraph", "text": text, "tag": tag_name})
        info = outline.feed(text, style_heading=is_heading)

        units.append(Unit(
            text=text,
            locator={"kind": "html", "block": index},
            section=info.section,
            path=info.path,
            kind=info.kind,
            atomic=len(text) < 900
        ))

    if not units:
        warnings.append("На веб-странице не найдено смыслового текста")

    title = page_title or title_hint
    return Parsed(
        media_type="html",
        title=title,
        units=units,
        blocks=blocks,
        warnings=warnings,
        needs_review=not units,
        links=links
    )
