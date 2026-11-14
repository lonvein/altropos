"""Rendering original PDF/DOCX pages and resolving multi-line fragment highlight boxes."""
from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

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
        ]
        if bold
        else [
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\calibri.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
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
                })
                cur_y += note_line_h
            block_index += 1

    if len(_DOCX_LAYOUT_CACHE) >= _DOCX_LAYOUT_CACHE_MAX:
        _DOCX_LAYOUT_CACHE.pop(next(iter(_DOCX_LAYOUT_CACHE)))
    _DOCX_LAYOUT_CACHE[cache_key] = pages
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
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    w, h = page.get_size()
                    rot = page.get_rotation()
                    pages.append({
                        "page": index + 1,
                        "width": round(float(w), 1),
                        "height": round(float(h), 1),
                        "rotation": int(rot),
                    })
                finally:
                    page.close()
        finally:
            pdf.close()
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


def _match_lines_for_query(
    lines: list[dict[str, Any]],
    query_text: str,
) -> list[dict[str, Any]]:
    """Find contiguous or high-overlap line boxes matching query_text."""
    if not lines or not query_text:
        return []

    target = _norm(_clean_query_for_matching(query_text))
    if not target:
        return []

    # 1. Contiguous span alignment across normalized line texts.
    spans: list[tuple[int, int, int]] = []
    joined_parts: list[str] = []
    cursor = 0
    for idx, ln in enumerate(lines):
        norm_ln = _norm(ln.get("text", ""))
        if not norm_ln:
            continue
        if joined_parts:
            joined_parts.append(" ")
            cursor += 1
        start = cursor
        joined_parts.append(norm_ln)
        cursor += len(norm_ln)
        spans.append((idx, start, cursor))
    joined = "".join(joined_parts)

    pos = joined.find(target)
    if pos == -1 and len(target) > 40:
        # Try matching normalized lines from a multi-line query.
        q_lines = [_norm(_clean_query_for_matching(part)) for part in query_text.splitlines()]
        q_lines = [part for part in q_lines if len(part) >= 8]
        matched_indices: set[int] = set()
        for q_ln in q_lines:
            q_pos = joined.find(q_ln)
            if q_pos != -1:
                q_end = q_pos + len(q_ln)
                for idx, s, e in spans:
                    if e > q_pos and s < q_end:
                        matched_indices.add(idx)
        if matched_indices:
            return [lines[i] for i in sorted(matched_indices)]

    if pos != -1:
        end = pos + len(target)
        matched = [lines[idx] for idx, s, e in spans if e > pos and s < end]
        if matched:
            return matched

    # 2. Token overlap fallback restricted to lines with meaningful lexical match.
    q_words = _words(target)
    if not q_words:
        return []
    matched = []
    for ln in lines:
        ln_words = _words(ln.get("text", ""))
        if not ln_words:
            continue
        overlap = q_words & ln_words
        if len(overlap) >= min(2, len(ln_words)) and (len(overlap) / len(ln_words) >= 0.45 or len(overlap) >= 3):
            matched.append(ln)
    return matched


def _pdfplumber_page_lines(path: Path | None, page_number: int) -> list[dict[str, Any]]:
    if path is None or not path.is_file():
        return []
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                return []
            page = pdf.pages[page_number - 1]
            ox, oy = page.bbox[:2]
            pw, ph = float(page.width), float(page.height)
            out: list[dict[str, Any]] = []
            for ln in page.extract_text_lines(layout=False, strip=True, return_chars=False):
                text = (ln.get("text") or "").strip()
                if not text:
                    continue
                x0 = max(0.0, round(float(ln["x0"]) - ox, 2))
                y0 = max(0.0, round(float(ln["top"]) - oy, 2))
                x1 = min(round(pw, 2), round(float(ln["x1"]) - ox, 2))
                y1 = min(round(ph, 2), round(float(ln["bottom"]) - oy, 2))
                if x1 > x0 and y1 > y0:
                    out.append({"text": text, "bbox": [x0, y0, x1, y1]})
            return out
    except Exception:
        return []


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


def _resolve_docx_highlight(
    path: Path | None,
    info: dict[str, Any],
    fragment: Fragment,
    quote: str | None,
) -> dict[str, Any]:
    """Compute per-line highlight boxes on the paginated DOCX layout."""
    frag_lines_raw = fragment.text.splitlines()
    frag_body = "\n".join(frag_lines_raw[1:]) if (len(frag_lines_raw) > 1 and " > " in frag_lines_raw[0]) else fragment.text
    query = (quote or "").strip() or frag_body

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

    matched_lines: list[dict[str, Any]] = []
    if target_block is not None:
        block_lines = [ln for ln in all_lines if ln.get("block") == target_block]
        matched_lines = _match_lines_for_query(block_lines, query)
        if not matched_lines and query != frag_body:
            matched_lines = _match_lines_for_query(block_lines, frag_body)

    if not matched_lines and target_block is not None:
        forward_lines = [ln for ln in all_lines if int(ln.get("block") or 0) >= int(target_block)]
        matched_lines = _match_lines_for_query(forward_lines, query)
        if not matched_lines and query != frag_body:
            matched_lines = _match_lines_for_query(forward_lines, frag_body)

    if not matched_lines:
        matched_lines = _match_lines_for_query(all_lines, query)
        if not matched_lines and query != frag_body:
            matched_lines = _match_lines_for_query(all_lines, frag_body)

    if matched_lines:
        # Choose the page containing the highest concentration of matched lines (tie-break: earliest page).
        page_counts: dict[int, int] = {}
        for ln in matched_lines:
            pnum = int(ln.get("page") or 1)
            page_counts[pnum] = page_counts.get(pnum, 0) + 1
        target_page = max(page_counts.items(), key=lambda item: (item[1], -item[0]))[0]
        page_meta = layout[max(0, min(len(layout) - 1, target_page - 1))]
        pw = float(page_meta["width"])
        ph = float(page_meta["height"])
        page_matched = [ln for ln in matched_lines if int(ln.get("page") or 1) == target_page]
        boxes = [
            _format_box(target_page, ln["bbox"], pw, ph, ln.get("text", ""))
            for ln in page_matched
        ]
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": "docx",
            "page": target_page,
            "page_count": len(layout),
            "page_width": pw,
            "page_height": ph,
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


def resolve_fragment_highlight(
    path: Path | None,
    media_type: str,
    fragment: Fragment,
    blocks: list[dict[str, Any]] | None,
    quote: str | None = None,
) -> dict[str, Any]:
    """Compute exact per-line highlight coordinates on the original document page."""
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

    target_page = int(fragment.locator.page)
    page_meta = next((p for p in info["pages"] if p["page"] == target_page), None)
    page_blocks = [b for b in (blocks or []) if int(b.get("page") or 0) == target_page]

    # Prefer block page_size if original PDF couldn't be read directly (e.g. unit fixtures).
    block_page_size = next(
        (b["page_size"] for b in page_blocks if isinstance(b.get("page_size"), (list, tuple)) and len(b["page_size"]) == 2),
        None,
    )
    if page_meta is not None and path is not None and path.is_file():
        page_width = float(page_meta["width"])
        page_height = float(page_meta["height"])
    elif block_page_size is not None:
        page_width = float(block_page_size[0])
        page_height = float(block_page_size[1])
    elif page_meta is not None:
        page_width = float(page_meta["width"])
        page_height = float(page_meta["height"])
    else:
        page_width, page_height = 595.0, 842.0

    # Strip injected outline header prefix ("Heading > Subheading\n") when matching body lines.
    frag_lines_raw = fragment.text.splitlines()
    frag_body = "\n".join(frag_lines_raw[1:]) if (len(frag_lines_raw) > 1 and " > " in frag_lines_raw[0]) else fragment.text
    query = (quote or "").strip() or frag_body

    frag_bbox = _valid_box(fragment.locator.bbox)

    # Filter blocks overlapping the fragment bbox when available so repeated text on page doesn't false-match.
    scoped_blocks = page_blocks
    if frag_bbox:
        overlapping = [
            b for b in page_blocks
            if (bb := _valid_box(b.get("bbox"))) is not None and _box_overlaps(bb, frag_bbox)
        ]
        if overlapping:
            scoped_blocks = overlapping

    # 1. Try per-line coordinates already saved in extraction blocks.
    candidate_lines: list[dict[str, Any]] = []
    for blk in scoped_blocks:
        for ln in blk.get("lines") or []:
            if isinstance(ln, dict) and (lbox := _valid_box(ln.get("bbox"))) is not None:
                candidate_lines.append({"text": str(ln.get("text") or ""), "bbox": lbox})

    matched_lines = _match_lines_for_query(candidate_lines, query)
    if not matched_lines and query != frag_body:
        matched_lines = _match_lines_for_query(candidate_lines, frag_body)
    # If quote/fragment matches the whole scoped block and candidate_lines are all within frag_bbox:
    if not matched_lines and candidate_lines and frag_bbox:
        matched_lines = [ln for ln in candidate_lines if _box_overlaps(ln["bbox"], frag_bbox)]

    # 2. If saved blocks didn't carry per-line boxes, inspect original PDF text lines via pdfplumber.
    if not matched_lines:
        native_lines = _pdfplumber_page_lines(path, target_page)
        if frag_bbox:
            scoped_native = [ln for ln in native_lines if _box_overlaps(ln["bbox"], frag_bbox)]
        else:
            scoped_native = native_lines
        matched_lines = _match_lines_for_query(scoped_native, query)
        if not matched_lines and query != frag_body:
            matched_lines = _match_lines_for_query(scoped_native, frag_body)
        if not matched_lines and frag_bbox and scoped_native:
            matched_lines = scoped_native

    # 2b. Check adjacent pages if quote spilled across a page boundary and nothing matched on target_page.
    if not matched_lines and not frag_bbox and path is not None and path.is_file():
        for adj_page in (target_page + 1, target_page - 1):
            if 1 <= adj_page <= info["page_count"]:
                adj_blocks = [b for b in (blocks or []) if int(b.get("page") or 0) == adj_page]
                adj_candidates = [
                    {"text": str(ln.get("text") or ""), "bbox": lbox}
                    for blk in adj_blocks
                    for ln in (blk.get("lines") or [])
                    if isinstance(ln, dict) and (lbox := _valid_box(ln.get("bbox"))) is not None
                ]
                adj_matched = _match_lines_for_query(adj_candidates, query) or _pdfplumber_page_lines(path, adj_page)
                if adj_candidates and not adj_matched:
                    adj_matched = []
                elif not adj_candidates:
                    adj_matched = _match_lines_for_query(adj_matched, query)
                if adj_matched:
                    target_page = adj_page
                    adj_meta = next((p for p in info["pages"] if p["page"] == adj_page), None)
                    if adj_meta is not None:
                        page_width = float(adj_meta["width"])
                        page_height = float(adj_meta["height"])
                    matched_lines = adj_matched
                    break

    if matched_lines:
        boxes = [
            _format_box(target_page, ln["bbox"], page_width, page_height, ln.get("text", ""))
            for ln in matched_lines
        ]
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": "pdf",
            "page": target_page,
            "page_count": max(info["page_count"], target_page),
            "page_width": page_width,
            "page_height": page_height,
            "exact": True,
            "boxes": boxes,
            "notice": None,
        }

    # 3. Block-level / locator-level bbox fallback (single box when coordinates genuinely exist).
    matched_blocks: list[dict[str, Any]] = []
    for blk in scoped_blocks:
        bbox = _valid_box(blk.get("bbox"))
        if bbox is None:
            continue
        blk_text = blk.get("text") or ""
        if not blk_text and blk.get("table"):
            blk_text = "\n".join(" ".join(str(c) for c in row) for row in blk["table"])
        blk_norm = _norm(blk_text)
        blk_words = _words(blk_text)
        q_words = _words(_clean_query_for_matching(query))
        overlap = q_words & blk_words
        if (blk_norm and _norm(_clean_query_for_matching(query)) in blk_norm) or (
            len(overlap) >= min(2, len(q_words)) and q_words and len(overlap) / len(q_words) >= 0.45
        ):
            matched_blocks.append({"text": blk_text, "bbox": bbox})

    if matched_blocks:
        boxes = [
            _format_box(target_page, mb["bbox"], page_width, page_height, mb["text"])
            for mb in matched_blocks
        ]
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": "pdf",
            "page": target_page,
            "page_count": max(info["page_count"], target_page),
            "page_width": page_width,
            "page_height": page_height,
            "exact": True,
            "boxes": boxes,
            "notice": None,
        }

    if frag_bbox:
        return {
            "document_id": fragment.document_id,
            "fragment_id": fragment.id,
            "media_type": "pdf",
            "page": target_page,
            "page_count": max(info["page_count"], target_page),
            "page_width": page_width,
            "page_height": page_height,
            "exact": True,
            "boxes": [_format_box(target_page, frag_bbox, page_width, page_height, frag_body)],
            "notice": None,
        }

    # 4. Honest fallback when coordinates do not exist anywhere: NEVER draw a fake highlight.
    return {
        "document_id": fragment.document_id,
        "fragment_id": fragment.id,
        "media_type": "pdf",
        "page": target_page,
        "page_count": max(info["page_count"], target_page),
        "page_width": page_width,
        "page_height": page_height,
        "exact": False,
        "boxes": [],
        "notice": f"Точное положение фрагмента на странице определить невозможно; открыта страница {target_page} оригинала.",
    }
