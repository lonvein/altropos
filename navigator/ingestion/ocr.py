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
MIN_CONFIDENCE = 45.0  # средняя уверенность слов; ниже — текст считается нечитаемым
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


def ocr_status() -> dict:
    """Для /health и README: доступен ли OCR и какие языки установлены."""
    cmd = find_tesseract()
    if not cmd:
        return {"available": False, "reason": "Tesseract не найден (TESSERACT_CMD / PATH)"}
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = cmd
        langs = set(pytesseract.get_languages(config=""))
    except Exception:
        return {"available": False, "reason": "Не удалось запустить Tesseract"}
    missing = {"rus", "eng"} - langs
    if missing:
        return {"available": False, "reason": f"Нет языковых данных: {', '.join(sorted(missing))}"}
    return {"available": True, "version": str(pytesseract.get_tesseract_version()), "languages": LANGS}


def make_ocr():
    if not ocr_status()["available"]:
        return None
    import pytesseract
    from pytesseract import Output
    pytesseract.pytesseract.tesseract_cmd = find_tesseract()

    def ocr(image, number: int, page_size: tuple[float, float] | None = None) -> list[dict]:
        """image — PIL, отрисованный в 300 dpi; возвращает абзацы [{text, bbox}] в пунктах."""
        scale = 72.0 / 300.0
        data = pytesseract.image_to_data(image, lang=LANGS, config="--psm 3", output_type=Output.DICT)
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
