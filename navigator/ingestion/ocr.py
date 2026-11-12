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


def find_tesseract() -> str | None:
    explicit = os.environ.get("TESSERACT_CMD")
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in _WIN_DEFAULTS if Path(p).is_file()), None)


def find_tessdata_dir() -> str | None:
    prefix = os.environ.get("TESSDATA_PREFIX")
    if prefix and Path(prefix).is_dir():
        return prefix
    # Локальная runtime/tessdata (ASCII-путь без проблем с кодировкой кириллических путей Windows)
    for candidate in (Path("runtime/tessdata").resolve(), Path("../runtime/tessdata").resolve()):
        if candidate.is_dir() and (candidate / "rus.traineddata").is_file():
            return str(candidate)
    cmd = find_tesseract()
    if cmd:
        std = Path(cmd).parent / "tessdata"
        if std.is_dir():
            return str(std)
    return None


def ocr_status() -> dict:
    """Для /health и README: доступен ли OCR и какие языки установлены."""
    cmd = find_tesseract()
    if not cmd:
        return {"available": False, "reason": "Tesseract не найден (TESSERACT_CMD / PATH)"}
    tessdata = find_tessdata_dir()
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        langs = set(pytesseract.get_languages())
    except Exception:
        if tessdata and Path(tessdata).is_dir():
            langs = {p.stem for p in Path(tessdata).glob("*.traineddata")}
        else:
            return {"available": False, "reason": "Не удалось запустить Tesseract"}
    missing = {"rus", "eng"} - langs
    if missing:
        return {"available": False, "reason": f"Нет языковых данных: {', '.join(sorted(missing))}"}
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception:
        version = "5.x"
    return {"available": True, "version": version, "languages": LANGS, "tessdata": tessdata}


def make_ocr():
    status = ocr_status()
    if not status["available"]:
        return None
    import pytesseract
    from pytesseract import Output
    pytesseract.pytesseract.tesseract_cmd = find_tesseract()
    tessdata = status.get("tessdata")
    if tessdata:
        os.environ["TESSDATA_PREFIX"] = tessdata

    def ocr(image, number: int, page_size: tuple[float, float] | None = None) -> list[dict]:
        """image — PIL, отрисованный в 300 dpi; возвращает абзацы [{text, bbox}] в пунктах."""
        scale = 72.0 / 300.0
        try:
            data = pytesseract.image_to_data(image, lang=LANGS, config="--psm 3", output_type=Output.DICT)
        except Exception:
            return []
        paragraphs: dict[tuple, dict] = {}
        for i, word in enumerate(data["text"]):
            word = (word or "").strip()
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if not word or conf < 0:
                continue
            key = (data["block_num"][i], data["par_num"][i])
            x, y, w, h = (data[k][i] * scale for k in ("left", "top", "width", "height"))
            par = paragraphs.setdefault(key, {"words": [], "confs": [], "box": [x, y, x + w, y + h], "line": {}})
            par["line"].setdefault(data["line_num"][i], []).append(word)
            par["confs"].append(conf)
            box = par["box"]
            box[0], box[1], box[2], box[3] = min(box[0], x), min(box[1], y), max(box[2], x + w), max(box[3], y + h)
        items = []
        all_conf: list[float] = []
        for par in paragraphs.values():
            text = " ".join(" ".join(words) for _, words in sorted(par["line"].items()))
            items.append({"text": text, "bbox": [round(v, 1) for v in par["box"]],
                          "conf": sum(par["confs"]) / len(par["confs"])})
            all_conf += par["confs"]
        total_chars = sum(len(i["text"]) for i in items)
        if total_chars < MIN_CHARS or not all_conf or sum(all_conf) / len(all_conf) < MIN_CONFIDENCE:
            return []  # нечитаемо: страница будет помечена как требующая проверки
        return items

    return ocr
