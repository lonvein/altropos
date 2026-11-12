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


def _footnote_separator(image):
    """Find a short, isolated left-hand rule; full-width table/form rules abstain."""
    import numpy as np
    small = image.convert("L")
    small.thumbnail((1200, 1600))
    ink = np.asarray(small) < 180
    h, w = ink.shape
    candidates = []
    for y in range(int(h*.7), int(h*.95)):
        padded = np.pad(ink[y].astype(int), (1, 1))
        changes = np.diff(padded)
        for left, right in zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)):
            band = ink[max(0,y-int(h*.005)):min(h,y+int(h*.005)+1)]
            if (left < w*.2 and w*.15 <= right-left <= w*.4
                    and band.any(axis=0).sum() < w*.5):
                # Text touching either side of the rule is not an isolated separator.
                strip = ink[max(0,y-int(h*.009)):min(h,y+int(h*.009)+1), left:right]
                if strip.mean() < .3:
                    candidates.append((y, left, right))
    if not candidates:
        return None
    # Thick decorations and clusters of table lines are not footnote separators.
    groups = []
    for item in candidates:
        if not groups or item[0]-groups[-1][-1][0] > 2:
            groups.append([])
        groups[-1].append(item)
    thin = [group for group in groups if group[-1][0]-group[0][0] <= max(2,h*.004)]
    return thin[-1][-1][0] * image.height/h if len(thin) == 1 else None


def _rule_like_word(image, box, text):
    """Flag short OCR labels whose pixels are almost entirely long table rules."""
    import numpy as np
    x0,y0,x1,y1 = box
    if len(text.strip()) > 4 or x1-x0 < image.width*.15 or y1 <= y0:
        return False
    crop = image.crop((int(x0),int(y0),int(x1),int(y1))).convert("L")
    ink = np.asarray(crop) < 180
    if not ink.size or not ink.sum():
        return False
    horizontal = ink.mean(axis=1) > .7
    # Small scan skew spreads a rule across neighboring raster rows.
    radius = max(1, int(image.height*.002))
    for y in range(ink.shape[0]):
        band = ink[max(0,y-radius):min(ink.shape[0],y+radius+1)]
        if band.any(axis=0).mean() > .8:
            horizontal[max(0,y-radius):min(ink.shape[0],y+radius+1)] = True
    vertical = ink.mean(axis=0) > .7
    explained = horizontal[:, None] | vertical[None, :]
    return bool(horizontal.any() and (ink & explained).sum()/ink.sum() > .9)


def _classify_notes(items, body_height, image_height, separator):
    """Small text is supporting evidence, never enough to turn 6) or 3. into notes."""
    import re
    marker = re.compile(r"^(?:\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+|[*†‡?]+)\s+")
    previous = None
    for item in sorted(items, key=lambda item: item["work_bbox"][1]):
        top = item["work_bbox"][1]
        text = item["text"]
        numbered_point = bool(re.match(r"^\d+(?:[.)]|\.\d+)\s*", text))
        below = separator is not None and top > separator
        small = body_height and item["height"] < body_height * (.98 if below else .8)
        starts_note = bool(marker.match(text))
        # Below an independently detected separator, tolerate missing marker OCR.
        orphan = below and small and previous is None and not numbered_point
        continuation = (below and small and previous is not None and not starts_note
                        and not numbered_point and top-previous["work_bbox"][3] < body_height*3)
        item["note"] = bool(not item.get("suspected_rule") and not numbered_point
                            and top > image_height*.7 and small
                            and (starts_note or orphan or continuation))
        item["note_continuation"] = bool(item["note"] and continuation)
        item["note_marker_uncertain"] = bool(item["note"] and not continuation and not starts_note)
        if item["note"]:
            previous = item
        elif not item.get("suspected_rule"):
            previous = None


def _reference_markers(words):
    """Record raised small tokens and damaged suffixes; never guess missing digits."""
    import re
    from statistics import median
    result = []
    for word in words:
        peers = [p for p in words if p["line"] == word["line"] and len(p["text"]) > 2]
        height = median(p["box"][3]-p["box"][1] for p in peers) if peers else 0
        bottom = median(p["box"][3] for p in peers) if peers else 0
        token = word["text"]
        raised = (height and word["box"][3]-word["box"][1] <= height*.75
                  and word["box"][3] < bottom-height*.15)
        if raised and re.fullmatch(r"\d{1,2}|[*†‡?]+", token):
            result.append({"text": token, "confirmed": token != "?" and word["conf"] >= 70,
                           "box": word["box"]})
        else:
            suffix = re.search(r"(?<=[^\W\d_])([?*†‡]+|[⁰¹²³⁴⁵⁶⁷⁸⁹]+|\d{1,2})(?=[;,:.)»]|$)", token)
            if suffix:
                result.append({"text": suffix[1], "confirmed": False, "box": word["box"]})
    return result


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
            par = paragraphs.setdefault(key, {"confs": [], "heights": [], "box": [x,y,x+w,y+h], "line": {}, "words": []})
            par["line"].setdefault(data["line_num"][i], []).append(word)
            par["confs"].append(conf)
            all_conf.append(conf)
            heights.append(h)
            par["heights"].append(h)
            par["words"].append({"text": word, "conf": conf, "box": [x,y,x+w,y+h],
                                 "line": data["line_num"][i]})
            b = par["box"]
            b[:] = [min(b[0],x), min(b[1],y), max(b[2],x+w), max(b[3],y+h)]
        from statistics import median
        body_height = median(heights) if heights else 0
        items = []
        for par in paragraphs.values():
            text = " ".join(" ".join(words) for _, words in sorted(par["line"].items()))
            markers = _reference_markers(par["words"])
            for marker in markers:
                marker["bbox"] = _inverse_box(marker.pop("box"), steps, original_size, page_size)
            items.append({"text": text, "bbox": _inverse_box(par["box"], steps, original_size, page_size),
                          "conf": sum(par["confs"])/len(par["confs"]),
                          "work_bbox": par["box"], "height": median(par["heights"]),
                          "reference_markers": markers,
                          "suspected_rule": _rule_like_word(work, par["box"], text)})
        separator = _footnote_separator(work)
        _classify_notes(items, body_height, work.height, separator)
        for item in items:
            if item["suspected_rule"]:
                diag["needs_review"] = True
                diag["warnings"].append("OCR принял линии таблицы за текст; результат сохранён как артефакт для проверки")
            if item["note_marker_uncertain"]:
                diag["needs_review"] = True
                diag["warnings"].append("Не распознан маркер сноски под разделительной линией")
            del item["work_bbox"], item["height"]
        diag["confidence"] = sum(all_conf)/len(all_conf) if all_conf else None
        if sum(len(item["text"]) for item in items) < MIN_CHARS or not all_conf or diag["confidence"] < MIN_CONFIDENCE:
            # Preserve uncertain text for review instead of silently discarding it.
            diag["needs_review"] = True
            diag["warnings"].append("Низкая уверенность или недостаточно текста OCR")
        return OCRResult(items, **diag)

    ocr.supports_page_size = True
    return ocr
