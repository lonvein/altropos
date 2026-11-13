"""Эвристический экстрактор метаданных нормативных документов МФТИ.

Извлекает:
- номер документа (приказа/распоряжения);
- дату издания (issued);
- даты действия (valid_from, valid_to);
- название (title);
- связи (relations: amends, repeals, appendix) с пометкой confirmed=False.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from navigator.ingestion.common import Unit
from navigator.schemas import MetadataPatch, Relation

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12
}

_NUM_RE = re.compile(r"(?:приказ|распоряжение)?\s*(?:№|N|n)\s*([0-9]+(?:[-/][0-9а-яА-Яa-zA-Z]+)?)", re.I)
_DATE_RE = re.compile(r"(?:от|«)\s*(\d{1,2})[.»\s]+([а-яё]+|\d{2})[.\s]+(\d{4})", re.I)
_VALID_FROM_RE = re.compile(r"(?:вступает\s+в\s+силу\s+(?:с|со)?|действие\s+с)\s*«?\s*(\d{1,2})[.»\s]+([а-яё]+|\d{2})[.\s]+(\d{4})", re.I)
_VALID_TO_RE = re.compile(r"(?:действует\s+до|срок\s+действия\s+до)\s*«?\s*(\d{1,2})[.»\s]+([а-яё]+|\d{2})[.\s]+(\d{4})", re.I)

_REPEALS_RE = re.compile(
    r"(?:отменить\s+действие|признать\s+утратившим[и]?\s+силу)\s+"
    r"(?:(?:пункт[ауы]?|пп?\.|раздел[ауы]?)\s*([0-9]+(?:\.[0-9]+)*)\s+)?"
    r"приказ[а]?(?:\s+от\s+[^«\n,]+)?\s*(?:№|N)\s*([0-9]+(?:[-/][0-9а-яА-Яa-zA-Z]+)?)",
    re.I,
)
_AMENDS_RE = re.compile(
    r"(?:внести\s+изменения\s+в|о\s+внесении\s+изменений\s+в)\s+"
    r"(?:(?:пункт[ауы]?|пп?\.|раздел[ауы]?)\s*([0-9]+(?:\.[0-9]+)*)\s+)?"
    r"приказ[а]?(?:\s+от\s+[^«\n,]+)?\s*(?:№|N)\s*([0-9]+(?:[-/][0-9а-яА-Яa-zA-Z]+)?)",
    re.I,
)
_APPENDIX_RE = re.compile(r"приложени[ея]\s*(?:№|N)?\s*([0-9а-яА-Я\w.-]+)?\s*к\s+приказу(?:\s+от\s+[^«\n,]+)?\s*(?:№|N)\s*([0-9]+(?:[-/][0-9а-яА-Яa-zA-Z]+)?)", re.I)


def _parse_date(match: re.Match | None) -> date | None:
    if not match:
        return None
    day_s, mon_s, yr_s = match.groups()
    try:
        mon_int = MONTHS.get(mon_s.lower()) or (int(mon_s) if mon_s.isdigit() else None)
        if mon_int and 1 <= mon_int <= 12:
            return date(int(yr_s), mon_int, int(day_s))
    except (ValueError, TypeError):
        return None
    return None


def extract_metadata(units: list[Unit], title_hint: str = "", filename: str = "") -> MetadataPatch:
    """Извлекает структурированные метаданные и связи из первых фрагментов и названия файла."""
    # Анализируем первые 25 единиц (шапка документа и первые пункты)
    header_texts = [u.text for u in units[:25]]
    header_blob = "\n".join(header_texts)

    # 1. Номер документа
    num_m = _NUM_RE.search(header_blob)
    number = num_m.group(1) if num_m else None
    if not number and filename:
        fn_num = re.search(r"(?:№|N|n|\b)(\d+[-/]\d+)\b", filename)
        if fn_num:
            number = fn_num.group(1)

    # 2. Дата издания (issued)
    issued = _parse_date(_DATE_RE.search(header_blob))
    if not issued and filename:
        fn_date = re.search(r"(\d{2})[.](\d{2})[.](\d{4})", filename)
        if fn_date:
            d, m, y = fn_date.groups()
            try:
                issued = date(int(y), int(m), int(d))
            except ValueError:
                pass

    # 3. Даты действия
    valid_from = _parse_date(_VALID_FROM_RE.search(header_blob)) or issued
    valid_to = _parse_date(_VALID_TO_RE.search(header_blob))
    if valid_from and valid_to and valid_from > valid_to:
        valid_to = None  # предотвращаем конфликт дат

    # 4. Название (title)
    title = title_hint
    if not title:
        for t in header_texts:
            low = t.lower()
            if low.startswith(("о ", "об ", "по ", "регламент", "порядок", "положение", "инструкция")) and len(t) < 300:
                title = t
                break
    if not title and filename:
        title = Path(filename).stem

    # 5. Связи (Relations)
    relations: list[Relation] = []
    seen_rel = set()

    for m in _AMENDS_RE.finditer(header_blob):
        target_section, target_num = m.groups()
        key = ("amends", target_num, target_section)
        if key not in seen_rel:
            seen_rel.add(key)
            relations.append(
                Relation(target_id=target_num, kind="amends", target_section=target_section, confirmed=False)
            )

    for m in _REPEALS_RE.finditer(header_blob):
        target_section, target_num = m.groups()
        key = ("repeals", target_num, target_section)
        if key not in seen_rel:
            seen_rel.add(key)
            relations.append(
                Relation(target_id=target_num, kind="repeals", target_section=target_section, confirmed=False)
            )

    for m in _APPENDIX_RE.finditer(header_blob):
        app_sec, target_num = m.groups()
        key = ("appendix", target_num, app_sec)
        if key not in seen_rel:
            seen_rel.add(key)
            relations.append(Relation(target_id=target_num, kind="appendix", target_section=app_sec, confirmed=False))

    return MetadataPatch(
        title=(title or "Без названия")[:500],
        number=number,
        issued=issued,
        valid_from=valid_from,
        valid_to=valid_to,
        reviewed=False,
        relations=relations or None
    )
