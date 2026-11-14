"""Highlight boxes must land on the printed text as the *viewer* renders it.

The viewer shows pdfium's rendering (CropBox, /Rotate applied) while extraction and OCR work in
pdfplumber's MediaBox frame. These tests compare highlight boxes with real rendered pixels.
"""
from __future__ import annotations

import io

import numpy as np
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from navigator.documents import textmatch
from navigator.documents.highlighting import resolve_fragment_highlight
from navigator.documents.pdf_geometry import frame_from_boxes, page_frame
from navigator.ingestion.pdf_parser import parse_pdf
from navigator.schemas import Fragment

CASES = {
    "plain": dict(mediabox=(0, 0, 600, 800)),
    "mediabox-offset": dict(mediabox=(20, 30, 620, 830)),
    "cropbox": dict(mediabox=(0, 0, 600, 800), cropbox=(50, 100, 450, 700)),
    "offset+crop": dict(mediabox=(20, 30, 620, 830), cropbox=(70, 130, 470, 730)),
    "rotate90": dict(mediabox=(0, 0, 600, 800), rotate=90),
    "rotate90+crop": dict(mediabox=(0, 0, 600, 800), cropbox=(50, 100, 450, 700), rotate=90),
    "rotate180+crop": dict(mediabox=(0, 0, 600, 800), cropbox=(50, 100, 450, 700), rotate=180),
    "rotate270+offset+crop": dict(mediabox=(20, 30, 620, 830), cropbox=(70, 130, 470, 730), rotate=270),
}


def _user_point(rotation, u, v, media):
    """Inverse of the viewer mapping: MediaBox display coords (u, v) -> PDF user space."""
    mx0, my0, mx1, my1 = media
    if rotation == 90:
        return mx0 + v, my0 + u
    if rotation == 180:
        return mx1 - u, my0 + v
    if rotation == 270:
        return mx1 - v, my1 - u
    return mx0 + u, my1 - v


_TM = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}


def _pdf(mediabox, cropbox=None, rotate=0, lines=()):
    """Upright-as-displayed Helvetica lines; (u, v) is the baseline start in *viewer* points."""
    frame = frame_from_boxes(mediabox, cropbox, rotate)
    a, b, c, d = _TM[rotate]
    ops = ["BT", "/F1 12 Tf"]
    for u, v, text in lines:
        x, y = _user_point(rotate, frame.dx + u, frame.dy + v, mediabox)
        ops.append(f"{a} {b} {c} {d} {x} {y} Tm ({text}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    extra = (f"/CropBox [{' '.join(map(str, cropbox))}] " if cropbox else "") + (f"/Rotate {rotate} " if rotate else "")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [{' '.join(map(str, mediabox))}] {extra}"
         f"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>").encode(),
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for index, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _ink(pdf_bytes: bytes, scale: float = 2.0, region=None):
    """Bounding box (viewer points) of dark pixels as pdfium renders the page."""
    page = pdfium.PdfDocument(pdf_bytes)[0]
    image = np.asarray(page.render(scale=scale).to_pil().convert("L"))
    if region is not None:
        mask = np.zeros_like(image, dtype=bool)
        x0, y0, x1, y1 = (int(v * scale) for v in region)
        mask[max(0, y0):y1, max(0, x0):x1] = True
        ys, xs = np.where((image < 140) & mask)
    else:
        ys, xs = np.where(image < 140)
    return (xs.min() / scale, ys.min() / scale, (xs.max() + 1) / scale, (ys.max() + 1) / scale), page.get_size()


def _fragment(parsed, text_part: str) -> Fragment:
    unit = next(u for u in parsed.units if text_part in u.text)
    return Fragment(id="d-f0", document_id="d", document_version="v1", ordinal=0, text=unit.text,
                    locator=unit.locator)


def _close(box, expected, tol=5.0):
    return all(abs(a - b) <= tol for a, b in zip(box, expected))


@pytest.mark.parametrize("name", list(CASES))
def test_native_highlight_lands_on_rendered_text_for_every_page_geometry(tmp_path, name):
    kw = CASES[name]
    lines = [(30, 100, "Intro paragraph about general provisions."),
             (30, 300, "Target clause requires registration within five days"),
             (30, 314, "second line of the target clause continues here.")]
    data = _pdf(**kw, lines=lines)
    path = tmp_path / "doc.pdf"
    path.write_bytes(data)
    parsed = parse_pdf(path)
    fragment = _fragment(parsed, "Target clause")
    block_frame = page_frame(path, 1)
    assert block_frame is not None

    result = resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks)
    assert result["exact"] is True and result["boxes"], result
    (w, h) = pdfium.PdfDocument(data)[0].get_size()
    assert (result["page_width"], result["page_height"]) == (w, h)

    # Union of the highlight boxes must hug the two target lines as actually drawn.
    union = [min(b["bbox"][0] for b in result["boxes"]), min(b["bbox"][1] for b in result["boxes"]),
             max(b["bbox"][2] for b in result["boxes"]), max(b["bbox"][3] for b in result["boxes"])]
    expected, _ = _ink(data, region=(union[0] - 2, union[1] - 2, union[2] + 2, union[3] + 2))
    assert _close(union, expected, tol=6.0), (name, union, expected)
    # normalized fractions are consistent with the viewer page size
    for box in result["boxes"]:
        assert box["norm"]["left"] == pytest.approx(box["bbox"][0] / w, abs=1e-3)
        assert box["norm"]["top"] == pytest.approx(box["bbox"][1] / h, abs=1e-3)


def test_partial_line_quote_highlights_only_quoted_words(tmp_path):
    data = _pdf(mediabox=(0, 0, 600, 800), lines=[(72, 150, "alpha beta gamma delta epsilon zeta eta theta")])
    path = tmp_path / "p.pdf"
    path.write_bytes(data)
    parsed = parse_pdf(path)
    fragment = _fragment(parsed, "alpha beta")
    result = resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks, quote="gamma delta epsilon")
    assert len(result["boxes"]) == 1
    box = result["boxes"][0]["bbox"]
    line_box = resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks)["boxes"][0]["bbox"]
    assert line_box[0] < box[0] and box[2] < line_box[2]
    # starts at the "gamma" glyphs, not at the line start
    full_ink, _ = _ink(data)
    assert box[0] > full_ink[0] + 40


def test_repeated_phrase_prefers_the_cited_block(tmp_path):
    lines = [(72, 100, "The applicant must sign the form."), (72, 400, "The applicant must sign the form.")]
    data = _pdf(mediabox=(0, 0, 600, 800), lines=lines)
    path = tmp_path / "r.pdf"
    path.write_bytes(data)
    parsed = parse_pdf(path)
    lower = max(parsed.units, key=lambda u: u.locator["bbox"][1])  # the one lower on the page
    fragment = Fragment(id="d-f1", document_id="d", document_version="v1", ordinal=1, text=lower.text,
                        locator=lower.locator)
    result = resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks)
    assert len(result["boxes"]) == 1
    assert result["boxes"][0]["bbox"][1] == pytest.approx(lower.locator["bbox"][1], abs=3)


def test_reviewer_corrected_text_still_highlights_its_own_lines(tmp_path):
    data = _pdf(mediabox=(0, 0, 600, 800), lines=[(72, 200, "Registration is mandatory for all staff members.")])
    path = tmp_path / "c.pdf"
    path.write_bytes(data)
    parsed = parse_pdf(path)
    base = _fragment(parsed, "Registration")
    edited = base.model_copy(update={"text": "Регистрация обязательна для всех сотрудников."})
    result = resolve_fragment_highlight(path, "pdf", edited, parsed.blocks)
    assert result["exact"] is True and len(result["boxes"]) == 1


@pytest.mark.parametrize("name", ["mediabox-offset", "cropbox", "rotate90+crop", "rotate270+offset+crop"])
def test_ocr_highlight_lands_on_rendered_ink(tmp_path, name):
    kw = CASES[name]
    # Scanned page: ink rectangle in a full-page raster, then box/rotation applied to the PDF.
    w = kw["mediabox"][2] - kw["mediabox"][0]
    h = kw["mediabox"][3] - kw["mediabox"][1]
    image = Image.new("RGB", (int(w), int(h)), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((150, 250, 420, 270), fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PDF", resolution=72.0)
    pdf = pdfium.PdfDocument(buf.getvalue())
    page = pdf[0]
    mb = kw["mediabox"]
    page.set_mediabox(*mb)
    if kw.get("cropbox"):
        page.set_cropbox(*kw["cropbox"])
    if kw.get("rotate"):
        page.set_rotation(kw["rotate"])
    out = io.BytesIO()
    pdf.save(out)
    data = out.getvalue()
    path = tmp_path / "scan.pdf"
    path.write_bytes(data)

    def fake_ocr(image, number, page_size=None):
        arr = np.asarray(image.convert("L"))
        ys, xs = np.where(arr < 128)
        pw, ph = page_size
        sx, sy = pw / image.width, ph / image.height
        box = [xs.min() * sx, ys.min() * sy, (xs.max() + 1) * sx, (ys.max() + 1) * sy]
        from navigator.ingestion.ocr import OCRResult
        words = [{"text": t, "bbox": [box[0] + i * (box[2] - box[0]) / 3, box[1],
                                      box[0] + (i + 1) * (box[2] - box[0]) / 3, box[3]]}
                 for i, t in enumerate(["Scanned", "clause", "text"])]
        return OCRResult([{"text": "Scanned clause text", "bbox": box, "conf": 95,
                           "lines": [{"text": "Scanned clause text", "bbox": box, "words": words}],
                           "page_size": [pw, ph], "reference_markers": []}],
                         page=number, source="ocr", rotation=0, warnings=[], confidence=95, needs_review=False)

    fake_ocr.supports_page_size = True
    parsed = parse_pdf(path, ocr=fake_ocr)
    fragment = _fragment(parsed, "Scanned clause")
    result = resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks)
    assert result["exact"] is True and len(result["boxes"]) == 1, result
    expected, size = _ink(data)
    assert (result["page_width"], result["page_height"]) == size
    assert _close(result["boxes"][0]["bbox"], expected, tol=4.0), (name, result["boxes"][0]["bbox"], expected)

    # A quote covering only the last word must narrow the box to that third of the ink.
    narrowed = resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks, quote="text")
    nb = narrowed["boxes"][0]["bbox"]
    full = result["boxes"][0]["bbox"]
    span = max(full[2] - full[0], full[3] - full[1])
    assert (nb[2] - nb[0]) * (nb[3] - nb[1]) < (full[2] - full[0]) * (full[3] - full[1]) * 0.6, span


def test_frame_math_matches_pdfium_for_all_rotations():
    for rotation in (0, 90, 180, 270):
        data = _pdf(mediabox=(10, 20, 610, 820), cropbox=(60, 100, 460, 700), rotate=rotation,
                    lines=[(50, 50, "x")])
        page = pdfium.PdfDocument(data)[0]
        frame = frame_from_boxes(page.get_mediabox(), (60, 100, 460, 700), rotation)
        assert (frame.width, frame.height) == pytest.approx(page.get_size(), abs=0.01)


class TestTextMatch:
    def _tokens(self, *lines):
        out = []
        for index, text in enumerate(lines):
            out += textmatch.tokens_for_lines([{"text": text, "bbox": [0, index * 12, 100, index * 12 + 10]}])
        for i, token in enumerate(out):
            token.line = next(j for j, t in enumerate(lines) if token.text in t and j >= 0)
        return out

    def test_hyphenated_word_across_lines_matches_joined_quote(self):
        lines = [{"text": "Положение о регистра-", "bbox": [0, 0, 100, 10], "page": 1},
                 {"text": "ции документов", "bbox": [0, 12, 100, 22], "page": 1}]
        tokens = textmatch.tokens_for_lines(lines)
        span = textmatch.align(tokens, "Положение о регистрации документов")
        assert span is not None and span.coverage == 1.0
        assert len(textmatch.line_boxes(span)) == 2

    def test_ocr_slip_still_aligns_without_spilling_to_neighbours(self):
        lines = [{"text": "Заявка подаётся в течение пяти дней", "bbox": [0, 0, 100, 10]},
                 {"text": "после регистрацин документа заявителем", "bbox": [0, 12, 100, 22]},
                 {"text": "Посторонний текст ниже по странице", "bbox": [0, 24, 100, 34]}]
        tokens = textmatch.tokens_for_lines(lines)
        span = textmatch.align(tokens, "Заявка подаётся в течение пяти дней после регистрации документа заявителем")
        assert span is not None and not span.exact and span.coverage >= 0.9
        assert {t.line for t in span.tokens} == {0, 1}

    def test_unrelated_query_does_not_match(self):
        tokens = textmatch.tokens_for_lines([{"text": "совсем другой текст про отпуск", "bbox": [0, 0, 100, 10]}])
        assert textmatch.align(tokens, "порядок оформления командировки сотрудников") is None


def test_viewer_requests_in_parallel_threads_do_not_crash_pdfium(tmp_path):
    """pdfium aborts the whole process when used from several threads at once; the viewer fires
    page info, page bitmap and highlight requests together."""
    from concurrent.futures import ThreadPoolExecutor
    from navigator.documents.highlighting import get_original_info, render_original_pdf_page

    data = _pdf(**CASES["cropbox"], lines=[(30, 100, "Parallel viewer request check line.")])
    path = tmp_path / "p.pdf"
    path.write_bytes(data)
    parsed = parse_pdf(path)
    fragment = _fragment(parsed, "Parallel")

    def work(index):
        if index % 3 == 0:
            return render_original_pdf_page(path, 1, scale=2.5)[:4]
        if index % 3 == 1:
            return get_original_info(path, "pdf")["page_count"]
        return resolve_fragment_highlight(path, "pdf", fragment, parsed.blocks)["exact"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(60)))
    assert results[0] == b"\x89PNG" and results[1] == 1 and results[2] is True
