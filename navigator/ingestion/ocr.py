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


def _isolated_marker(word, following, body_height):
    x0,y0,x1,y1 = word["box"]
    w,h = x1-x0,y1-y0
    return bool(body_height and .22*body_height <= h <= .8*body_height
                and .65 <= w/max(h,1) <= 1.4 and following["box"][0]-x1 >= body_height*1.5)


def _filled_bullet(image, word, following, body_height):
    """Use raster shape and indentation, independent of the OCR character label."""
    import numpy as np
    if not _isolated_marker(word,following,body_height):
        return False
    x0,y0,x1,y1 = word["box"]
    w,h = x1-x0,y1-y0
    ink = np.asarray(image.crop((int(x0),int(y0),int(x1),int(y1))).convert("L")) < 170
    center = ink[int(h*.25):max(int(h*.75),1),int(w*.25):max(int(w*.75),1)]
    return bool(ink.size and center.size and ink.mean() > .65 and center.mean() > .9)


def _numeric_stroke(image, box):
    """A serif-like digit stroke with a sloping head and foot, unlike a plain bar/I."""
    import numpy as np
    x0,y0,x1,y1 = (int(v) for v in box)
    ink = np.asarray(image.crop((x0,y0,x1,y1)).convert("L")) < 170
    if not ink.size or ink.shape[0] < 12 or ink.shape[1] < 4:
        return False
    h,w = ink.shape
    head,stem,foot = ink[:max(1,int(h*.25))],ink[int(h*.35):int(h*.7)],ink[int(h*.8):]
    hy,hx = np.where(head)
    sy,sx = np.where(stem)
    if not len(hx) or not len(sx) or not foot.any():
        return False
    stem_width = float(np.median(stem.sum(axis=1)))
    return bool(stem_width and hx.mean() < sx.mean()-w*.04
                and stem_width*1.3 < head.sum(axis=1).max() < w*.85
                and foot.sum(axis=1).max() > stem_width*1.4)


def _reread_inline_number(image, box, read):
    """Unrestricted OCR on raw and binary ROI must independently agree."""
    import math
    x0,y0,x1,y1 = box
    pad = max(3,math.ceil((y1-y0)*.14))
    crop = image.crop((max(0,int(x0)-pad),max(0,int(y0)-pad),
                       min(image.width,int(x1)+pad),min(image.height,int(y1)+pad))).convert("L")
    values = []
    for variant in (crop,crop.point(lambda v:255 if v>160 else 0)):
        try:
            text,confidence = read(variant,13)
        except Exception:
            return None
        if confidence < 85 or len(text.strip()) != 1 or text.strip() not in "0123456789":
            return None
        values.append(text.strip())
    return values[0] if values[0] == values[1] else None


_POINT_NUMBER_RE = None


def _point_number_re():
    global _POINT_NUMBER_RE
    if _POINT_NUMBER_RE is None:
        import re
        _POINT_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d+)+[.)]?|\d{1,3}[.)])")
    return _POINT_NUMBER_RE


def _normalize_point_text(text: str) -> str:
    import re
    return re.sub(r"(?<=\d),(?=\d)", ".", text.strip())


def _reread_number(image, box, read, original="", confidence=0, read_unrestricted=None):
    """Changes require two local readings; one can confirm an unchanged source number."""
    from collections import Counter
    from PIL import Image
    point_re = _point_number_re()
    x0,y0,x1,y1 = box
    pad = max(3, round((y1-y0)*.13))
    crop = image.crop((max(0,int(x0)-pad),max(0,int(y0)-pad),
                       min(image.width,int(x1)+pad),min(image.height,int(y1)+pad))).convert("L")
    bin_crop = crop.point(lambda v: 255 if v > 160 else 0)
    bicubic = getattr(getattr(Image, "Resampling", Image), "BICUBIC", Image.BICUBIC)
    up_crop = crop.resize((max(1, crop.width * 2), max(1, crop.height * 2)), bicubic)
    up_bin = up_crop.point(lambda v: 255 if v > 160 else 0)
    source_confidence = confidence
    norm_original = _normalize_point_text(original)
    readings = [norm_original] if confidence >= 70 and point_re.fullmatch(norm_original) else []
    for variant, psm in ((crop,7),(crop,13),(bin_crop,7),(up_crop,7),(up_bin,7)):
        try:
            text, confidence = read(variant,psm)
        except Exception:
            continue
        text = _normalize_point_text(text)
        if confidence >= 70 and point_re.fullmatch(text):
            readings.append(text)
        counts = Counter(readings)
        if counts and counts.most_common(1)[0][1] >= 2:
            return counts.most_common(1)[0][0]
    if read_unrestricted is not None:
        votes = []
        for variant, psm in ((crop,13),(bin_crop,13),(up_crop,7),(up_bin,7)):
            try:
                text, local_confidence = read_unrestricted(variant,psm)
            except Exception:
                continue
            text = _normalize_point_text(text)
            if local_confidence >= 80 and point_re.fullmatch(text):
                if text == norm_original and source_confidence >= 70:
                    return norm_original
                votes.append(text)
                counts = Counter(votes)
                if counts.most_common(1)[0][1] >= 2:
                    return counts.most_common(1)[0][0]
    return None


def _reattach_isolated_numbers(raw_lines, body):
    """Merge column-isolated point numbers back into horizontally aligned body lines."""
    import re
    candidates = []
    for src in raw_lines:
        if len(src["words"]) != 1:
            continue
        tok = src["words"][0]["text"]
        if ("." in tok or ")" in tok) and re.fullmatch(r"\d[\d.,\]\[|Il)]{1,12}", tok):
            candidates.append(src)
    for src in candidates:
        src_cy = (src["box"][1] + src["box"][3]) / 2
        best = None
        best_dist = None
        for dst in raw_lines:
            if dst is src or dst.get("consumed") or not dst["words"]:
                continue
            first_dst = dst["words"][0]["text"]
            if ("." in first_dst and re.fullmatch(r"\d[\d.,\]\[|Il)]{1,12}", first_dst)) or first_dst in ("—", "–", "-", "•"):
                continue
            gap_x = dst["box"][0] - src["box"][2]
            if gap_x < max(2.0, body * 0.25):
                continue
            dst_cy = (dst["box"][1] + dst["box"][3]) / 2
            dy = abs(src_cy - dst_cy)
            if dy > max(6.0, body * 0.65):
                continue
            dist = (round(dy, 1), gap_x)
            if best_dist is None or dist < best_dist:
                best = dst
                best_dist = dist
        if best is not None:
            num_word = dict(src["words"][0])
            num_word["line"] = best["line_id"]
            num_word["_reattached"] = True
            best["words"] = [num_word] + best["words"]
            b = best["box"]
            nb = num_word["box"]
            b[:] = [min(b[0], nb[0]), min(b[1], nb[1]), max(b[2], nb[2]), max(b[3], nb[3])]
            best["heights"] = [w["box"][3] - w["box"][1] for w in best["words"]]
            src["consumed"] = True


def _order_paragraph_lines(raw_lines, body):
    """Order paragraph blocks naturally without interleaving side-by-side column blocks."""
    by_key = {}
    for line in raw_lines:
        if line.get("consumed") or not line["words"]:
            continue
        entry = by_key.setdefault(line["key"], {"key": line["key"], "lines": [], "box": list(line["box"])})
        entry["lines"].append(line)
        eb = entry["box"]
        lb = line["box"]
        eb[:] = [min(eb[0], lb[0]), min(eb[1], lb[1]), max(eb[2], lb[2]), max(eb[3], lb[3])]
    entries = sorted(by_key.values(), key=lambda e: (e["box"][1], e["box"][0]))
    if not entries:
        return []
    bands = []
    current_band = [entries[0]]
    band_bottom = entries[0]["box"][3]
    for entry in entries[1:]:
        if entry["box"][1] < band_bottom - max(4.0, body * 0.35):
            current_band.append(entry)
            band_bottom = max(band_bottom, entry["box"][3])
        else:
            bands.append(current_band)
            current_band = [entry]
            band_bottom = entry["box"][3]
    bands.append(current_band)
    ordered = []
    x_tol = max(20.0, body * 2.5)
    for band in bands:
        band.sort(key=lambda e: (round(e["box"][0] / x_tol), e["box"][1]))
        ordered.extend(band)
    return ordered


def _layout_groups(paragraphs, image, read, diagnostics, read_unrestricted=None):
    """Recover marker boundaries and hanging continuations across OCR paragraphs."""
    import re
    from statistics import median
    point_re = _point_number_re()
    heights = [h for par in paragraphs.values() for h in par["heights"]]
    body = median(heights) if heights else 0
    raw_lines = []
    for key, par in paragraphs.items():
        line_ids = list(dict.fromkeys(word["line"] for word in par["words"]))
        for line_id in line_ids:
            words = [dict(word) for word in par["words"] if word["line"] == line_id]
            if not words:
                continue
            raw_lines.append({
                "key": key,
                "line_id": line_id,
                "words": words,
                "box": [min(w["box"][0] for w in words), min(w["box"][1] for w in words),
                        max(w["box"][2] for w in words), max(w["box"][3] for w in words)],
                "heights": [w["box"][3] - w["box"][1] for w in words],
            })
    _reattach_isolated_numbers(raw_lines, body)
    groups = []
    serial = 0
    for par_entry in _order_paragraph_lines(raw_lines, body):
        key = par_entry["key"]
        for line_rec in par_entry["lines"]:
            words = line_rec["words"]
            raw = " ".join(word["text"] for word in words)
            first = words[0]
            changes = []
            bullet = len(words) > 1 and _filled_bullet(image,first,words[1],body)
            if (not bullet and len(words)>1 and len(first["text"]) <= 2 and first["conf"] < 70
                    and _isolated_marker(first,words[1],body)):
                diagnostics["needs_review"] = True
                diagnostics["warnings"].append("Неоднозначный маркер списка; исходный символ сохранён")
            dash_bullet = (not bullet and len(words) > 1 and first["text"] in ("—", "–", "-")
                           and words[1]["box"][0] - first["box"][2] >= body * 0.25)
            body_word = next((word for word in words[1:] if any(c.isalpha() for c in word["text"])),
                             words[1] if len(words)>1 else first)
            number = (not bullet and not dash_bullet and len(words)>1 and ("." in first["text"] or ")" in first["text"])
                      and re.fullmatch(r"\d[\d.,\]\[|Il)]{1,12}", first["text"])
                      and (first.pop("_reattached", False) or body_word["box"][0]-first["box"][2] >= body*1.25))
            if bullet:
                first["text"] = "•"
                if raw != " ".join(word["text"] for word in words):
                    changes.append({"kind":"bullet", "before":raw.split()[0],
                                    "after":"•", "box":first["box"]})
            elif number and (first["conf"] < 90 or not point_re.fullmatch(_normalize_point_text(first["text"]))):
                corrected = _reread_number(image,first["box"],read,first["text"],first["conf"],read_unrestricted)
                if corrected and corrected != first["text"]:
                    changes.append({"kind":"number", "before":first["text"],"after":corrected,"box":first["box"]})
                    first["text"] = corrected
                elif corrected is None:
                    diagnostics["needs_review"] = True
                    diagnostics["warnings"].append(f"Номер пункта {first['text']} не подтверждён повторным OCR области")
            if read_unrestricted is not None:
                for index, token in enumerate(words[1:], start=1):
                    height = token["box"][3]-token["box"][1]
                    left_word = words[index-1]
                    if token["text"] not in ("|","I","l") or not body*.65 <= height <= body*1.5:
                        continue
                    after_num = left_word["text"].endswith(("№", "No", "N")) and token["box"][0]-left_word["box"][2] >= body*.15
                    between_alpha = (
                        index + 1 < len(words)
                        and any(c.isalpha() for c in left_word["text"])
                        and any(c.isalpha() for c in words[index+1]["text"])
                        and token["box"][0]-left_word["box"][2] >= body*.15
                        and words[index+1]["box"][0]-token["box"][2] >= body*.15
                        and _numeric_stroke(image,token["box"])
                    )
                    if not (between_alpha or after_num):
                        continue
                    corrected = _reread_inline_number(image,token["box"],read_unrestricted)
                    if corrected:
                        changes.append({"kind":"inline_number","before":token["text"],"after":corrected,"box":token["box"]})
                        token["text"] = corrected
                    elif between_alpha:
                        diagnostics["needs_review"] = True
                        diagnostics["warnings"].append("Неоднозначный числовой токен внутри строки; исходный символ сохранён")
            top = min(word["box"][1] for word in words)
            bottom = max(word["box"][3] for word in words)
            left = min(word["box"][0] for word in words)
            right = max(word["box"][2] for word in words)
            line_h = median(word["box"][3] - word["box"][1] for word in words)
            previous = groups[-1] if groups else None
            gap = top-previous["box"][3] if previous else 0
            line_limit = max(body, line_h, previous["line_height"] if previous else 0) * 1.8
            hanging = (previous and previous["list_item"] and not number and not bullet and not dash_bullet
                       and abs(left-previous["body_left"]) < max(3,body*.7) and 0 <= gap < line_limit)
            same_par = previous and previous["par_key"] == key
            continued_point = (previous and previous["numbered"] and not previous["list_item"]
                               and not previous["text"].rstrip().endswith((".",";",":"))
                               and (abs(left-previous["box"][0]) < max(3,body*.7)
                                    or abs(left-previous["body_left"]) < max(3,body*.7))
                               and 0 <= gap < line_limit)
            first_alpha = next((c for c in first["text"] if c.isalpha()), "")
            line_center = (left + right) / 2
            prev_center = (previous["box"][0] + previous["box"][2]) / 2 if previous else 0
            horiz_overlap = previous and min(right, previous["box"][2]) > max(left, previous["box"][0])
            similar_height = previous and abs(line_h - previous["line_height"]) <= max(3.0, body * 0.35)
            prev_letters = [c for c in previous["text"] if c.isalpha()] if previous else []
            line_letters = [c for c in " ".join(w["text"] for w in words) if c.isalpha()]
            both_upper = bool(prev_letters and line_letters and all(c.isupper() for c in prev_letters) and all(c.isupper() for c in line_letters))
            wrapped_heading = (
                previous and not previous["list_item"] and not bullet and not dash_bullet and not number
                and not previous["text"].rstrip().endswith((".", ";", ":", "!", "?"))
                and 0 <= gap < line_limit and similar_height
                and (horiz_overlap or abs(line_center - prev_center) <= max(10.0, body * 2.5))
                and (first_alpha.islower() or both_upper)
            )
            if not previous or bullet or dash_bullet or number or not (same_par or hanging or continued_point or wrapped_heading):
                body_left = words[1]["box"][0] if (bullet or dash_bullet or number) else left
                group = {"words":[],"line":{},"box":[left,top,right,bottom],"confs":[],"heights":[],
                         "list_item":bool(bullet or dash_bullet),"numbered":bool(number),"body_left":body_left,
                         "line_height":line_h,"par_key":key,"raw_text":"","text":"","layout_corrections":[]}
                groups.append(group)
            else:
                group = previous
                group["line_height"] = line_h
            serial += 1
            for word in words:
                word["line"] = serial
                group["words"].append(word)
                group["confs"].append(word["conf"])
                group["heights"].append(word["box"][3]-word["box"][1])
                box = group["box"]
                box[:] = [min(box[0],word["box"][0]),min(box[1],word["box"][1]),
                          max(box[2],word["box"][2]),max(box[3],word["box"][3])]
            group["line"][serial] = [word["text"] for word in words]
            group["text"] = " ".join(" ".join(line) for line in group["line"].values())
            group["raw_text"] = (group["raw_text"]+" "+raw).strip()
            group["layout_corrections"].extend(changes)
    return groups


def make_ocr(tesseract_cmd=None, tessdata_dir=None):
    status = ocr_status(tesseract_cmd, tessdata_dir)
    if not status["available"]:
        return None
    import pytesseract
    from pytesseract import Output
    cmd = find_tesseract(tesseract_cmd)
    tessdata = status.get("tessdata")
    config = _config(tessdata)
    osd_tessdata = (
        tessdata if (tessdata and (Path(tessdata) / "osd.traineddata").is_file())
        else find_tessdata_dir(None, cmd)
    )
    osd_config = _config(osd_tessdata) if osd_tessdata else config

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
            osd = pytesseract.image_to_osd(work, config=osd_config, output_type=Output.DICT)
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
        def read_number(crop, psm):
            data = pytesseract.image_to_data(crop, lang="eng",
                config=f"{config} --psm {psm} -c tessedit_char_whitelist=0123456789.", output_type=Output.DICT)
            values = [(t,float(c)) for t,c in zip(data["text"],data["conf"]) if t.strip()]
            return ("".join(t for t,_ in values), min(c for _,c in values)) if values else ("",0.)

        def read_unrestricted(crop, psm):
            data = pytesseract.image_to_data(crop,lang=LANGS,config=f"{config} --psm {psm}",output_type=Output.DICT)
            values = [(t,float(c)) for t,c in zip(data["text"],data["conf"]) if t.strip()]
            return ("".join(t for t,_ in values),min(c for _,c in values)) if values else ("",0.)

        for par in _layout_groups(paragraphs,work,read_number,diag,read_unrestricted):
            text = " ".join(" ".join(words) for _, words in sorted(par["line"].items()))
            line_words: dict[int, list[dict]] = {}
            for word in par["words"]:
                line_words.setdefault(word.get("line", 0), []).append(word)
            line_boxes = []
            for line_id, words in sorted(line_words.items()):
                lbox = [min(w["box"][0] for w in words), min(w["box"][1] for w in words),
                        max(w["box"][2] for w in words), max(w["box"][3] for w in words)]
                line_boxes.append({
                    "text": " ".join(w["text"] for w in words),
                    "bbox": _inverse_box(lbox, steps, original_size, page_size),
                    # Word boxes let a quote that covers part of a line be highlighted exactly.
                    "words": [{"text": w["text"], "bbox": _inverse_box(w["box"], steps, original_size, page_size)}
                              for w in sorted(words, key=lambda w: w["box"][0])],
                })
            markers = _reference_markers(par["words"])
            for marker in markers:
                marker["bbox"] = _inverse_box(marker.pop("box"), steps, original_size, page_size)
            changes = par["layout_corrections"]
            for change in changes:
                change["bbox"] = _inverse_box(change.pop("box"), steps, original_size, page_size)
            items.append({"text": text, "raw_text":par["raw_text"], "list_item":par["list_item"],
                          "layout_corrections":changes, "bbox": _inverse_box(par["box"], steps, original_size, page_size),
                          "lines": line_boxes,
                          "page_size": [round(float(page_size[0]), 1), round(float(page_size[1]), 1)],
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
