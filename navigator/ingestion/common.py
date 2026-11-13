"""Общие типы и разбиение на фрагменты для всех парсеров.

Парсер превращает файл в плоский список `Unit` (абзац, пункт, таблица, группа строк),
а `build_fragments` упаковывает их в `Fragment` по правилам из задания:
ориентир 500 токенов, максимум 800, длинные разделы — с перекрытием 80,
короткие пункты не дробятся, таблицы режутся группами строк с повтором шапки.

Токены оцениваются приблизительно (по символам): точный токенизатор модели есть
только у Клима, окончательный бюджет контекста считает он.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from navigator.schemas import Fragment

CHARS_PER_TOKEN = 3.0  # консервативно для русского текста
TARGET_TOKENS = 500
MAX_TOKENS = 800
OVERLAP_TOKENS = 80

TARGET_CHARS = int(TARGET_TOKENS * CHARS_PER_TOKEN)
MAX_CHARS = int(MAX_TOKENS * CHARS_PER_TOKEN)
OVERLAP_CHARS = int(OVERLAP_TOKENS * CHARS_PER_TOKEN)


class ParseError(Exception):
    """Файл нечитаем или повреждён; сообщение безопасно показывать пользователю."""


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


@dataclass
class Unit:
    """Минимальный адресуемый кусок источника."""

    text: str
    locator: dict  # поля локатора без `kind`-зависимой логики: уже в формате схемы
    section: str = ""  # номер пункта, например "3.2"
    path: tuple[str, ...] = ()  # цепочка заголовков, от общего к частному
    kind: str = "paragraph"  # paragraph | heading | table
    atomic: bool = False  # не разрезать внутри (короткий список, строка таблицы)
    table_header: str = ""  # шапка таблицы, повторяется при делении
    rows: list[str] | None = None  # для таблиц: строки без шапки
    row_locators: list[dict] | None = None  # локатор каждой строки (XLSX: диапазон ячеек)
    note_refs: tuple[str, ...] = ()  # explicit source markers
    note_id: str = ""


@dataclass
class FragmentDraft:
    text: str
    section: str
    locator: dict
    units: list[int] = field(default_factory=list)  # индексы Unit, из которых собран фрагмент
    warnings: list[str] = field(default_factory=list)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_ids(sha256: str) -> tuple[str, str]:
    """ID документа и версии выводятся из содержимого: новый файл — новые ID."""
    return f"doc-{sha256[:16]}", "v1"


def fragment_id(document_id: str, ordinal: int) -> str:
    return f"{document_id}-f{ordinal:04d}"


_SENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+(?=[А-ЯЁA-Z«\"(\d•\-])")


def _split_long_text(text: str, reserve: int = 0) -> list[str]:
    """Режет длинный текст по границам предложений, затем по словам; перекрытие — OVERLAP_CHARS.

    reserve — сколько символов займёт префикс (заголовки), чтобы итог не превысил MAX_CHARS.
    """
    limit = max(MAX_CHARS - reserve, 200)
    target = min(TARGET_CHARS, limit)
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENT_SPLIT.split(text):
        while len(sentence) > limit:  # предложение без точек: режем по словам
            cut = sentence.rfind(" ", 0, target)
            cut = cut if cut > 0 else target
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut])
            sentence = sentence[cut:].lstrip()
        if current and len(current) + 1 + len(sentence) > target:
            pieces.append(current)
            tail = current[-OVERLAP_CHARS:]
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            current = f"{tail} {sentence}".strip() if tail else sentence
            if len(current) > limit:
                current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence
    if current:
        pieces.append(current)
    return pieces


def _header_line(path: tuple[str, ...]) -> str:
    return " > ".join(p for p in path if p)


def _cell_bounds(rng: str) -> tuple[int, int, int, int]:
    from openpyxl.utils import range_boundaries
    return range_boundaries(rng)  # min_col, min_row, max_col, max_row


def merge_locators(locators: list[dict]) -> dict:
    """Локатор фрагмента из локаторов его строк: для XLSX — объемлющий диапазон."""
    first = dict(locators[0])
    if first.get("kind") == "xlsx" and len(locators) > 1:
        from openpyxl.utils import get_column_letter
        same_sheet = [loc for loc in locators if loc["sheet"] == first["sheet"]]
        bounds = [_cell_bounds(loc["range"]) for loc in same_sheet]
        c0, r0 = min(b[0] for b in bounds), min(b[1] for b in bounds)
        c1, r1 = max(b[2] for b in bounds), max(b[3] for b in bounds)
        a, b = f"{get_column_letter(c0)}{r0}", f"{get_column_letter(c1)}{r1}"
        first["range"] = a if a == b else f"{a}:{b}"
    return first


def _build_fragments(units: list[Unit]) -> list[FragmentDraft]:
    """Упаковывает Unit в черновики фрагментов. Порядок сохраняется.

    Правила: цепочка заголовков (path) добавляется первой строкой каждого фрагмента;
    заголовки сами фрагментами не становятся; соседние абзацы одного раздела
    объединяются до TARGET_CHARS; строка «...:» не отрывается от списка под ней;
    таблицы и крупные абзацы формируют отдельные фрагменты.
    """
    drafts: list[FragmentDraft] = []
    buffer: list[tuple[int, Unit]] = []

    def flush():
        if not buffer:
            return
        first = buffer[0][1]
        header = _header_line(first.path)
        body = "\n".join(u.text for _, u in buffer)
        text = f"{header}\n{body}" if header else body
        section = first.section or next((u.section for _, u in buffer if u.section), "")
        drafts.append(FragmentDraft(text=text, section=section, locator=dict(first.locator),
                                    units=[i for i, _ in buffer]))
        buffer.clear()

    def buffer_chars() -> int:
        return sum(len(u.text) + 1 for _, u in buffer)

    for index, unit in enumerate(units):
        if not unit.text.strip():
            continue
        if unit.kind == "heading":
            flush()
            continue
        if unit.kind == "note":  # сноски не смешиваются с основным текстом
            flush()
            buffer.append((index, unit))
            flush()
            continue
        if unit.kind == "table":
            flush()
            drafts.extend(_table_drafts(index, unit))
            continue
        if buffer and buffer[0][1].locator.get("page") != unit.locator.get("page"):
            flush()  # фрагмент PDF не пересекает границу страницы: ссылка ведёт на нужную страницу
        same_context = bool(buffer) and buffer[-1][1].path == unit.path
        lead_in = bool(buffer) and buffer[-1][1].text.rstrip().endswith(":")
        starts_new_point = bool(unit.section) and bool(buffer) and unit.section != buffer[-1][1].section
        if buffer and not lead_in and (not same_context
                                       or buffer_chars() + len(unit.text) > TARGET_CHARS
                                       or (starts_new_point and buffer_chars() >= TARGET_CHARS // 3)):
            flush()
        elif buffer and not same_context:
            flush()
        if len(unit.text) > MAX_CHARS and not unit.atomic:
            flush()
            header = _header_line(unit.path)
            for piece in _split_long_text(unit.text, reserve=len(header) + 1):
                text = f"{header}\n{piece}" if header else piece
                drafts.append(FragmentDraft(text=text, section=unit.section, locator=dict(unit.locator),
                                            units=[index]))
            continue
        buffer.append((index, unit))
    flush()
    return drafts


def build_fragments(units: list[Unit]) -> list[FragmentDraft]:
    """Keep explicitly linked notes beside their grounds, including split tables."""
    drafts = _build_fragments(units)
    # Consecutive headings and a heading at EOF may never become a path of a
    # content fragment. Keep such source text rather than silently dropping it.
    missing = [FragmentDraft(text=u.text, section=u.section, locator=dict(u.locator), units=[i])
               for i, u in enumerate(units) if u.kind == "heading" and u.text.strip()
               and not any(u.text in d.text for d in drafts)]
    drafts = sorted(drafts + missing, key=lambda d: min(d.units))
    notes = {u.note_id: (i, u) for i, u in enumerate(units) if u.note_id}
    result = []
    for draft in drafts:
        grounds = draft.units + [i for i, unit in enumerate(units)
                                  if unit.kind == "heading" and unit.text in draft.text]
        refs = dict.fromkeys(ref for i in grounds for ref in units[i].note_refs)
        linked = [notes[ref] for ref in refs if ref in notes and notes[ref][0] not in draft.units]
        if not linked:
            result.append(draft)
            continue
        suffix = "\n" + "\n".join(u.text for _, u in linked)
        indices = draft.units + [i for i, _ in linked]
        if len(suffix) >= MAX_CHARS - 200:
            draft.warnings.append("Сноска слишком велика для совместного фрагмента; см. отдельную сноску")
            result.append(draft)
            continue
        prefix = ''
        body = draft.text
        table = next((units[i] for i in draft.units if units[i].kind == 'table'), None)
        if table:
            prefix = '\n'.join(s for s in (_header_line(table.path), table.table_header) if s)
            if prefix and body.startswith(prefix+'\n'):
                body = body[len(prefix)+1:]
                prefix += '\n'
            else:
                prefix = ''
        if len(prefix)+len(suffix) > MAX_CHARS-200:
            draft.warnings.append('Шапка и сноска слишком велики для совместного фрагмента')
        for piece in _split_long_text(body, reserve=len(suffix)+len(prefix)):
            result.append(FragmentDraft(text=prefix+piece+suffix, section=draft.section,
                                        locator=draft.locator.copy(), units=indices,
                                        warnings=list(draft.warnings)))
    return result


def _table_drafts(index: int, unit: Unit) -> list[FragmentDraft]:
    header = _header_line(unit.path)
    prefix = "\n".join(x for x in (header, unit.table_header) if x)
    rows = unit.rows or [unit.text]
    row_locs = unit.row_locators or [unit.locator] * len(rows)
    if len(row_locs) != len(rows):
        raise ParseError('Число строк таблицы не совпадает с числом локаторов')
    drafts: list[FragmentDraft] = []
    group: list[str] = []
    group_locs: list[dict] = []

    def make(text_rows: list[str], locs: list[dict]):
        body = "\n".join(text_rows)
        text = f"{prefix}\n{body}" if prefix else body
        warnings = ['Шапка таблицы превышает бюджет фрагмента'] if len(text) > MAX_CHARS else []
        drafts.append(FragmentDraft(text=text, section=unit.section,
                                    locator=merge_locators(locs), units=[index], warnings=warnings))

    def emit():
        if group:
            make(group, group_locs)
            group.clear()
            group_locs.clear()

    for row, loc in zip(rows, row_locs):
        if group and len(prefix) + sum(len(g) + 1 for g in group) + len(row) > TARGET_CHARS:
            emit()
        if len(prefix) + len(row) > MAX_CHARS:  # одна очень длинная строка
            emit()
            for piece in _split_long_text(row, reserve=len(prefix) + 1):
                make([piece], [loc])
            continue
        group.append(row)
        group_locs.append(loc)
    emit()
    return drafts

def to_fragments(document_id: str, document_version: str, drafts: list[FragmentDraft]) -> list[Fragment]:
    """Присваивает стабильные ID и ordinal по порядку. Locator-словарь содержит `kind`."""
    return [
        Fragment(id=fragment_id(document_id, ordinal), document_id=document_id,
                 document_version=document_version, ordinal=ordinal, text=draft.text,
                 section=draft.section, locator=draft.locator)
        for ordinal, draft in enumerate(drafts)
    ]


_ALWAYS_NOISY_WARNING_SUBSTRINGS = (
    "уточнён согласованным повторным OCR области",
    "уточнён согласованным OCR области без ограничения алфавита",
    "распознаны OCR; текст может содержать ошибки",
    "Визуально подтверждён оператором по оригиналу, текст не менялся",
    "технически сверены с оригиналом",
    "без сохранённого значения",
    "значения не пересчитывались",
)
_READY_ONLY_SUPPRESSED_SUBSTRINGS = (
    "Значительная часть страницы представлена изображением; требуется OCR всей страницы",
    "не подтверждён повторным OCR области",
    "ручные надстрочные маркеры; требуется проверка",
)


def filter_user_warnings(
    warnings: list[str],
    *,
    status: str | None = None,
    needs_review: bool = False,
) -> list[str]:
    """Strip noisy informational diagnostics while keeping real actionable warnings."""
    is_ready = status == "ready" and not needs_review
    filtered: list[str] = []
    for raw in warnings or ():
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        if any(marker in text for marker in _ALWAYS_NOISY_WARNING_SUBSTRINGS):
            continue
        if is_ready and any(marker in text for marker in _READY_ONLY_SUPPRESSED_SUBSTRINGS):
            continue
        if text not in filtered:
            filtered.append(text)
    return filtered
