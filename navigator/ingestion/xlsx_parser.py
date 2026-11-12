"""XLSX: лист, диапазон, подписи колонок, формулы и сохранённые значения.

Формулы НЕ пересчитываются. Если у ячейки с формулой нет сохранённого результата
(файл не открывали в Excel после правки), это явно написано в тексте и предупреждении;
ноль не подставляется.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from openpyxl.utils import get_column_letter

from navigator.ingestion.common import ParseError, Unit
from navigator.ingestion.model import Parsed

MAX_CELLS = 2_000_000  # защита от «раздутых» листов


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, float):
        return f"{value:.10g}"
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%d.%m.%Y")
    return " ".join(str(value).replace("\u00a0", " ").split())


def _short(formula: str, limit: int = 70) -> str:
    return formula if len(formula) <= limit else formula[:limit] + "…"


def _is_formula(value) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _rng(row: int, c0: int, c1: int) -> str:
    a = f"{get_column_letter(c0)}{row}"
    return a if c0 == c1 else f"{a}:{get_column_letter(c1)}{row}"


def parse_xlsx(path: Path) -> Parsed:
    try:
        import openpyxl
        wb_formulas = openpyxl.load_workbook(str(path), data_only=False)
        wb_values = openpyxl.load_workbook(str(path), data_only=True)
    except Exception as exc:
        raise ParseError("Не удалось прочитать XLSX: файл повреждён, защищён паролем или имеет неверный формат") from exc

    units: list[Unit] = []
    warnings: list[str] = []
    blocks: list[dict] = []
    title = Path(path).stem

    for ws in wb_formulas.worksheets:
        values_ws = wb_values[ws.title]
        if ws.max_row * ws.max_column > MAX_CELLS:
            warnings.append(f"Лист «{ws.title}» слишком большой ({ws.dimensions}), разобрана только часть")
        rows: list[tuple[int, list[tuple[int, str, str | None]]]] = []  # row, [(col, formula_or_value, cached)]
        for row in ws.iter_rows(max_row=min(ws.max_row, 20000)):
            cells = []
            for cell in row:
                if cell.value is None or (isinstance(cell.value, str) and not cell.value.strip()):
                    continue
                cached = values_ws[cell.coordinate].value
                cells.append((cell.column, cell.value, cached))
            if cells:
                rows.append((row[0].row, cells))
        blocks.append({"sheet": ws.title, "dimensions": ws.dimensions,
                       "state": ws.sheet_state, "rows": [
                           {"row": r, "cells": [{"col": c, "ref": f"{get_column_letter(c)}{r}",
                                                 "text": _fmt(v) if not _is_formula(v) else None,
                                                 "formula": v if _is_formula(v) else None,
                                                 "cached": None if cv is None else _fmt(cv)}
                                                for c, v, cv in cells]} for r, cells in rows]})
        if not rows:
            warnings.append(f"Лист «{ws.title}» пустой")
            continue

        # Шапка таблицы: первая строка с >=3 непустыми текстовыми ячейками без формул.
        header_idx = None
        for i, (_, cells) in enumerate(rows[:40]):
            if len(cells) >= 3 and all(isinstance(v, str) and not _is_formula(v) for _, v, _ in cells) \
                    and i + 1 < len(rows):
                header_idx = i
                break
        headers: dict[int, str] = {}
        if header_idx is not None:
            headers = {c: _fmt(v) for c, v, _ in rows[header_idx][1]}

        missing: list[str] = []
        sheet_path = (f"Лист «{ws.title}»",)

        def render(row_no: int, cells) -> tuple[str, dict]:
            parts = []
            for col, value, cached in cells:
                in_table = header_idx is not None and row_no > rows[header_idx][0]
                key = headers.get(col) if in_table else None
                label = key or get_column_letter(col)
                if in_table and col == min(headers, default=col) and not _is_formula(value):
                    parts.append(_fmt(value))  # колонка с названием строки — без подписи
                    continue
                if _is_formula(value):
                    if cached is None:
                        parts.append(f"{label} ({get_column_letter(col)}{row_no}): формула {_short(value)}, "
                                     f"значение не сохранено в файле (не пересчитывалось)")
                        missing.append(f"{get_column_letter(col)}{row_no}")
                    else:
                        parts.append(f"{label} ({get_column_letter(col)}{row_no}): {_fmt(cached)} "
                                     f"(формула {_short(value)})")
                else:
                    parts.append(f"{label}: {_fmt(value)}" if key else _fmt(value) if col == cells[0][0]
                                 else f"{label}{row_no}: {_fmt(value)}")
            c0, c1 = cells[0][0], cells[-1][0]
            return " | ".join(parts), {"kind": "xlsx", "sheet": ws.title, "range": _rng(row_no, c0, c1)}

        # Строки до шапки — обычные абзацы (заголовок формы, реквизиты).
        pre_end = header_idx if header_idx is not None else len(rows)
        for row_no, cells in rows[:pre_end]:
            text, loc = render(row_no, cells)
            units.append(Unit(text=text, locator=loc, path=sheet_path, atomic=True))

        if header_idx is not None:
            h_no, h_cells = rows[header_idx]
            header_text, header_loc = render(h_no, h_cells)
            table_rows, row_locs = [], []
            for row_no, cells in rows[header_idx + 1:]:
                text, loc = render(row_no, cells)
                table_rows.append(text)
                row_locs.append(loc)
            header_line = "Колонки: " + " | ".join(headers[c] for c in sorted(headers))
            units.append(Unit(text="\n".join(table_rows), locator=row_locs[0], kind="table", path=sheet_path,
                              table_header=header_line, rows=table_rows, row_locators=row_locs))
        if missing:
            shown = ", ".join(missing[:8]) + ("…" if len(missing) > 8 else "")
            warnings.append(f"Лист «{ws.title}»: {len(missing)} формул(ы) без сохранённого значения "
                            f"({shown}); значения не пересчитывались")

    if not units:
        warnings.append("В книге не найдено данных")
    return Parsed(media_type="xlsx", title=title, units=units, blocks=blocks, warnings=warnings,
                  needs_review=not units)
