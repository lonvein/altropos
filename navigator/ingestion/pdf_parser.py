"""PDF: текстовый слой через pdfplumber, абзацы, таблицы, координаты.

Страница нумеруется с единицы, bbox — в пунктах от левого верхнего угла страницы
(так отдаёт pdfplumber). Страницы без пригодного текстового слоя передаются в OCR
(`navigator.ingestion.ocr`); если OCR недоступен или результат нечитаем, страница
НЕ пропускается молча: добавляется предупреждение, документ получает needs_review.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from navigator.ingestion.common import ParseError, Unit
from navigator.ingestion.model import Parsed
from navigator.ingestion.outline import Outline

MIN_PAGE_CHARS = 30
MARGIN_RATIO = 0.07  # доля высоты страницы под колонтитулы
_BULLETS = {"\uf02d": "-", "\uf0b7": "•", "\uf0a7": "•", "\uf076": "•", "\uf0d8": "•", "\uf0fc": "•"}
_LIST_START = re.compile(r"^(\d+(?:\.\d+)*[.)]?\s|[•\-–—]\s|[а-яa-z]\)\s)", re.I)
_SPACES = re.compile(r"[ \t\u00a0\u2007\u202f]+")


def _clean(text: str) -> str:
    for src, dst in _BULLETS.items():
        text = text.replace(src, dst)
    text = "".join(c for c in text if not (0xE000 <= ord(c) <= 0xF8FF))  # прочие символы-значки
    return _SPACES.sub(" ", text).strip()


def _union(lines: list[dict]) -> tuple[float, float, float, float]:
    return (min(l["x0"] for l in lines), min(l["top"] for l in lines),
            max(l["x1"] for l in lines), max(l["bottom"] for l in lines))


def _bbox(box, page) -> list[float] | None:
    x0, y0, x1, y1 = (round(v, 1) for v in box)
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(round(float(page.width), 1), x1), min(round(float(page.height), 1), y1)
    return [x0, y0, x1, y1] if x0 < x1 and y0 < y1 else None


def _good_table(rows: list[list[str | None]]) -> bool:
    """Отсекает ложные «таблицы» из рамок со сплошным текстом."""
    rows = [[(c or "").strip() for c in r] for r in rows if r]
    if len(rows) < 2:
        return False
    multi = sum(1 for r in rows if sum(1 for c in r if c) >= 2)
    return multi / len(rows) >= 0.6


def _in_box(line: dict, box) -> bool:
    cx, cy = (line["x0"] + line["x1"]) / 2, (line["top"] + line["bottom"]) / 2
    return box[0] - 1 <= cx <= box[2] + 1 and box[1] - 1 <= cy <= box[3] + 1


def _line_font(line: dict) -> tuple[bool, float]:
    chars = [c for c in line.get("chars", []) if c["text"].strip()]
    if not chars:
        return False, 0.0
    bold = sum(1 for c in chars if "bold" in c.get("fontname", "").lower()) / len(chars) >= 0.8
    sizes = sorted(c["size"] for c in chars)
    return bold, sizes[len(sizes) // 2]


def _page_lines(page, table_boxes) -> list[dict]:
    lines = page.extract_text_lines(layout=False, strip=True, return_chars=True)
    result = []
    for line in lines:
        text = _clean(line["text"])
        if not text or any(_in_box(line, box) for box in table_boxes):
            continue
        bold, size = _line_font(line)
        result.append({**{k: v for k, v in line.items() if k != "chars"}, "text": text,
                       "bold": bold, "size": size})
    if result:
        body = Counter(round(l["size"], 1) for l in result).most_common(1)[0][0]
        for l in result:
            l["note"] = l["size"] < body - 1.5 and l["top"] > page.height * 0.7
    return result


def _paragraphs(lines: list[dict]) -> list[list[dict]]:
    paragraphs: list[list[dict]] = []
    for line in lines:
        if paragraphs:
            prev = paragraphs[-1][-1]
            height = max(prev["bottom"] - prev["top"], 1)
            gap = line["top"] - prev["bottom"]
            new = (gap > 0.8 * height or bool(_LIST_START.match(line["text"]))
                   or line.get("bold") != prev.get("bold") or line.get("note") != prev.get("note"))
            # Строка с резким отступом влево после точки — тоже новый абзац
            if not new and prev["text"].endswith((".", ":", ";")) and line["x0"] - prev["x0"] > 12:
                new = True
            if not new:
                paragraphs[-1].append(line)
                continue
        paragraphs.append([line])
    return paragraphs


def _join(lines: list[dict]) -> str:
    out = ""
    for line in lines:
        text = line["text"]
        if out.endswith("-") and text[:1].islower():  # перенос слова
            out = out[:-1] + text
        else:
            out = f"{out} {text}".strip()
    return out


def _margin_noise(pdf) -> set[str]:
    """Повторяющиеся колонтитулы: строки у краёв, встречающиеся на >=50% страниц."""
    if len(pdf.pages) < 3:
        return set()
    counter: Counter[str] = Counter()
    for page in pdf.pages:
        seen = set()
        for line in page.extract_text_lines(layout=False, strip=True, return_chars=False):
            if line["top"] < page.height * MARGIN_RATIO or line["bottom"] > page.height * (1 - MARGIN_RATIO):
                text = re.sub(r"\d+", "#", _clean(line["text"]))
                if text and text not in seen:
                    seen.add(text)
                    counter[text] += 1
    return {t for t, n in counter.items() if n >= max(2, len(pdf.pages) // 2)}


def parse_pdf(path: Path, ocr=None) -> Parsed:
    """ocr: необязательная функция (page_image_pil, page_number) -> list[dict(text, bbox)]."""
    try:
        import pdfplumber
        pdf = pdfplumber.open(str(path))
    except Exception as exc:
        raise ParseError("Не удалось прочитать PDF: файл повреждён, защищён паролем или имеет неверный формат") from exc

    units: list[Unit] = []
    blocks: list[dict] = []
    warnings: list[str] = []
    needs_review = False
    outline = Outline()
    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []
    try:
        if not pdf.pages:
            raise ParseError("PDF не содержит страниц")
        noise = _margin_noise(pdf)
        for number, page in enumerate(pdf.pages, start=1):
            try:
                text_chars = len((page.extract_text() or "").strip())
            except Exception:
                text_chars = 0
            if text_chars < MIN_PAGE_CHARS:
                result = _ocr_page(page, number, ocr)
                if result is None:
                    unreadable_pages.append(number)
                    continue
                ocr_pages.append(number)
                for item in result:
                    info = outline.feed(item["text"])
                    loc = {"kind": "pdf", "page": number}
                    if item.get("bbox"):
                        loc["bbox"] = item["bbox"]
                    units.append(Unit(text=item["text"], locator=loc, section=info.section, path=info.path,
                                      kind=info.kind, atomic=len(item["text"]) < 900))
                    blocks.append({"page": number, "text": item["text"], "bbox": item.get("bbox"), "ocr": True})
                continue

            table_boxes, tables = [], []
            try:
                for table in page.find_tables():
                    data = table.extract()
                    if _good_table(data):
                        table_boxes.append(table.bbox)
                        tables.append((table, data))
            except Exception:
                warnings.append(f"Страница {number}: не удалось определить таблицы")
            lines = [l for l in _page_lines(page, table_boxes)
                     if not (re.sub(r"\d+", "#", l["text"]) in noise
                             and (l["top"] < page.height * MARGIN_RATIO
                                  or l["bottom"] > page.height * (1 - MARGIN_RATIO)))
                     and not (re.fullmatch(r"[-–—\s]*\d{1,3}[-–—\s]*", l["text"])
                              and (l["top"] < page.height * MARGIN_RATIO
                                   or l["bottom"] > page.height * (1 - MARGIN_RATIO)))]
            # Абзацы и таблицы идут в порядке вертикального положения на странице
            items: list[tuple[float, str, object]] = [(p[0]["top"], "p", p) for p in _paragraphs(lines)]
            items += [(t.bbox[1], "t", (t, d)) for t, d in tables]
            for _, kind, payload in sorted(items, key=lambda x: x[0]):
                if kind == "p":
                    para = payload
                    text = _join(para)
                    if not text:
                        continue
                    is_note = all(l.get("note") for l in para)
                    bold_heading = (all(l.get("bold") for l in para) and len(para) <= 2 and len(text) <= 100
                                    and not text.endswith((".", ";", ",", ":")))
                    info = outline.feed(text, style_heading=bold_heading and not is_note) if not is_note \
                        else None
                    loc = {"kind": "pdf", "page": number}
                    box = _bbox(_union(para), page)
                    if box:
                        loc["bbox"] = box
                    if info is None:  # сноска: отдельный фрагмент, не смешивается с основным текстом
                        units.append(Unit(text=text, locator=loc, path=outline.path, kind="note", atomic=True))
                    else:
                        units.append(Unit(text=text, locator=loc, section=info.section, path=info.path,
                                          kind=info.kind, atomic=len(text) < 900))
                    blocks.append({"page": number, "text": text, "bbox": box})
                else:
                    table, data = payload
                    rows = [[_clean((c or "").replace("\n", " ")) for c in r] for r in data]
                    rows = [r for r in rows if any(r)]
                    loc = {"kind": "pdf", "page": number}
                    box = _bbox(table.bbox, page)
                    if box:
                        loc["bbox"] = box
                    header = " | ".join(rows[0])
                    body = [" | ".join(r) for r in rows[1:]]
                    units.append(Unit(text="\n".join(" | ".join(r) for r in rows), locator=loc, kind="table",
                                      path=outline.path, table_header=header, rows=body))
                    blocks.append({"page": number, "table": rows, "bbox": box})
    finally:
        pdf.close()

    if ocr_pages:
        warnings.append(f"Страницы распознаны OCR: {_ranges(ocr_pages)}; текст может содержать ошибки")
    if unreadable_pages:
        needs_review = True
        why = "OCR недоступен" if ocr is None else "OCR не дал читаемого текста"
        warnings.append(f"Страницы без текста ({why}): {_ranges(unreadable_pages)}; требуется проверка")
    if not units:
        needs_review = True
        warnings.append("Из PDF не удалось извлечь текст")
    return Parsed(media_type="pdf", units=units, blocks=blocks, warnings=warnings, needs_review=needs_review)


def _ocr_page(page, number: int, ocr):
    """None, если страницу нельзя прочитать (OCR отсутствует/пустой результат)."""
    if ocr is None:
        return None
    try:
        image = page.to_image(resolution=300).original
        items = ocr(image, number)
    except Exception:
        return None
    return items or None


def _ranges(pages: list[int]) -> str:
    out, start, prev = [], None, None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start}" if start == prev else f"{start}–{prev}")
            start = prev = p
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}–{prev}")
    return ", ".join(out)
