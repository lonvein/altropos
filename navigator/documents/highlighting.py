"""Rendering original PDF/DOCX pages and resolving multi-line fragment highlight boxes."""
from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from navigator.documents import textmatch
from navigator.documents.pdf_geometry import PDFIUM_LOCK, PageFrame, native_lines, native_words, page_frame
from navigator.schemas import Fragment

_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_ANNOTATION_TAG_RE = re.compile(r"\[(?:надстрочный|сноска):([^\]]+)\]")
_MERGED_TAG_RE = re.compile(r"\[объединено с R\d+C\d+:\s*[^\]]*\]")
_PRECEDING_ROW_RE = re.compile(r"^\[Предшествующая строка R\d+[^\]]*\]\s*", re.MULTILINE)
_ROW_PREFIX_RE = re.compile(r"(?m)^R\d+:\s*")

_DOCX_PAGE_W = 595.0
_DOCX_PAGE_H = 842.0
_DOCX_MARGIN_X = 48.0
_DOCX_MARGIN_TOP = 46.0
_DOCX_MARGIN_BOTTOM = 46.0
_DOCX_CONTENT_W = _DOCX_PAGE_W - 2 * _DOCX_MARGIN_X
_DOCX_LAYOUT_CACHE: dict[tuple[str, int, int], list[dict[str, Any]] | None] = {}
_DOCX_LAYOUT_CACHE_MAX = 32


def _norm(text: str) -> str:
    cleaned = (text or "").replace("\u00a0", " ").replace("\u202f", " ").replace("\u2007", " ").replace("\u00ad", "")
    cleaned = cleaned.replace("ё", "е").replace("Ё", "Е")
    cleaned = cleaned.replace("—", "-").replace("–", "-")
    return _SPACE_RE.sub(" ", cleaned).strip().lower()


def _clean_query_for_matching(text: str) -> str:
    """Strip synthetic markdown/table markers so citation text matches original page lines."""
    raw = text or ""
    lines = raw.splitlines()
    if len(lines) > 1 and " > " in lines[0]:
        raw = "\n".join(lines[1:])
    raw = _PRECEDING_ROW_RE.sub("", raw)
    raw = _ROW_PREFIX_RE.sub("", raw)
    raw = _MERGED_TAG_RE.sub("", raw)
    raw = _ANNOTATION_TAG_RE.sub(r"[\1]", raw)
    raw = raw.replace("|", " ")
    return raw.strip()


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(_norm(text)) if len(w) >= 2}


def _box_overlaps(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...], pad: float = 4.0) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def _valid_box(box: Any) -> list[float] | None:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (round(float(v), 2) for v in box)
    except (TypeError, ValueError):
        return None
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


@lru_cache(maxsize=32)
def _load_font(size_px: int, bold: bool = False):
    from PIL import ImageFont

    size = max(8, int(size_px))
    candidates = (
        [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\segoeuib.ttf",
            r"C:\Windows\Fonts\calibrib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]
        if bold
        else [
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\calibri.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
        ]
    )
    for font_path in candidates:
        if os.path.isfile(font_path):
            try:
                return ImageFont.truetype(font_path, size=size)
            except OSError:
                continue
    if bold:
        # Try regular TTF font before built-in fallback.
        for font_path in (
            r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ):
            if os.path.isfile(font_path):
                try:
                    return ImageFont.truetype(font_path, size=size)
                except OSError:
                    continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _text_width_pt(text: str, font, measure_scale: float = 2.0) -> float:
    if not text:
        return 0.0
    try:
        return float(font.getlength(text)) / measure_scale
    except Exception:
        return len(text) * 5.2


def _wrap_text_to_width(text: str, max_width_pt: float, font, measure_scale: float = 2.0) -> list[str]:
    """Wrap text onto visual lines fitting inside max_width_pt without splitting words unnecessarily."""
    result: list[str] = []
    usable = max(24.0, max_width_pt)
    for raw_para in (text or "").split("\n"):
        tokens = raw_para.split()
        if not tokens:
            continue
        current = ""
        for tok in tokens:
            candidate = tok if not current else f"{current} {tok}"
            if _text_width_pt(candidate, font, measure_scale) <= usable:
                current = candidate
                continue
            if current:
                result.append(current)
                current = ""
            if _text_width_pt(tok, font, measure_scale) <= usable:
                current = tok
            else:
                # Hard-wrap extra-long tokens so tables never overflow cell borders.
                chunk = ""
                for ch in tok:
                    if chunk and _text_width_pt(chunk + ch, font, measure_scale) > usable:
                        result.append(chunk)
                        chunk = ch
                    else:
                        chunk += ch
                current = chunk
        if current:
            result.append(current)
    return result or [""]


def _word_boxes(text: str, x: float, y0: float, y1: float, font, measure_scale: float) -> list[dict[str, Any]]:
    """Per-word boxes of a drawn DOCX line, measured with the font used to render it."""
    words: list[dict[str, Any]] = []
    for match in re.finditer(r"\S+", text):
        left = x + _text_width_pt(text[:match.start()], font, measure_scale)
        right = x + _text_width_pt(text[:match.end()], font, measure_scale)
        words.append({"text": match.group(0), "bbox": [round(left, 2), y0, round(max(right, left + 0.5), 2), y1]})
    return words


def _docx_display_text(text: str) -> str:
    cleaned = _MERGED_TAG_RE.sub("", text or "")
    cleaned = _ANNOTATION_TAG_RE.sub(r"[\1]", cleaned)
    return _SPACE_RE.sub(" ", cleaned).strip()


def _build_docx_paginated_layout(path: Path | None) -> list[dict[str, Any]] | None:
    """Lay out a DOCX document onto paginated A4 canvases with per-line bounding boxes."""
    if path is None or not path.is_file():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    cache_key = (str(path.resolve()), int(st.st_mtime_ns), int(st.st_size))
    if cache_key in _DOCX_LAYOUT_CACHE:
        return _DOCX_LAYOUT_CACHE[cache_key]

    try:
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = docx.Document(str(path))
    except Exception:
        if len(_DOCX_LAYOUT_CACHE) >= _DOCX_LAYOUT_CACHE_MAX:
            _DOCX_LAYOUT_CACHE.pop(next(iter(_DOCX_LAYOUT_CACHE)))
        _DOCX_LAYOUT_CACHE[cache_key] = None
        return None

    from navigator.ingestion.docx_parser import _element_text, _footnotes, _table_grid

    measure_scale = 2.0
    fonts = {
        (13.0, True): _load_font(int(13.0 * measure_scale), bold=True),
        (11.5, True): _load_font(int(11.5 * measure_scale), bold=True),
        (10.0, True): _load_font(int(10.0 * measure_scale), bold=True),
        (10.0, False): _load_font(int(10.0 * measure_scale), bold=False),
        (9.0, True): _load_font(int(9.0 * measure_scale), bold=True),
        (9.0, False): _load_font(int(9.0 * measure_scale), bold=False),
        (8.5, False): _load_font(int(8.5 * measure_scale), bold=False),
    }

    pages: list[dict[str, Any]] = []

    def new_page() -> dict[str, Any]:
        page_dict = {
            "page": len(pages) + 1,
            "width": _DOCX_PAGE_W,
            "height": _DOCX_PAGE_H,
            "lines": [],
            "rects": [],
            "draws": [],
        }
        pages.append(page_dict)
        return page_dict

    cur_page = new_page()
    cur_y = _DOCX_MARGIN_TOP
    max_y = _DOCX_PAGE_H - _DOCX_MARGIN_BOTTOM

    def ensure_space(needed_h: float) -> None:
        nonlocal cur_page, cur_y
        if cur_y + needed_h > max_y and cur_y > _DOCX_MARGIN_TOP + 2.0:
            cur_page = new_page()
            cur_y = _DOCX_MARGIN_TOP

    block_index = 0
    referenced_notes: set[str] = set()

    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            raw_text = _element_text(element)
            if not raw_text:
                continue
            para = Paragraph(element, document)
            style_name = (para.style.name or "").lower() if para.style is not None else ""
            is_title = "title" in style_name
            is_h1 = style_name.startswith(("heading 1", "заголовок 1")) or is_title
            is_heading = style_name.startswith(("heading", "заголовок")) or is_title
            runs_bold = bool(para.runs) and all(bool(r.bold) for r in para.runs if (r.text or "").strip())
            bold = is_heading or runs_bold
            font_pt = 13.0 if is_h1 else (11.5 if is_heading else 10.0)
            line_h = round(font_pt * 1.42, 2)
            font = fonts[(font_pt, bold)]

            display = _docx_display_text(raw_text)
            if not display:
                block_index += 1
                continue

            wrapped = _wrap_text_to_width(display, _DOCX_CONTENT_W, font, measure_scale)
            align = para.alignment
            for line_text in wrapped:
                ensure_space(line_h)
                text_w = max(8.0, min(_DOCX_CONTENT_W, _text_width_pt(line_text, font, measure_scale)))
                if align == WD_ALIGN_PARAGRAPH.CENTER:
                    x0 = round(_DOCX_MARGIN_X + (_DOCX_CONTENT_W - text_w) / 2.0, 2)
                elif align == WD_ALIGN_PARAGRAPH.RIGHT:
                    x0 = round(_DOCX_MARGIN_X + (_DOCX_CONTENT_W - text_w), 2)
                else:
                    x0 = _DOCX_MARGIN_X
                y0 = round(cur_y, 2)
                x1 = round(min(_DOCX_PAGE_W - _DOCX_MARGIN_X, x0 + text_w + 4.0), 2)
                y1 = round(y0 + line_h, 2)
                line_bbox = [x0, y0, x1, y1]
                cur_page["draws"].append({
                    "x": x0,
                    "y": y0 + 1.0,
                    "text": line_text,
                    "font_pt": font_pt,
                    "bold": bold,
                })
                cur_page["lines"].append({
                    "page": cur_page["page"],
                    "block": block_index,
                    "text": line_text,
                    "bbox": line_bbox,
                    "words": _word_boxes(line_text, x0, y0, y1, font, measure_scale),
                })
                cur_y += line_h
            cur_y += 6.0 if is_heading else 4.5
            block_index += 1

        elif tag == "tbl":
            rows, _resolved, _merges, trs = _table_grid(Table(element, document))
            if not rows:
                continue
            col_count = max(len(r) for r in rows)
            if col_count <= 0:
                block_index += 1
                continue

            # Weight columns softly by observed content length so wide text columns get fair room.
            weights = [8.0] * col_count
            for r in rows[:20]:
                for c_idx in range(min(col_count, len(r))):
                    cell_len = min(80, max(4, len(_docx_display_text(r[c_idx]))))
                    weights[c_idx] = max(weights[c_idx], float(cell_len))
            total_weight = max(1.0, sum(weights))
            col_widths = [max(36.0, (_DOCX_CONTENT_W * w) / total_weight) for w in weights]
            scale_factor = _DOCX_CONTENT_W / sum(col_widths)
            col_widths = [round(cw * scale_factor, 2) for cw in col_widths]

            cell_font_pt = 9.0
            cell_line_h = round(cell_font_pt * 1.38, 2)
            pad_x = 5.0
            pad_y = 4.0

            for r_idx, row in enumerate(rows):
                is_header = r_idx == 0
                font = fonts[(cell_font_pt, is_header)]
                cell_wrapped: list[list[str]] = []
                max_lines = 1
                for c_idx in range(col_count):
                    cell_text = _docx_display_text(row[c_idx] if c_idx < len(row) else "")
                    wrapped_cell = _wrap_text_to_width(cell_text, max(16.0, col_widths[c_idx] - 2 * pad_x), font, measure_scale)
                    if not cell_text:
                        wrapped_cell = []
                    cell_wrapped.append(wrapped_cell)
                    max_lines = max(max_lines, len(wrapped_cell))

                row_h = round(max_lines * cell_line_h + 2 * pad_y, 2)
                if cur_y + row_h > max_y and cur_y > _DOCX_MARGIN_TOP + 2.0:
                    cur_page = new_page()
                    cur_y = _DOCX_MARGIN_TOP

                row_y0 = round(cur_y, 2)
                row_y1 = round(cur_y + row_h, 2)
                x_cursor = _DOCX_MARGIN_X
                for c_idx in range(col_count):
                    cw = col_widths[c_idx]
                    cell_x0 = round(x_cursor, 2)
                    cell_x1 = round(x_cursor + cw, 2)
                    cur_page["rects"].append({
                        "bbox": [cell_x0, row_y0, cell_x1, row_y1],
                        "fill": (241, 245, 249) if is_header else None,
                        "outline": (175, 188, 204),
                    })
                    for ln_idx, ln_txt in enumerate(cell_wrapped[c_idx]):
                        ty = round(row_y0 + pad_y + ln_idx * cell_line_h, 2)
                        tw = min(cw - 2 * pad_x, max(6.0, _text_width_pt(ln_txt, font, measure_scale)))
                        cur_page["draws"].append({
                            "x": round(cell_x0 + pad_x, 2),
                            "y": ty,
                            "text": ln_txt,
                            "font_pt": cell_font_pt,
                            "bold": is_header,
                        })
                        cur_page["lines"].append({
                            "page": cur_page["page"],
                            "block": block_index,
                            "row": r_idx,
                            "col": c_idx,
                            "text": ln_txt,
                            "bbox": [
                                round(cell_x0 + pad_x, 2),
                                ty,
                                round(min(cell_x1 - 2.0, cell_x0 + pad_x + tw + 3.0), 2),
                                round(ty + cell_line_h, 2),
                            ],
                            "words": _word_boxes(ln_txt, round(cell_x0 + pad_x, 2), ty,
                                                 round(ty + cell_line_h, 2), font, measure_scale),
                        })
                    x_cursor += cw
                cur_y = row_y1
            cur_y += 6.0
            block_index += 1

    try:
        notes = _footnotes(path)
    except Exception:
        notes = {}
    if notes:
        note_font_pt = 8.5
        note_line_h = round(note_font_pt * 1.38, 2)
        note_font = fonts[(note_font_pt, False)]
        for note_id, note_text in notes.items():
            if not note_text:
                continue
            display = f"[{note_id}] {_docx_display_text(note_text)}"
            for line_text in _wrap_text_to_width(display, _DOCX_CONTENT_W, note_font, measure_scale):
                ensure_space(note_line_h)
                y0 = round(cur_y, 2)
                tw = max(8.0, min(_DOCX_CONTENT_W, _text_width_pt(line_text, note_font, measure_scale)))
                cur_page["draws"].append({
                    "x": _DOCX_MARGIN_X,
                    "y": y0,
                    "text": line_text,
                    "font_pt": note_font_pt,
                    "bold": False,
                })
                cur_page["lines"].append({
                    "page": cur_page["page"],
                    "block": block_index,
                    "text": line_text,
                    "bbox": [_DOCX_MARGIN_X, y0, round(_DOCX_MARGIN_X + tw + 3.0, 2), round(y0 + note_line_h, 2)],
                    "words": _word_boxes(line_text, _DOCX_MARGIN_X, y0, round(y0 + note_line_h, 2),
                                         note_font, measure_scale),
                })
                cur_y += note_line_h
            block_index += 1

    if len(_DOCX_LAYOUT_CACHE) >= _DOCX_LAYOUT_CACHE_MAX:
        _DOCX_LAYOUT_CACHE.pop(next(iter(_DOCX_LAYOUT_CACHE)))
    _DOCX_LAYOUT_CACHE[cache_key] = pages
    return pages


def _pdfium_page_sizes(path: Path) -> list[dict[str, Any]]:
    import pypdfium2 as pdfium

    pages: list[dict[str, Any]] = []
    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    w, h = page.get_size()
                    pages.append({"page": index + 1, "width": round(float(w), 1),
                                  "height": round(float(h), 1), "rotation": int(page.get_rotation())})
                finally:
                    page.close()
        finally:
            pdf.close()
    return pages


def get_original_info(path: Path | None, media_type: str) -> dict[str, Any]:
    """Return page count and page dimensions for PDF and DOCX original documents."""
    if media_type == "docx" and path is not None and path.is_file():
        layout = _build_docx_paginated_layout(path)
        if layout:
            return {
                "available": True,
                "media_type": "docx",
                "page_count": len(layout),
                "pages": [
                    {
                        "page": p["page"],
                        "width": round(float(p["width"]), 1),
                        "height": round(float(p["height"]), 1),
                        "rotation": 0,
                    }
                    for p in layout
                ],
                "supports_bbox": True,
            }
        return {
            "available": True,
            "media_type": "docx",
            "page_count": 1,
            "pages": [{"page": 1, "width": _DOCX_PAGE_W, "height": _DOCX_PAGE_H, "rotation": 0}],
            "supports_bbox": False,
        }

    if media_type != "pdf" or path is None or not path.is_file():
        return {
            "media_type": media_type,
            "page_count": 1,
            "pages": [{"page": 1, "width": 800.0, "height": 1100.0, "rotation": 0}],
            "supports_bbox": media_type == "pdf",
        }

    pages: list[dict[str, Any]] = []
    try:
        pages = _pdfium_page_sizes(path)
    except Exception:
        pages = []

    if not pages:
        try:
            import pdfplumber

            with pdfplumber.open(str(path)) as pdf:
                for index, p in enumerate(pdf.pages, start=1):
                    pages.append({
                        "page": index,
                        "width": round(float(p.width), 1),
                        "height": round(float(p.height), 1),
                        "rotation": int(getattr(p, "rotation", 0) or 0),
                    })
        except Exception:
            pages = [{"page": 1, "width": 595.0, "height": 842.0, "rotation": 0}]

    return {
        "available": True,
        "media_type": "pdf",
        "page_count": len(pages),
        "pages": pages,
        "supports_bbox": True,
    }


def render_original_docx_page(path: Path, page_number: int, scale: float = 1.75) -> bytes:
    """Render a 1-based page from a paginated DOCX original file to PNG bytes."""
    if not path.is_file():
        raise FileNotFoundError("Оригинал документа не найден.")
    if page_number < 1:
        raise ValueError("Некорректный номер страницы.")

    from PIL import Image, ImageDraw

    layout = _build_docx_paginated_layout(path)
    if layout is None:
        if page_number > 1:
            raise ValueError(f"Страница {page_number} отсутствует в документе (всего 1).")
        img = Image.new("RGB", (int(_DOCX_PAGE_W * scale), int(_DOCX_PAGE_H * scale)), (252, 252, 253))
        draw = ImageDraw.Draw(img)
        draw.rectangle((16, 16, img.width - 16, img.height - 16), outline=(185, 195, 210), width=2)
        draw.text((40, 40), f"DOCX Page {page_number}", fill=(40, 50, 70), font=_load_font(int(11 * scale)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    if page_number > len(layout):
        raise ValueError(f"Страница {page_number} отсутствует в документе (всего {len(layout)}).")

    page_data = layout[page_number - 1]
    width_px = max(320, int(round(page_data["width"] * scale)))
    height_px = max(440, int(round(page_data["height"] * scale)))
    img = Image.new("RGB", (width_px, height_px), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Subtle page number footer for orientation
    footer_font = _load_font(max(8, int(8.5 * scale)), bold=False)
    footer_label = f"Стр. {page_number} из {len(layout)}"
    draw.text(
        (width_px / 2.0 - 30 * scale, (page_data["height"] - 24.0) * scale),
        footer_label,
        fill=(120, 130, 145),
        font=footer_font,
    )

    for rect in page_data["rects"]:
        x0, y0, x1, y1 = (v * scale for v in rect["bbox"])
        if rect.get("fill") is not None:
            draw.rectangle((x0, y0, x1, y1), fill=rect["fill"])
        if rect.get("outline") is not None:
            draw.rectangle((x0, y0, x1, y1), outline=rect["outline"], width=max(1, int(round(scale * 0.7))))

    render_fonts: dict[tuple[float, bool], Any] = {}
    for item in page_data["draws"]:
        key = (item["font_pt"], item["bold"])
        if key not in render_fonts:
            render_fonts[key] = _load_font(max(9, int(round(item["font_pt"] * scale))), bold=item["bold"])
        draw.text(
            (item["x"] * scale, item["y"] * scale),
            item["text"],
            fill=(18, 24, 38),
            font=render_fonts[key],
        )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_original_pdf_page(path: Path, page_number: int, scale: float = 1.75) -> bytes:
    """Render a 1-based PDF or DOCX page directly from the original file to PNG bytes."""
    if not path.is_file():
        raise FileNotFoundError("Оригинал документа не найден.")
    if page_number < 1:
        raise ValueError("Некорректный номер страницы.")
    if path.suffix.lower() == ".docx":
        return render_original_docx_page(path, page_number, scale=scale)

    try:
        import pypdfium2 as pdfium

        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                if page_number > len(pdf):
                    raise ValueError(f"Страница {page_number} отсутствует в документе (всего {len(pdf)}).")
                page = pdf[page_number - 1]
                try:
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            buf = io.BytesIO()
                            image.save(buf, format="PNG")
                            return buf.getvalue()
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            finally:
                pdf.close()
    except ValueError:
        raise
    except Exception:
        pass

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            if page_number > len(pdf.pages):
                raise ValueError(f"Страница {page_number} отсутствует в документе (всего {len(pdf.pages)}).")
            page = pdf.pages[page_number - 1]
            with PDFIUM_LOCK:
                image = page.to_image(resolution=int(72 * scale), force_mediabox=True).original
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            return buf.getvalue()
    except ValueError:
        raise
    except Exception:
        if path.suffix.lower() == ".docx":
            return render_original_docx_page(path, page_number, scale=scale)
        # Synthetic placeholder PNG only if the file on disk is a non-PDF byte fixture in UI tests.
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (800, 1100), (250, 250, 252))
        draw = ImageDraw.Draw(img)
        draw.rectangle((24, 24, 776, 1076), outline=(180, 190, 205), width=2)
        draw.text((48, 48), f"Original PDF Page {page_number}", fill=(40, 50, 70))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def _format_box(
    page: int,
    bbox: list[float],
    page_width: float,
    page_height: float,
    text: str = "",
) -> dict[str, Any]:
    x0, y0, x1, y1 = bbox
    pw = max(page_width, 1.0)
    ph = max(page_height, 1.0)
    left = max(0.0, min(1.0, x0 / pw))
    top = max(0.0, min(1.0, y0 / ph))
    width = max(0.002, min(1.0 - left, (x1 - x0) / pw))
    height = max(0.002, min(1.0 - top, (y1 - y0) / ph))
    return {
        "page": page,
        "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
        "norm": {
            "left": round(left, 6),
            "top": round(top, 6),
            "width": round(width, 6),
            "height": round(height, 6),
        },
        "text": text,
    }


def _queries(quote: str | None, body: str) -> list[str]:
    """Quote first, then the whole fragment; cleaned of synthetic table/outline markers."""
    seen: list[str] = []
    for raw in ((quote or "").strip(), body):
        cleaned = _clean_query_for_matching(raw)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _align_lines(lines: list[dict[str, Any]], queries: list[str], near: list[float] | None = None):
    """Return (boxes, coverage) for the first query that aligns inside ``lines``.

    ``lines`` carry page-local viewer-frame boxes and, when known, per-word boxes.
    The result is a list of ``(page, bbox, text)``.
    """
    tokens = textmatch.tokens_for_lines(lines)
    if not tokens:
        return None
    for query in queries:
        span = textmatch.align(tokens, query, near=near)
        if span is None or not span.tokens:
            continue
        result = []
        by_line: dict[int, list[textmatch.Token]] = {}
        for token in span.tokens:
            by_line.setdefault(token.line, []).append(token)
        for line_index in sorted(by_line):
            page = int(lines[line_index].get("page") or 0)
            part = textmatch.Span(tokens=by_line[line_index], coverage=span.coverage, exact=span.exact)
            for box, text in textmatch.line_boxes(part):
                result.append((page, box, text))
        return result, span.coverage
    return None


def _resolve_docx_highlight(
    path: Path | None,
    info: dict[str, Any],
    fragment: Fragment,
    quote: str | None,
) -> dict[str, Any]:
    """Compute per-word highlight boxes on the paginated DOCX layout."""
    frag_lines_raw = fragment.text.splitlines()
    frag_body = "\n".join(frag_lines_raw[1:]) if (len(frag_lines_raw) > 1 and " > " in frag_lines_raw[0]) else fragment.text
    queries = _queries(quote, frag_body)

    layout = _build_docx_paginated_layout(path)
    if not layout:
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": "docx",
            "page": 1,
            "page_count": info["page_count"],
            "page_width": info["pages"][0]["width"],
            "page_height": info["pages"][0]["height"],
            "exact": False,
            "boxes": [],
            "notice": "Оригинал DOCX недоступен для постраничной разметки; показан исходный текст.",
        }

    all_lines = [ln for p in layout for ln in p["lines"]]
    target_block = getattr(fragment.locator, "block", None)

    matched: tuple[list[tuple[int, list[float], str]], float] | None = None
    scopes: list[list[dict[str, Any]]] = []
    if target_block is not None:
        scopes.append([ln for ln in all_lines if ln.get("block") == target_block])
        scopes.append([ln for ln in all_lines if int(ln.get("block") or 0) >= int(target_block)])
    scopes.append(all_lines)
    for scope in scopes:
        if scope:
            matched = _align_lines(scope, queries)
            if matched:
                break

    if matched:
        entries, _coverage = matched
        page_counts: dict[int, int] = {}
        for page, _box, _text in entries:
            page_counts[page] = page_counts.get(page, 0) + 1
        target_page = min(page for page in page_counts)
        page_meta = layout[max(0, min(len(layout) - 1, target_page - 1))]
        boxes = []
        for page, box, text in entries:
            meta = layout[max(0, min(len(layout) - 1, page - 1))]
            boxes.append(_format_box(page, box, float(meta["width"]), float(meta["height"]), text))
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": "docx",
            "page": target_page,
            "pages": sorted(page_counts),
            "page_count": len(layout),
            "page_width": float(page_meta["width"]),
            "page_height": float(page_meta["height"]),
            "exact": True,
            "boxes": boxes,
            "notice": None,
        }

    # Estimate landing page from target_block if available so the user still lands near the section.
    fallback_page = 1
    if target_block is not None:
        for ln in all_lines:
            if int(ln.get("block") or 0) >= int(target_block):
                fallback_page = int(ln.get("page") or 1)
                break
    page_meta = layout[max(0, min(len(layout) - 1, fallback_page - 1))]
    return {
        "document_id": fragment.document_id,
        "fragment_id": fragment.id,
        "media_type": "docx",
        "page": fallback_page,
        "page_count": len(layout),
        "page_width": float(page_meta["width"]),
        "page_height": float(page_meta["height"]),
        "exact": False,
        "boxes": [],
        "notice": f"Точное положение фрагмента на странице определить невозможно; открыта страница {fallback_page} оригинала DOCX.",
    }


def _stored_lines(blocks: list[dict[str, Any]], page: int, frame: PageFrame, region: list[float] | None):
    """Saved extraction lines of ``page`` in viewer-frame coordinates (optionally inside region)."""
    out: list[dict[str, Any]] = []
    for blk in blocks:
        if int(blk.get("page") or 0) != page:
            continue
        for ln in blk.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            lbox = _valid_box(ln.get("bbox"))
            lbox = frame.to_viewer(lbox) if lbox else None
            if lbox is None:
                continue
            if region is not None and not _box_overlaps(lbox, region, pad=1.0):
                continue
            words = []
            for word in ln.get("words") or []:
                wbox = _valid_box(word.get("bbox")) if isinstance(word, dict) else None
                wbox = frame.to_viewer(wbox) if wbox else None
                if wbox is not None:
                    words.append({"text": str(word.get("text") or ""), "bbox": wbox})
            out.append({"text": str(ln.get("text") or ""), "bbox": lbox, "words": words, "page": page})
    return out


def _pdf_page_geometry(path: Path | None, info: dict[str, Any], blocks: list[dict[str, Any]], page: int):
    """(frame, width, height) of a PDF page in the viewer frame, with safe fallbacks."""
    frame = page_frame(path, page)
    if frame is not None:
        return frame, frame.width, frame.height
    meta = next((p for p in info["pages"] if p["page"] == page), None)
    size = next((b["page_size"] for b in blocks if int(b.get("page") or 0) == page
                 and isinstance(b.get("page_size"), (list, tuple)) and len(b["page_size"]) == 2), None)
    if meta is not None and path is not None and path.is_file():
        width, height = float(meta["width"]), float(meta["height"])
    elif size is not None:
        width, height = float(size[0]), float(size[1])
    elif meta is not None:
        width, height = float(meta["width"]), float(meta["height"])
    else:
        width, height = 595.0, 842.0
    return PageFrame(width=width, height=height), width, height


def _store_frame(blocks: list[dict[str, Any]], page: int, frame: PageFrame) -> PageFrame:
    """Frame of the *saved* coordinates of ``page``.

    Text-layer blocks are in pdfplumber's MediaBox frame (``frame`` converts them); OCR
    blocks are tagged ``frame="viewer"`` because OCR renders what the reader sees.
    """
    if any(b.get("frame") == "viewer" for b in blocks if int(b.get("page") or 0) == page):
        return PageFrame(width=frame.width, height=frame.height)
    return frame


def _pdf_result(fragment: Fragment, info: dict[str, Any], page: int, width: float, height: float,
                *, exact: bool, boxes: list[dict[str, Any]], notice: str | None) -> dict[str, Any]:
    return {
        "document_id": fragment.document_id,
        "fragment_id": fragment.id,
        "media_type": "pdf",
        "page": page,
        "pages": sorted({int(b["page"]) for b in boxes}) or [page],
        "page_count": max(info["page_count"], page),
        "page_width": width,
        "page_height": height,
        "exact": exact,
        "boxes": boxes,
        "notice": notice,
    }


def _pdf_page_boxes(path, info, fragment, blocks, page, queries, frag_bbox_ext, *, use_region: bool):
    """Try to place the queries on one page. Returns (frame, w, h, entries) or None."""
    frame, width, height = _pdf_page_geometry(path, info, blocks, page)
    store = _store_frame(blocks, page, frame)
    region = store.to_viewer(frag_bbox_ext) if (use_region and frag_bbox_ext) else None
    page_blocks = [b for b in blocks if int(b.get("page") or 0) == page]
    ocr_page = any(b.get("ocr") for b in page_blocks)
    real_pdf = path is not None and path.is_file() and path.suffix.lower() == ".pdf" and page_frame(path, page) is not None

    def native(scoped: bool):
        words = native_words(path, page, frame, region=region if scoped else None)
        return native_lines(words)

    attempts = []
    if real_pdf and not ocr_page:
        if region is not None:
            attempts.append(lambda: native(True))
        attempts.append(lambda: native(False))
    attempts.append(lambda: _stored_lines(blocks, page, store, region))
    if region is not None:
        attempts.append(lambda: _stored_lines(blocks, page, store, None))
    if real_pdf and ocr_page:
        attempts.append(lambda: native(False))

    for build in attempts:
        lines = [{**ln, "page": page} for ln in build()]
        if not lines:
            continue
        found = _align_lines(lines, queries, near=region)
        if found:
            return frame, width, height, found[0]
    return None


def resolve_fragment_highlight(
    path: Path | None,
    media_type: str,
    fragment: Fragment,
    blocks: list[dict[str, Any]] | None,
    quote: str | None = None,
) -> dict[str, Any]:
    """Compute exact per-word highlight coordinates on the original document page.

    Coordinates are produced in the *viewer frame* (what the page image shows), so they
    stay correct for CropBox, /Rotate and offset MediaBox PDFs; the browser only has to
    scale the normalized fractions with the image.
    """
    info = get_original_info(path, media_type)
    if media_type == "docx":
        return _resolve_docx_highlight(path, info, fragment, quote)

    if media_type != "pdf" or fragment.locator.kind != "pdf":
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": media_type,
            "page": 1,
            "page_count": info["page_count"],
            "page_width": info["pages"][0]["width"],
            "page_height": info["pages"][0]["height"],
            "exact": False,
            "boxes": [],
            "notice": (
                "Точное координатное выделение поверх страниц поддерживается только для PDF и DOCX оригиналов; "
                "показан исходный фрагмент текста."
            ),
        }

    blocks = blocks or []
    target_page = int(fragment.locator.page)

    frag_lines_raw = fragment.text.splitlines()
    frag_body = "\n".join(frag_lines_raw[1:]) if (len(frag_lines_raw) > 1 and " > " in frag_lines_raw[0]) else fragment.text
    queries = _queries(quote, frag_body)
    frag_bbox = _valid_box(fragment.locator.bbox)

    frame, width, height = _pdf_page_geometry(path, info, blocks, target_page)

    # 1. Word-level alignment on the cited page (scoped to the fragment's own block first).
    placed = _pdf_page_boxes(path, info, fragment, blocks, target_page, queries, frag_bbox, use_region=True)
    page = target_page
    # 2. Quote crossing a page boundary / locator off by one page, when no block coordinates pin it.
    if placed is None and frag_bbox is None:
        for adjacent in (target_page + 1, target_page - 1):
            if 1 <= adjacent <= info["page_count"]:
                placed = _pdf_page_boxes(path, info, fragment, blocks, adjacent, queries, None, use_region=False)
                if placed is not None:
                    page = adjacent
                    break

    if placed is not None:
        frame, width, height, entries = placed
        boxes = [_format_box(page, box, width, height, text) for _, box, text in entries]
        return _pdf_result(fragment, info, page, width, height, exact=True, boxes=boxes, notice=None)

    # 3. Reviewer-edited text no longer aligns word-for-word: the fragment *is* its block,
    #    so highlight the block's own saved lines instead of guessing across the page.
    store = _store_frame(blocks, target_page, frame)
    region = store.to_viewer(frag_bbox) if frag_bbox else None
    if region is not None:
        own = _stored_lines(blocks, target_page, store, region)
        if own:
            boxes = [_format_box(target_page, ln["bbox"], width, height, ln["text"]) for ln in own]
            return _pdf_result(fragment, info, target_page, width, height, exact=True, boxes=boxes, notice=None)

    # 4. Block-level / locator-level bbox (single box when coordinates genuinely exist).
    q_words = set(textmatch.words_of(queries[0])) if queries else set()
    matched_blocks: list[tuple[list[float], str]] = []
    for blk in blocks:
        if int(blk.get("page") or 0) != target_page:
            continue
        bbox = _valid_box(blk.get("bbox"))
        bbox = store.to_viewer(bbox) if bbox else None
        if bbox is None or (region is not None and not _box_overlaps(bbox, region)):
            continue
        blk_text = blk.get("text") or ""
        if not blk_text and blk.get("table"):
            blk_text = "\n".join(" ".join(str(c) for c in row) for row in blk["table"])
        blk_words = set(textmatch.words_of(blk_text))
        overlap = q_words & blk_words
        if q_words and len(overlap) / len(q_words) >= 0.45 and len(overlap) >= min(2, len(q_words)):
            matched_blocks.append((bbox, blk_text))
    if matched_blocks:
        boxes = [_format_box(target_page, bbox, width, height, text) for bbox, text in matched_blocks]
        return _pdf_result(fragment, info, target_page, width, height, exact=True, boxes=boxes, notice=None)

    if region is not None:
        return _pdf_result(fragment, info, target_page, width, height, exact=True,
                           boxes=[_format_box(target_page, region, width, height, frag_body)], notice=None)

    # 5. Honest fallback when coordinates do not exist anywhere: NEVER draw a fake highlight.
    return _pdf_result(
        fragment, info, target_page, width, height, exact=False, boxes=[],
        notice=f"Точное положение фрагмента на странице определить невозможно; открыта страница {target_page} оригинала.",
    )
