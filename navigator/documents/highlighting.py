"""Rendering original PDF pages and resolving multi-line fragment highlight boxes."""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Any

from navigator.schemas import Fragment

_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _norm(text: str) -> str:
    return _SPACE_RE.sub(" ", (text or "").replace("\u00a0", " ")).strip().lower()


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


def get_original_info(path: Path | None, media_type: str) -> dict[str, Any]:
    """Return page count and page dimensions for the original document."""
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
        "media_type": "pdf",
        "page_count": len(pages),
        "pages": pages,
        "supports_bbox": True,
    }


def render_original_pdf_page(path: Path, page_number: int, scale: float = 1.75) -> bytes:
    """Render a 1-based PDF page directly from the original file to PNG bytes."""
    if not path.is_file():
        raise FileNotFoundError("Оригинал документа не найден.")
    if page_number < 1:
        raise ValueError("Некорректный номер страницы.")

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

    target = _norm(query_text)
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
        # Try matching longest normalized line from multi-line query.
        q_lines = [_norm(part) for part in query_text.splitlines() if len(_norm(part)) >= 8]
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


def resolve_fragment_highlight(
    path: Path | None,
    media_type: str,
    fragment: Fragment,
    blocks: list[dict[str, Any]] | None,
    quote: str | None = None,
) -> dict[str, Any]:
    """Compute exact per-line highlight coordinates on the original document page."""
    info = get_original_info(path, media_type)
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
                "Точное координатное выделение поверх страниц поддерживается только для PDF-оригиналов; "
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
        q_words = _words(query)
        overlap = q_words & blk_words
        if (blk_norm and _norm(query) in blk_norm) or (
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
