"""OCR страниц PDF через Tesseract (rus+eng).

`make_ocr()` возвращает функцию для `parse_pdf(..., ocr=...)` или None, если Tesseract
не установлен — тогда страницы без текста честно помечаются needs_review.
Координаты слов переводятся из пикселей изображения в пункты страницы
(от левого верхнего угла), как у текстового слоя.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

LANGS = "rus+eng"
MIN_CONFIDENCE = 30.0  # порог уверенности слов
MIN_CHARS = 20
_WIN_DEFAULTS = (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                 r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")


def find_tesseract(tesseract_cmd: str | None = None) -> str | None:
    explicit = tesseract_cmd or os.environ.get("TESSERACT_CMD")
    if explicit:
        return str(explicit) if Path(explicit).is_file() else None
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in _WIN_DEFAULTS if Path(p).is_file()), None)


def find_tessdata_dir(tessdata_dir: str | None = None, tesseract_cmd: str | None = None) -> str | None:
    prefix = tessdata_dir or os.environ.get("TESSDATA_PREFIX")
    if prefix and Path(prefix).is_dir():
        return str(prefix)
    # Локальная runtime/tessdata (ASCII-путь без проблем с кодировкой кириллических путей Windows)
    for candidate in (Path("runtime/tessdata").resolve(), Path("../runtime/tessdata").resolve()):
        if candidate.is_dir() and (candidate / "rus.traineddata").is_file():
            return str(candidate)
    cmd = find_tesseract(tesseract_cmd)
    if cmd:
        std = Path(cmd).parent / "tessdata"
        if std.is_dir():
            return str(std)
    return None


def ocr_status(tesseract_cmd=None, tessdata_dir=None) -> dict:
    """Check the executable and language data without changing process environment."""
    if tessdata_dir is not None and not Path(tessdata_dir).is_dir():
        return {"available": False, "reason": "Каталог tessdata не найден"}
    cmd = find_tesseract(tesseract_cmd)
    if not cmd:
        return {"available": False, "reason": "Tesseract не найден (TESSERACT_CMD / PATH)"}
    tessdata = find_tessdata_dir(tessdata_dir, cmd)
    try:
        _config(tessdata)  # validate Windows path support before reporting available
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        # get_languages is cached globally by pytesseract and parses config with
        # different quoting rules from recognition. Probe this specific binary/data.
        import subprocess
        args = [str(cmd), "--list-langs"]
        if tessdata:
            args += ["--tessdata-dir", str(tessdata)]
        probe = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=15, check=True)
        langs = set(probe.stdout.splitlines())
        missing = {"rus", "eng"} - langs
        if missing:
            return {"available": False, "reason": f"Нет языковых данных: {', '.join(sorted(missing))}"}
        version = str(pytesseract.get_tesseract_version())
    except Exception:
        return {"available": False, "reason": "Не удалось запустить Tesseract"}
    return {"available": True, "version": version, "languages": LANGS, "tessdata": tessdata,
            "osd_available": "osd" in langs}


def _config(tessdata):
    if not tessdata:
        return ""
    path = str(tessdata)
    if os.name == "nt":
        # pytesseract uses shlex(posix=False): surrounding quotes reach Tesseract
        # literally. Use the native short path for directories containing spaces.
        if any(c.isspace() for c in path):
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)):
                path = buffer.value
        if any(c.isspace() for c in path):
            raise ValueError("Tessdata path requires an available Windows short name")
        return f"--tessdata-dir {path.replace(chr(92), chr(47))}"
    import shlex
    return f"--tessdata-dir {shlex.quote(path)}"


class OCRResult(list):
    """List-compatible OCR hook result with per-page diagnostics (also on failure)."""
    def __init__(self, items=(), **diagnostics):
        super().__init__(items)
        self.diagnostics = diagnostics


def _inverse_box(box, steps, original_size, page_size):
    """Undo PIL rotate(expand=True), then map raster to displayed PDF points.

    PDF /Rotate is already applied by the renderer; it must not be applied twice.
    Each step is (CCW angle, input size, output size), in raster edge coordinates.
    """
    import math
    x0, y0, x1, y1 = box
    points = [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]
    for angle, before, after in reversed(steps):
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        points = [(c*(x-after[0]/2)-s*(y-after[1]/2)+before[0]/2,
                   s*(x-after[0]/2)+c*(y-after[1]/2)+before[1]/2) for x, y in points]
    w, h = original_size
    pw, ph = page_size
    xs = [max(0., min(pw, x * pw/w)) for x, _ in points]
    ys = [max(0., min(ph, y * ph/h)) for _, y in points]
    return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]


def _deskew_angle(image):
    """Conservative projection-profile estimator; abstains on sparse/ambiguous pages."""
    import numpy as np
    from PIL import Image
    small = image.convert("L")
    small.thumbnail((1000, 1000))
    ink = np.asarray(small) < 160
    if ink.sum() < 1500 or ink.mean() > .25:
        return 0.
    binary = Image.fromarray((ink * 255).astype('uint8'))
    angles = [i * .5 for i in range(-6, 7)]
    scores = []
    for angle in angles:
        profile = np.asarray(binary.rotate(angle, expand=False)).sum(axis=1).astype(float)
        scores.append(float(np.square(np.diff(profile)).sum()))
    best = int(np.argmax(scores))
    # Avoid correcting border maxima, flat peaks, and an already aligned page.
    if best in (0, len(angles)-1) or not angles[best] or scores[best] < 1.3 * scores[6]:
        return 0.
    if scores[best] < 1.05 * max(scores[best-1], scores[best+1]):
        return 0.
    return angles[best]


def make_ocr(tesseract_cmd=None, tessdata_dir=None):
    status = ocr_status(tesseract_cmd, tessdata_dir)
    if not status["available"]:
        return None
    import pytesseract
    from pytesseract import Output
    cmd = find_tesseract(tesseract_cmd)
    config = _config(status.get("tessdata"))

    def ocr(image, number: int, page_size=None):
        pytesseract.pytesseract.tesseract_cmd = cmd
        original_size = image.size
        page_size = page_size or tuple(v * 72 / 300 for v in original_size)
        diag = {"page": number, "source": "ocr", "rotation": None,
                "orientation_confidence": None, "confidence": None, "warnings": [],
                "needs_review": False, "deskew": 0.}
        steps = []
        work = image
        try:
            osd = pytesseract.image_to_osd(work, config=config, output_type=Output.DICT)
            rotation = int(osd["rotate"])
            confidence = float(osd["orientation_conf"])
            diag["orientation_confidence"] = confidence
            if rotation not in (0, 90, 180, 270) or confidence < 8:
                raise ValueError("uncertain orientation")
            diag["rotation"] = rotation
            if rotation:
                work = image.rotate(-rotation, expand=True, fillcolor="white")
                steps.append((-rotation, image.size, work.size))
        except Exception:
            diag["warnings"].append("Ориентация не подтверждена OSD; сохранено исходное положение")
            diag["needs_review"] = True
        if diag["rotation"] is not None:
            angle = _deskew_angle(work)
            if angle:
                before = work.size
                work = work.rotate(angle, expand=True, fillcolor="white")
                steps.append((angle, before, work.size))
                diag["deskew"] = angle
        try:
            data = pytesseract.image_to_data(work, lang=LANGS, config=f"{config} --psm 3",
                                             output_type=Output.DICT)
        except Exception:
            diag.update(source="error", needs_review=True)
            diag["warnings"].append("Ошибка локального OCR")
            return OCRResult(**diag)
        paragraphs = {}
        all_conf = []
        heights = []
        for i, word in enumerate(data["text"]):
            word = (word or "").strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.
            if not word or conf < 0:
                continue
            x, y, w, h = (float(data[k][i]) for k in ("left", "top", "width", "height"))
            key = (data["block_num"][i], data["par_num"][i])
            par = paragraphs.setdefault(key, {"confs": [], "heights": [], "box": [x,y,x+w,y+h], "line": {}})
            par["line"].setdefault(data["line_num"][i], []).append(word)
            par["confs"].append(conf)
            all_conf.append(conf)
            heights.append(h)
            par["heights"].append(h)
            b = par["box"]
            b[:] = [min(b[0],x), min(b[1],y), max(b[2],x+w), max(b[3],y+h)]
        import re
        from statistics import median
        body_height = median(heights) if heights else 0
        items = []
        for par in paragraphs.values():
            text = " ".join(" ".join(words) for _, words in sorted(par["line"].items()))
            items.append({"text": text, "bbox": _inverse_box(par["box"], steps, original_size, page_size),
                          "conf": sum(par["confs"])/len(par["confs"]),
                          "note": bool(body_height and median(par["heights"]) < body_height*.8
                                       and par["box"][1] > work.height*.7
                                       and re.match(r"^(?:\d+|[*†‡]+)[.)]?\s+", text))})
        diag["confidence"] = sum(all_conf)/len(all_conf) if all_conf else None
        if sum(len(item["text"]) for item in items) < MIN_CHARS or not all_conf or diag["confidence"] < MIN_CONFIDENCE:
            # Preserve uncertain text for review instead of silently discarding it.
            diag["needs_review"] = True
            diag["warnings"].append("Низкая уверенность или недостаточно текста OCR")
        return OCRResult(items, **diag)

    ocr.supports_page_size = True
    return ocr
