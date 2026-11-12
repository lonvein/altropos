"""Определение структуры регламента по тексту: пункты, заголовки, приложения.

В приказах и регламентах нумерация набрана вручную («1.1. Текст»), стилей заголовков
нет, поэтому разделы определяются эвристиками. Один и тот же трекер используется
для DOCX, PDF и HTML, чтобы разделы в фрагментах были согласованы.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

POINT = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(\S.*)$", re.S)
APPENDIX = re.compile(r"^приложени[ея](\s*(№|N)\s*[\w.-]+|\s+\d+[\w.-]*)?\s*$|^приложени[ея]\s*(№|N)\s*[\w.-]+(\s|$)", re.I)
SECTION_WORD = re.compile(r"^(раздел|глава|статья|часть)\s+[\dIVXLC]+", re.I)


def is_upper_heading(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return 3 <= len(text) <= 90 and len(letters) >= 3 and all(c.isupper() for c in letters)


@dataclass
class Classified:
    kind: str  # paragraph | heading
    section: str
    path: tuple[str, ...]


class Outline:
    """Отслеживает текущий контекст: приложение/крупный заголовок и нумерованный раздел."""

    def __init__(self):
        self.top = ""
        self.sub = ""
        self._expect_appendix_title = False

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(x for x in (self.top, self.sub) if x)

    def feed(self, text: str, style_heading: bool = False) -> Classified:
        point = POINT.match(text)
        if self._expect_appendix_title and not point:
            self._expect_appendix_title = False
            self.top = f"{self.top}. {text}" if self.top else text
            return Classified("heading", "", self.path)
        self._expect_appendix_title = False
        if (APPENDIX.match(text) or SECTION_WORD.match(text)) and len(text) < 120 and not point:
            self.top, self.sub = text, ""
            self._expect_appendix_title = bool(APPENDIX.match(text)) and len(text) < 40
            return Classified("heading", "", self.path)
        if style_heading or (is_upper_heading(text) and not point):
            self.top, self.sub = text, ""
            return Classified("heading", "", self.path)
        if point:
            section = point.group(1)
            depth = section.count(".") + 1
            # A numbered enumeration is content even if its short label looks
            # like a section title. Do not replace context with each list item.
            enumeration = bool(re.match(r"^\d+\)\s", text))
            if not enumeration and depth == 1 and len(text) <= 100 and not text.rstrip().endswith((".", ";", ":", ",")):
                self.sub = text
                return Classified("heading", section, self.path)
            return Classified("paragraph", section, self.path)
        return Classified("paragraph", "", self.path)
