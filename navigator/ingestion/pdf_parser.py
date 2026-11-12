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
from navigator.ingestion.ocr import OCRResult

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
    origin_x, origin_y = page.bbox[:2]
    x0, y0, x1, y1 = (round(v - offset, 1) for v, offset in
                       zip(box, (origin_x, origin_y, origin_x, origin_y)))
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
        previous = None
        for line in result:
            marker = re.match(r"^(?:\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+|[*†‡]+)\s+", line["text"])
            point = re.match(r"^\d+[.)]", line["text"])
            continuation = (previous and previous["note"]
                            and line["top"]-previous["bottom"] < max(1, previous["bottom"]-previous["top"])*1.2)
            line["note"] = bool(line["size"] < body - 1.5 and line["top"] > page.height*.7
                                and not point and (marker or continuation))
            previous = line
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
        try:
            margin_lines = page.extract_text_lines(layout=False, strip=True, return_chars=False)
        except Exception:
            continue
        for line in margin_lines:
            if line["top"] < page.height * MARGIN_RATIO:
                text = re.sub(r"\d+", "#", _clean(line["text"]))
                if text and text not in seen:
                    seen.add(text)
                    counter[text] += 1
    return {t for t, n in counter.items() if n >= max(2, len(pdf.pages) // 2)}


def _broken_encoding(text):
    nonspace = [c for c in text if not c.isspace()]
    bad = sum(c == "\ufffd" or (0xE000 <= ord(c) <= 0xF8FF and c not in _BULLETS)
              for c in nonspace)
    return (bool(re.search(r"\(cid:\d+\)", text)) or bad > 0
            or (len(nonspace) >= MIN_PAGE_CHARS and sum(c.isalpha() for c in nonspace) < len(nonspace)*.2))


def _link_notes(units, blocks, diagnostics):
    # Only explicit, unique markers on the same page justify a link.
    superscripts = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
    for index, note in enumerate(units):
        if note.kind != "note":
            continue
        page = note.locator["page"]
        match = re.match(r"^\s*(\d+|[*†‡]+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s+", note.text)
        candidates = []
        if match:
            marker = match[1]
            if marker.isascii() and marker.isdigit():
                pattern = re.compile(r"\["+re.escape(marker)+r"\]|(?<![⁰¹²³⁴⁵⁶⁷⁸⁹])"
                                     +re.escape(marker.translate(superscripts))+r"(?![⁰¹²³⁴⁵⁶⁷⁸⁹])")
            else:
                pattern = re.compile(r"(?<![*†‡⁰¹²³⁴⁵⁶⁷⁸⁹])"+re.escape(marker)+r"(?![*†‡⁰¹²³⁴⁵⁶⁷⁸⁹])")
            for unit in units[:index]:
                if unit.kind == "note" or unit.locator.get("page") != page:
                    continue
                evidence = [m for b in blocks if b.get("page") == page
                            and b.get("bbox") == unit.locator.get("bbox")
                            for m in b.get("reference_markers", [])]
                literal = bool(pattern.search(unit.text))
                if all(c in "*†‡" for c in marker):
                    # A bare asterisk may be multiplication or damaged OCR punctuation.
                    literal = f"[{marker}]" in unit.text
                if literal or any(m.get("confirmed") and m["text"] == marker for m in evidence):
                    candidates.append(unit)
        note.note_id = f"pdf:{page}:{index}"
        if len(candidates) == 1:
            candidates[0].note_refs += (note.note_id,)
        else:
            diag = diagnostics[page-1]
            diag["warnings"].append("Связь сноски с пунктом/таблицей не подтверждена")
            diag["needs_review"] = True
        for block in blocks:
            if block.get("page") == page and block.get("text") == note.text:
                block.update(kind="note", note_id=note.note_id,
                             linked=len(candidates) == 1)
                if len(candidates) != 1:
                    block["candidate_grounds"] = [
                        {"bbox": b.get("bbox"), "markers": b["reference_markers"]}
                        for b in blocks if b.get("page") == page and not b.get("kind") == "note"
                        and b.get("reference_markers") and b.get("bbox", [0,0,0,0])[1] < note.locator.get("bbox", [0,0,0,0])[1]]


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
    diagnostics = []
    outline = Outline()
    ocr_pages: list[int] = []
    unreadable_pages: list[int] = []
    try:
        if not pdf.pages:
            raise ParseError("PDF не содержит страниц")
        noise = _margin_noise(pdf)
        for number, page in enumerate(pdf.pages, start=1):
            diag = {"page": number, "source": "native", "rotation": 0,
                    "pdf_rotation": page.rotation, "warnings": [], "confidence": None,
                    "needs_review": False}
            diagnostics.append(diag)
            try:
                native_text = (page.extract_text() or "").strip()
            except Exception:
                native_text = ""
                diag["warnings"].append("Ошибка чтения текстового слоя")
            if len(native_text) < MIN_PAGE_CHARS or _broken_encoding(native_text):
                result = _ocr_page(page, number, ocr)
                diag.update(result.diagnostics)
                if _broken_encoding(native_text):
                    diag["warnings"].append("Повреждённая кодировка текстового слоя; использован OCR")
                if not result:
                    unreadable_pages.append(number)
                    diag["needs_review"] = True
                    continue
                ocr_pages.append(number)
                for item in result:
                    if item.get("suspected_rule"):
                        blocks.append({"page": number, "text": item["text"], "bbox": item.get("bbox"),
                                       "ocr": True, "kind": "ocr_artifact", "suspected_rule": True})
                        diag["needs_review"] = True
                        continue
                    if item.get("note_continuation") and units and units[-1].kind == "note" \
                            and units[-1].locator.get("page") == number:
                        previous = units[-1]
                        previous_block = next(b for b in reversed(blocks)
                                              if b.get("page") == number and b.get("text") == previous.text)
                        previous.text += " " + item["text"]
                        old, new = previous.locator.get("bbox"), item.get("bbox")
                        if old and new:
                            previous.locator["bbox"] = [min(old[0],new[0]),min(old[1],new[1]),
                                                        max(old[2],new[2]),max(old[3],new[3])]
                        previous_block.update(text=previous.text, bbox=previous.locator.get("bbox"))
                        continue
                    box = item.get("bbox")
                    is_note = bool(item.get("note"))
                    info = None if is_note else outline.feed(item["text"], list_item=bool(item.get("list_item")))
                    loc = {"kind": "pdf", "page": number}
                    if item.get("bbox"):
                        loc["bbox"] = item["bbox"]
                    units.append(Unit(text=item["text"], locator=loc, section=info.section if info else "",
                                      path=info.path if info else outline.path,
                                      kind=info.kind if info else "note", atomic=len(item["text"]) < 900))
                    blocks.append({"page": number, "text": item["text"], "bbox": item.get("bbox"), "ocr": True,
                                   "conf": item.get("conf"), "reference_markers": item.get("reference_markers", []),
                                   "kind": "note" if is_note else info.kind,
                                   "list_item": bool(item.get("list_item")), "raw_text": item.get("raw_text", item["text"]),
                                   "layout_corrections": item.get("layout_corrections", [])})
                continue

            table_boxes, tables = [], []
            try:
                for table in page.find_tables():
                    data = table.extract()
                    if _good_table(data):
                        table_boxes.append(table.bbox)
                        tables.append((table, data))
            except Exception:
                diag["warnings"].append("Не удалось определить таблицы")
                diag["needs_review"] = True
            try:
                page_lines = _page_lines(page, table_boxes)
            except Exception:
                diag.update(source="error", needs_review=True)
                diag["warnings"].append("Ошибка извлечения строк страницы")
                continue
            lines = [l for l in page_lines
                     if not (not l.get("note") and re.sub(r"\d+", "#", l["text"]) in noise
                             and (l["top"] < page.height * MARGIN_RATIO
                                  or l["bottom"] > page.height * (1 - MARGIN_RATIO)))
                     and not (not l.get("note") and re.fullmatch(r"[-–—\s]*\d{1,3}[-–—\s]*", l["text"])
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
                    if info is None:  # сноска сохраняется и связывается после разбора страницы
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

    _link_notes(units, blocks, diagnostics)
    for diag in diagnostics:
        warnings.extend(f"Страница {diag['page']}: {w}" for w in diag["warnings"])
        needs_review |= diag.get("needs_review", False)
    if ocr_pages:
        warnings.append(f"Страницы распознаны OCR: {_ranges(ocr_pages)}; текст может содержать ошибки")
    if unreadable_pages:
        needs_review = True
        why = "OCR недоступен" if ocr is None else "OCR не дал читаемого текста"
        warnings.append(f"Страницы без текста ({why}): {_ranges(unreadable_pages)}; требуется проверка")
    if not units:
        needs_review = True
        warnings.append("Из PDF не удалось извлечь текст")
    return Parsed(media_type="pdf", units=units, blocks=blocks, warnings=warnings, needs_review=needs_review,
                  diagnostics=diagnostics)


def _ocr_page(page, number: int, ocr):
    """Legacy two-argument hooks remain supported; rich hooks receive display size."""
    diag = {"page": number, "source": "error", "rotation": None,
            "warnings": [], "confidence": None, "needs_review": True}
    if ocr is None:
        diag["warnings"].append("OCR недоступен")
        return OCRResult(**diag)
    try:
        image = page.to_image(resolution=300, force_mediabox=True).original
        if getattr(ocr, "supports_page_size", False):
            items = ocr(image, number, page_size=(float(page.width), float(page.height)))
        else:
            items = ocr(image, number)
        if hasattr(items, "diagnostics"):
            if not items and image.convert("L").getextrema()[0] > 250:
                items.diagnostics["source"] = "blank"
            return items
        diag.update(source="ocr", needs_review=not bool(items))
        # Legacy hooks have no orientation/confidence evidence.
        if not items and image.convert("L").getextrema()[0] > 250:
            diag["source"] = "blank"
        return OCRResult(items or (), **diag)
    except Exception:
        diag["warnings"].append("Не удалось отрисовать или распознать страницу")
        return OCRResult(**diag)


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
