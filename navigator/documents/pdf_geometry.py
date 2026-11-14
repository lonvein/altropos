"""PDF page geometry shared by extraction, OCR and the original-page viewer.

Three coordinate frames exist for one PDF page:

* the *viewer frame*: what pdfium renders for the reader -- the CropBox (clipped to the
  MediaBox) with /Rotate applied, origin at the top-left corner, in points;
* the *extraction frame*: what pdfplumber and the OCR renderer (``force_mediabox``)
  report -- the whole MediaBox with /Rotate applied, origin shifted by ``page.bbox``;
* normalized ``left/top/width/height`` fractions sent to the browser.

Blocks saved in approved snapshots are in the extraction frame. Reading them as if they
were in the viewer frame shifts every highlight by the CropBox offset (and clips it on
the right/bottom), which is the main reason highlights drifted off the printed text on
real-world PDFs. ``PageFrame`` converts between the two without touching stored data.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

Box = list[float]


@dataclass(frozen=True)
class PageFrame:
    width: float
    height: float
    dx: float = 0.0
    dy: float = 0.0
    rotation: int = 0

    def to_viewer(self, box: Iterable[float]) -> Box | None:
        """Extraction-frame box -> viewer-frame box, clipped to the page (None if outside)."""
        x0, y0, x1, y1 = (float(v) for v in box)
        x0, x1 = x0 - self.dx, x1 - self.dx
        y0, y1 = y0 - self.dy, y1 - self.dy
        x0, y0 = max(0.0, x0), max(0.0, y0)
        x1, y1 = min(self.width, x1), min(self.height, y1)
        if x1 - x0 < 0.2 or y1 - y0 < 0.2:
            return None
        return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]

    def to_extraction(self, box: Iterable[float]) -> Box:
        x0, y0, x1, y1 = (float(v) for v in box)
        return [x0 + self.dx, y0 + self.dy, x1 + self.dx, y1 + self.dy]


def _rotate_point(x: float, y: float, media: tuple[float, float, float, float], rotation: int):
    mx0, my0, mx1, my1 = media
    if rotation == 90:
        return y - my0, x - mx0
    if rotation == 180:
        return mx1 - x, y - my0
    if rotation == 270:
        return my1 - y, mx1 - x
    return x - mx0, my1 - y


def frame_from_boxes(media, crop, rotation: int, size: tuple[float, float] | None = None) -> PageFrame:
    """Build a frame from PDF user-space boxes ``(left, bottom, right, top)``."""
    mx0, my0, mx1, my1 = (float(v) for v in media)
    mx0, mx1 = sorted((mx0, mx1))
    my0, my1 = sorted((my0, my1))
    media = (mx0, my0, mx1, my1)
    if crop is None:
        cx0, cy0, cx1, cy1 = media
    else:
        cx0, cx1 = sorted((float(crop[0]), float(crop[2])))
        cy0, cy1 = sorted((float(crop[1]), float(crop[3])))
        cx0, cy0, cx1, cy1 = max(cx0, mx0), max(cy0, my0), min(cx1, mx1), min(cy1, my1)
        if cx1 <= cx0 or cy1 <= cy0:
            cx0, cy0, cx1, cy1 = media
    rotation = int(rotation or 0) % 360
    if rotation not in (0, 90, 180, 270):
        rotation = 0
    points = [_rotate_point(x, y, media, rotation) for x in (cx0, cx1) for y in (cy0, cy1)]
    us, vs = [p[0] for p in points], [p[1] for p in points]
    width, height = max(us) - min(us), max(vs) - min(vs)
    if size is not None and size[0] > 0 and size[1] > 0:
        width, height = float(size[0]), float(size[1])
    return PageFrame(width=round(width, 3), height=round(height, 3),
                     dx=round(min(us), 3), dy=round(min(vs), 3), rotation=rotation)


# pdfium is not thread-safe, and the web server answers viewer requests (page info, page
# bitmap, highlight) concurrently in a thread pool: every pdfium call goes through this lock.
PDFIUM_LOCK = threading.RLock()

_CACHE: dict[tuple[str, int, int], dict[int, PageFrame]] = {}
_CACHE_MAX = 32
_LOCK = threading.Lock()


def _remember(key, value):
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = value


def page_frames(path: Path | None) -> dict[int, PageFrame]:
    """Frames of every page (1-based) of an on-disk PDF; empty dict if unreadable."""
    if path is None or not path.is_file():
        return {}
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    frames: dict[int, PageFrame] = {}
    try:
        import pypdfium2 as pdfium

        with PDFIUM_LOCK:
            frames = _read_frames_pdfium(pdfium, path)
    except Exception:
        frames = {}
    if not frames:
        frames = _read_frames_plumber(path)
    _remember(key, frames)
    return frames


def _read_frames_pdfium(pdfium, path) -> dict[int, PageFrame]:
    frames: dict[int, PageFrame] = {}
    if True:
        pdf = pdfium.PdfDocument(str(path))
        try:
            for index in range(len(pdf)):
                page = pdf[index]
                try:
                    media = page.get_mediabox()
                    try:
                        crop = page.get_cropbox(fallback_ok=False)
                    except TypeError:  # older pypdfium2 builds
                        crop = page.get_cropbox()
                    frames[index + 1] = frame_from_boxes(media, crop, page.get_rotation(), page.get_size())
                finally:
                    page.close()
        finally:
            pdf.close()
    return frames


def _read_frames_plumber(path) -> dict[int, PageFrame]:
    frames: dict[int, PageFrame] = {}
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                # pdfplumber boxes are already rotated by /Rotate.
                frames[number] = frame_from_boxes(page.mediabox, page.cropbox, 0)
    except Exception:
        return {}
    return frames


def page_frame(path: Path | None, page_number: int) -> PageFrame | None:
    return page_frames(path).get(int(page_number))


@dataclass
class NativeWord:
    text: str
    box: Box  # viewer frame
    line: int


def native_words(path: Path | None, page_number: int, frame: PageFrame | None, *,
                 region: Box | None = None) -> list[NativeWord]:
    """Words of the PDF text layer in viewer-frame points, grouped into visual lines.

    ``region`` (viewer frame) keeps only words whose centre lies inside it; inside a region
    the geometric order is reliable. Without a region the producer's reading order
    (``use_text_flow``) is kept so multi-column pages are not interleaved.
    """
    if path is None or not path.is_file():
        return []
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            if page_number < 1 or page_number > len(pdf.pages):
                return []
            page = pdf.pages[page_number - 1]
            ox, oy = page.bbox[:2]
            frame = frame or PageFrame(width=float(page.width), height=float(page.height))
            raw = page.extract_words(use_text_flow=region is None, keep_blank_chars=False,
                                     x_tolerance=2, y_tolerance=3)
    except Exception:
        return []
    items: list[tuple[str, Box]] = []
    for word in raw:
        text = (word.get("text") or "").strip()
        if not text:
            continue
        box = frame.to_viewer((word["x0"] - ox, word["top"] - oy, word["x1"] - ox, word["bottom"] - oy))
        if box is None:
            continue
        if region is not None:
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            if not (region[0] - 2 <= cx <= region[2] + 2 and region[1] - 2 <= cy <= region[3] + 2):
                continue
        items.append((text, box))
    def same_row(a: Box, b: Box) -> bool:
        return abs((a[1] + a[3]) / 2 - (b[1] + b[3]) / 2) <= 0.6 * min(a[3] - a[1], b[3] - b[1])

    rows: list[list[tuple[str, Box]]] = []
    if region is not None:
        # Inside one block geometry is reliable: cluster by vertical centre, order by x.
        for item in sorted(items, key=lambda it: (it[1][1] + it[1][3]) / 2):
            if rows and same_row(rows[-1][0][1], item[1]):
                rows[-1].append(item)
            else:
                rows.append([item])
        rows = [sorted(row, key=lambda it: it[1][0]) for row in rows]
    else:
        # Producer reading order: a new row starts when the baseline changes or x steps back.
        for item in items:
            if rows and same_row(rows[-1][-1][1], item[1]) and item[1][0] >= rows[-1][-1][1][0] - 1.0:
                rows[-1].append(item)
            else:
                rows.append([item])
    return [NativeWord(text=text, box=box, line=index)
            for index, row in enumerate(rows) for text, box in row]


def native_lines(words: list[NativeWord]) -> list[dict[str, Any]]:
    """Line dicts (text, bbox, words) for ``textmatch.tokens_for_lines``."""
    grouped: dict[int, list[NativeWord]] = {}
    for word in words:
        grouped.setdefault(word.line, []).append(word)
    lines = []
    for _, items in sorted(grouped.items()):
        lines.append({
            "text": " ".join(w.text for w in items),
            "bbox": [min(w.box[0] for w in items), min(w.box[1] for w in items),
                     max(w.box[2] for w in items), max(w.box[3] for w in items)],
            "words": [{"text": w.text, "bbox": w.box} for w in items],
        })
    return lines
