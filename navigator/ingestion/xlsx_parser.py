"""XLSX: лист, диапазон, подписи колонок, формулы и сохранённые значения.

Формулы НЕ пересчитываются. Если у ячейки с формулой нет сохранённого результата
(файл не открывали в Excel после правки), это явно написано в тексте и предупреждении;
ноль не подставляется.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from openpyxl.utils import get_column_letter

from navigator.ingestion.common import ParseError, Unit
from navigator.ingestion.model import Parsed

MAX_CELLS = 2_000_000  # защита от «раздутых» листов


def _fmt(value) -> str:
    if value is None:
        return ""
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
    incomplete = False

    for ws in wb_formulas.worksheets:
        values_ws = wb_values[ws.title]
        row_limit = min(ws.max_row, 20000)
        col_limit = min(ws.max_column, max(1, MAX_CELLS // max(1, row_limit)))
        if ws.max_row > row_limit or ws.max_column > col_limit:
            incomplete = True
            warnings.append(f"Лист «{ws.title}»: обработка ограничена {row_limit} строками и "
                            f"{col_limit} столбцами из {ws.dimensions}; требуется проверка")
        rows: list[tuple[int, list[tuple[int, str, str | None]]]] = []  # row, [(col, formula_or_value, cached)]
        blank_rows = []
        for row in ws.iter_rows(max_row=row_limit, max_col=col_limit):
            cells = []
            for cell in row:
                if cell.value is None or (isinstance(cell.value, str) and not cell.value.strip()):
                    continue
                cached = values_ws[cell.coordinate].value
                cells.append((cell.column, cell.value, cached))
            if cells:
                rows.append((row[0].row, cells))
            else:
                blank_rows.append(row[0].row)
        if not rows:
            warnings.append(f"Лист «{ws.title}» пустой")
            blocks.append({"sheet": ws.title, "dimensions": ws.dimensions,
                           "state": ws.sheet_state, "rows": [], "blank_rows": blank_rows,
                           "merged_ranges": [str(m) for m in ws.merged_cells.ranges]})
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

        # Keep gaps and the complete header span. A blank is not zero and an
        # anonymized literal must never be replaced by an inferred real value.
        expanded = []
        for index, (row_no, cells) in enumerate(rows):
            by_col = {c: (v, cv) for c, v, cv in cells}
            c0, c1 = min(by_col), max(by_col)
            if header_idx is not None and index >= header_idx:
                c0, c1 = min(c0, min(headers)), max(c1, max(headers))
            expanded.append((row_no, [(c, *by_col.get(c, (None, None))) for c in range(c0, c1 + 1)]))
        rows = expanded
        merged = list(ws.merged_cells.ranges)

        def cell_record(row_no, col, value, cached):
            ref = f"{get_column_letter(col)}{row_no}"
            anchor = next((f"{get_column_letter(m.min_col)}{m.min_row}" for m in merged
                           if m.min_row <= row_no <= m.max_row and m.min_col <= col <= m.max_col
                           and ref != f"{get_column_letter(m.min_col)}{m.min_row}"), None)
            placeholder = isinstance(value, str) and re.fullmatch(
                r"(?:[xхXХ*]{2,}|\[\s*(?:скрыто|обезличено)\s*\]|<[^<>]+>)", value.strip(), re.I)
            state = ("merged" if anchor else "empty" if value is None else "formula" if _is_formula(value)
                     else "placeholder" if placeholder else "value")
            return {"col": col, "ref": ref, "state": state, "merged_from": anchor,
                    "text": _fmt(value) if not _is_formula(value) else None,
                    "formula": value if _is_formula(value) else None,
                    "cached": None if cached is None else _fmt(cached)}

        blocks.append({"sheet": ws.title, "dimensions": ws.dimensions, "state": ws.sheet_state,
                       "merged_ranges": [str(m) for m in merged], "blank_rows": blank_rows,
                       "rows": [{"row": r, "cells": [cell_record(r, c, v, cv) for c, v, cv in cells]}
                                for r, cells in rows]})

        missing: list[str] = []
        sheet_path = (f"Лист «{ws.title}»",)

        def render(row_no: int, cells) -> tuple[str, dict]:
            parts = []
            for col, value, cached in cells:
                in_table = header_idx is not None and row_no > rows[header_idx][0]
                key = headers.get(col) if in_table else None
                label = key or get_column_letter(col)
                if value is None:
                    ref = f"{get_column_letter(col)}{row_no}"
                    record = cell_record(row_no, col, value, cached)
                    note = f"объединено с {record['merged_from']}" if record["merged_from"] else "нет значения"
                    parts.append(f"{label} ({ref}): {note}")
                    continue
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
    wb_formulas.close()
    wb_values.close()
    return Parsed(media_type="xlsx", title=title, units=units, blocks=blocks, warnings=warnings,
                  needs_review=not units or incomplete)
