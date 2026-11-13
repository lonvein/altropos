"""XLSX: лист, диапазон, подписи колонок, формулы и сохранённые/вычисленные значения.

Сохранённые значения приоритетны. При их отсутствии поддерживаемые формулы
детерминированно вычисляются по заполненным данным; для незаполненных бланков
или неподдерживаемых выражений фиктивный ноль не подставляется.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from pathlib import Path

from openpyxl.utils import column_index_from_string, get_column_letter

from navigator.ingestion.common import ParseError, Unit
from navigator.ingestion.model import Parsed

MAX_CELLS = 2_000_000  # защита от «раздутых» листов
_BLANK = object()
_UNRESOLVED = object()


class _ExcelError(str):
    """Explicit Excel formula error (#REF!, #DIV/0!, etc.)."""


_CELL_REF_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_TOKEN_RE = re.compile(
    r"""
    \s+
    | (?P<STR>"(?:""|[^"])*")
    | (?P<QSHEET>'(?:''|[^'])*'!)
    | (?P<ERR>\#(?:REF!|DIV/0!|VALUE!|NAME\?|N/A|NUM!|NULL!))
    | (?P<OP><=|>=|<>|[+\-*/^&=<>(),;:])
    | (?P<NUM>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?%?)
    | (?P<IDENT>\$?[A-Za-z_А-Яа-яЁё][A-Za-z0-9_А-Яа-яЁё.$]*!?)
    """,
    re.VERBOSE,
)


def _is_num(val) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _as_float(val) -> float:
    return float(val)


def _tokenize_formula(formula: str) -> list[tuple[str, str]]:
    expr = formula[1:].strip() if formula.startswith("=") else formula.strip()
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ValueError(f"Unexpected token at {pos}: {expr[pos:pos+10]}")
        pos = m.end()
        kind = m.lastgroup
        if not kind:
            continue
        text = m.group(kind)
        if kind == "STR":
            tokens.append(("STR", text[1:-1].replace('""', '"')))
        elif kind == "QSHEET":
            tokens.append(("SHEET", text[1:-2].replace("''", "'")))
        elif kind == "IDENT" and text.endswith("!"):
            tokens.append(("SHEET", text[:-1]))
        elif kind == "NUM":
            tokens.append(("NUM", text))
        else:
            tokens.append((kind, text))
    return tokens


class _FormulaEvaluator:
    """Safe deterministic evaluator without fabricated zeroes for blank templates."""

    def __init__(self, wb_formulas, wb_values):
        self.wb_formulas = wb_formulas
        self.wb_values = wb_values
        self.cache: dict[tuple[str, int, int], object] = {}
        self.visiting: set[tuple[str, int, int]] = set()
        self.sheet_names = {ws.title: ws.title for ws in wb_formulas.worksheets}
        self.sheet_names_ci = {ws.title.lower(): ws.title for ws in wb_formulas.worksheets}

    def resolve_sheet(self, name: str | None, default_sheet: str) -> str | None:
        if name is None:
            return default_sheet
        return self.sheet_names.get(name) or self.sheet_names_ci.get(name.lower())

    def eval_cell(self, sheet: str, row: int, col: int):
        key = (sheet, row, col)
        if key in self.cache:
            return self.cache[key]
        if key in self.visiting:
            return _UNRESOLVED
        if sheet not in self.wb_formulas.sheetnames:
            return _UNRESOLVED
        ws_f = self.wb_formulas[sheet]
        ws_v = self.wb_values[sheet]
        cached = ws_v.cell(row=row, column=col).value
        raw = ws_f.cell(row=row, column=col).value
        if cached is not None:
            if isinstance(cached, str) and cached.startswith("#"):
                res = _ExcelError(cached)
            else:
                res = cached
            self.cache[key] = res
            return res
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            self.cache[key] = _BLANK
            return _BLANK
        if not _is_formula(raw):
            self.cache[key] = raw
            return raw
        self.visiting.add(key)
        try:
            res = self.eval_formula(raw, sheet)
        except Exception:
            res = _UNRESOLVED
        finally:
            self.visiting.discard(key)
        self.cache[key] = res
        return res

    def evaluate_top(self, raw: str, sheet: str, row: int, col: int):
        res = self.eval_cell(sheet, row, col)
        if res is _BLANK or res is _UNRESOLVED or isinstance(res, _ExcelError):
            return None
        if isinstance(res, float) and abs(res - round(res)) < 1e-9:
            return int(round(res))
        return res

    def eval_formula(self, formula: str, sheet: str):
        tokens = _tokenize_formula(formula)
        parser = _ExprParser(tokens, self, sheet)
        val = parser.parse_expr()
        if parser.pos != len(tokens):
            return _UNRESOLVED
        return val


class _ExprParser:
    def __init__(self, tokens: list[tuple[str, str]], ctx: _FormulaEvaluator, sheet: str):
        self.tokens = tokens
        self.pos = 0
        self.ctx = ctx
        self.sheet = sheet

    def peek(self, offset: int = 0) -> tuple[str, str] | None:
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else None

    def consume(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def match(self, kind: str, val: str | None = None) -> bool:
        tok = self.peek()
        if tok and tok[0] == kind and (val is None or tok[1] == val):
            self.pos += 1
            return True
        return False

    def parse_expr(self):
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_concat()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in ("=", "<>", "<", "<=", ">", ">="):
            op = self.consume()[1]
            right = self.parse_concat()
            left = self._eval_cmp(op, left, right)
        return left

    def _eval_cmp(self, op: str, a, b):
        if isinstance(a, _ExcelError):
            return a
        if isinstance(b, _ExcelError):
            return b
        if a is _UNRESOLVED or b is _UNRESOLVED or isinstance(a, tuple) or isinstance(b, tuple):
            return _UNRESOLVED
        if a is _BLANK and b is _BLANK:
            a, b = "", ""
        elif a is _BLANK:
            a = 0 if _is_num(b) else ""
        elif b is _BLANK:
            b = 0 if _is_num(a) else ""
        if isinstance(a, str) and isinstance(b, str):
            a_cmp, b_cmp = a.lower(), b.lower()
        elif _is_num(a) and _is_num(b):
            a_cmp, b_cmp = float(a), float(b)
        elif isinstance(a, bool) and isinstance(b, bool):
            a_cmp, b_cmp = a, b
        elif op in ("=", "<>"):
            return (op == "<>") if type(a) is not type(b) else (a == b if op == "=" else a != b)
        else:
            return _UNRESOLVED
        if op == "=":
            return a_cmp == b_cmp
        if op == "<>":
            return a_cmp != b_cmp
        if op == "<":
            return a_cmp < b_cmp
        if op == "<=":
            return a_cmp <= b_cmp
        if op == ">":
            return a_cmp > b_cmp
        return a_cmp >= b_cmp

    def parse_concat(self):
        left = self.parse_add()
        while self.match("OP", "&"):
            right = self.parse_add()
            if isinstance(left, _ExcelError):
                return left
            if isinstance(right, _ExcelError):
                return right
            if left is _UNRESOLVED or right is _UNRESOLVED:
                return _UNRESOLVED
            ls = "" if left is _BLANK else _fmt(left)
            rs = "" if right is _BLANK else _fmt(right)
            left = ls + rs
        return left

    def parse_add(self):
        left = self.parse_mul()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in ("+", "-"):
            op = self.consume()[1]
            right = self.parse_mul()
            left = self._eval_arith(op, left, right)
        return left

    def parse_mul(self):
        left = self.parse_pow()
        while (tok := self.peek()) and tok[0] == "OP" and tok[1] in ("*", "/"):
            op = self.consume()[1]
            right = self.parse_pow()
            left = self._eval_arith(op, left, right)
        return left

    def parse_pow(self):
        left = self.parse_unary()
        if self.match("OP", "^"):
            right = self.parse_pow()
            return self._eval_arith("^", left, right)
        return left

    def _eval_arith(self, op: str, a, b):
        if isinstance(a, _ExcelError):
            return a
        if isinstance(b, _ExcelError):
            return b
        if a is _UNRESOLVED or b is _UNRESOLVED:
            return _UNRESOLVED
        if a is _BLANK and b is _BLANK:
            if op == "/":
                return _ExcelError("#DIV/0!")
            return _UNRESOLVED
        if op in ("+", "-"):
            if a is _BLANK and _is_num(b):
                a = 0.0
            elif b is _BLANK and _is_num(a):
                b = 0.0
            elif a is _BLANK or b is _BLANK:
                return _UNRESOLVED
        else:
            if op == "/" and (b is _BLANK or (_is_num(b) and float(b) == 0.0)):
                return _ExcelError("#DIV/0!")
            if a is _BLANK or b is _BLANK:
                return _UNRESOLVED
        if not (_is_num(a) and _is_num(b)):
            return _UNRESOLVED
        fa, fb = float(a), float(b)
        if op == "+":
            return fa + fb
        if op == "-":
            return fa - fb
        if op == "*":
            return fa * fb
        if op == "/":
            return _ExcelError("#DIV/0!") if fb == 0.0 else fa / fb
        return fa ** fb

    def parse_unary(self):
        if self.match("OP", "+"):
            val = self.parse_unary()
            return val if (val is _BLANK or val is _UNRESOLVED or isinstance(val, _ExcelError)) else (+float(val) if _is_num(val) else _UNRESOLVED)
        if self.match("OP", "-"):
            val = self.parse_unary()
            if val is _BLANK or val is _UNRESOLVED or isinstance(val, _ExcelError):
                return val
            return -float(val) if _is_num(val) else _UNRESOLVED
        return self.parse_primary()

    def _parse_range_values(self, sheet: str, r1: int, c1: int, r2: int, c2: int) -> list:
        target_sheet = self.ctx.resolve_sheet(sheet, self.sheet)
        if target_sheet is None or target_sheet not in self.ctx.wb_formulas.sheetnames:
            return [_UNRESOLVED]
        ws = self.ctx.wb_formulas[target_sheet]
        r_lo, r_hi = sorted((r1, r2))
        c_lo, c_hi = sorted((c1, c2))
        max_r = ws.max_row or 1
        max_c = ws.max_column or 1
        if r_lo > max_r or c_lo > max_c or r_hi < 1 or c_hi < 1:
            return [_BLANK]
        rr1, rr2 = max(1, r_lo), min(max_r, r_hi)
        cc1, cc2 = max(1, c_lo), min(max_c, c_hi)
        if (rr2 - rr1 + 1) * (cc2 - cc1 + 1) > 25_000:
            return [_UNRESOLVED]
        vals = []
        for r in range(rr1, rr2 + 1):
            for c in range(cc1, cc2 + 1):
                vals.append(self.ctx.eval_cell(target_sheet, r, c))
        return vals

    def parse_primary(self, as_range: bool = False):
        tok = self.peek()
        if not tok:
            return _UNRESOLVED
        kind, val = tok
        if kind == "OP" and val == "(":
            self.consume()
            inner = self.parse_expr()
            if not self.match("OP", ")"):
                return _UNRESOLVED
            return inner
        if kind == "ERR":
            self.consume()
            return _ExcelError(val)
        if kind == "NUM":
            self.consume()
            if val.endswith("%"):
                return float(val[:-1]) / 100.0
            return float(val) if ("." in val or "e" in val.lower()) else int(val)
        if kind == "STR":
            self.consume()
            return val
        if kind == "SHEET":
            sheet_name = self.consume()[1]
            return self._parse_ref(sheet_name, as_range=as_range)
        if kind == "IDENT":
            upper = val.upper()
            nxt = self.peek(1)
            if nxt == ("OP", "("):
                self.consume()
                return self._eval_func(upper)
            if _CELL_REF_RE.match(val):
                return self._parse_ref(self.sheet, as_range=as_range)
            self.consume()
            if upper in ("TRUE", "ИСТИНА"):
                return True
            if upper in ("FALSE", "ЛОЖЬ"):
                return False
            return _UNRESOLVED
        return _UNRESOLVED

    def _parse_ref(self, sheet_name: str, as_range: bool = False):
        tok1 = self.peek()
        if not tok1 or tok1[0] != "IDENT":
            return _UNRESOLVED
        m1 = _CELL_REF_RE.match(tok1[1])
        if not m1:
            return _UNRESOLVED
        self.consume()
        c1 = column_index_from_string(m1.group(1).upper())
        r1 = int(m1.group(2))
        if self.match("OP", ":"):
            tok2 = self.peek()
            if not tok2 or tok2[0] != "IDENT":
                return _UNRESOLVED
            m2 = _CELL_REF_RE.match(tok2[1])
            if not m2:
                return _UNRESOLVED
            self.consume()
            c2 = column_index_from_string(m2.group(1).upper())
            r2 = int(m2.group(2))
            if as_range:
                return self._parse_range_values(sheet_name, r1, c1, r2, c2)
            return _UNRESOLVED
        target_sheet = self.ctx.resolve_sheet(sheet_name, self.sheet)
        if target_sheet is None:
            return _UNRESOLVED
        val = self.ctx.eval_cell(target_sheet, r1, c1)
        return [val] if as_range else val

    def _eval_arg_as_list(self) -> list:
        tok = self.peek()
        if tok and tok[0] in ("SHEET", "IDENT") and _CELL_REF_RE.match(
            (self.peek(1) or ("", ""))[1] if tok[0] == "SHEET" else tok[1]
        ):
            nxt_after = self.peek(2 if tok[0] == "SHEET" else 1)
            if nxt_after == ("OP", ":"):
                val = self.parse_primary(as_range=True)
                return val if isinstance(val, list) else [val]
        val = self.parse_expr()
        return val if isinstance(val, list) else [val]

    def _skip_expr_tokens(self):
        depth = 0
        while (tok := self.peek()):
            if tok == ("OP", "("):
                depth += 1
            elif tok == ("OP", ")"):
                if depth == 0:
                    return
                depth -= 1
            elif depth == 0 and tok[0] == "OP" and tok[1] in (",", ";"):
                return
            self.consume()

    def _eval_func(self, name: str):
        self.consume()  # consume '('
        if name in ("IF", "ЕСЛИ"):
            cond = self.parse_expr() if not self.match("OP", ")") else _UNRESOLVED
            if not (self.match("OP", ",") or self.match("OP", ";")):
                return _UNRESOLVED
            if isinstance(cond, _ExcelError) or cond is _UNRESOLVED:
                self._skip_expr_tokens()
                if self.match("OP", ",") or self.match("OP", ";"):
                    self._skip_expr_tokens()
                self.match("OP", ")")
                return cond
            truthy = bool(cond) if cond is not _BLANK else False
            if truthy:
                res = self.parse_expr()
                if self.match("OP", ",") or self.match("OP", ";"):
                    self._skip_expr_tokens()
                self.match("OP", ")")
                return res
            self._skip_expr_tokens()
            if self.match("OP", ",") or self.match("OP", ";"):
                res = self.parse_expr()
                self.match("OP", ")")
                return res
            self.match("OP", ")")
            return False

        if name in ("IFERROR", "ЕСЛИОШИБКА"):
            first = self.parse_expr()
            if not (self.match("OP", ",") or self.match("OP", ";")):
                self.match("OP", ")")
                return _UNRESOLVED
            if isinstance(first, _ExcelError):
                fallback = self.parse_expr()
                self.match("OP", ")")
                return fallback
            self._skip_expr_tokens()
            self.match("OP", ")")
            return first

        if name == "ISERROR":
            val = self.parse_expr()
            self.match("OP", ")")
            return False if val is _UNRESOLVED else isinstance(val, _ExcelError)

        if name == "ISBLANK":
            val = self.parse_expr()
            self.match("OP", ")")
            return val is _BLANK if val is not _UNRESOLVED else _UNRESOLVED

        # Standard multi-argument eager functions
        args_flat: list = []
        args_raw: list = []
        if not self.match("OP", ")"):
            while True:
                items = self._eval_arg_as_list()
                args_raw.append(items)
                args_flat.extend(items)
                if self.match("OP", ",") or self.match("OP", ";"):
                    continue
                if not self.match("OP", ")"):
                    return _UNRESOLVED
                break

        for item in args_flat:
            if isinstance(item, _ExcelError):
                return item
        for item in args_flat:
            if item is _UNRESOLVED:
                return _UNRESOLVED

        if name in ("SUM", "СУММ", "AVERAGE", "СРЗНАЧ", "MIN", "МИН", "MAX", "МАКС"):
            nums = [float(x) for x in args_flat if _is_num(x)]
            if not nums:
                return _UNRESOLVED
            if name in ("SUM", "СУММ"):
                return sum(nums)
            if name in ("AVERAGE", "СРЗНАЧ"):
                return sum(nums) / len(nums)
            return min(nums) if name in ("MIN", "МИН") else max(nums)

        if name in ("COUNT", "СЧЁТ", "СЧЕТ"):
            return sum(1 for x in args_flat if _is_num(x))

        if name == "ABS":
            if len(args_raw) != 1 or not _is_num(args_raw[0][0]):
                return _UNRESOLVED
            return abs(float(args_raw[0][0]))

        if name in ("ROUND", "ОКРУГЛ"):
            if len(args_raw) != 2 or not _is_num(args_raw[0][0]) or not _is_num(args_raw[1][0]):
                return _UNRESOLVED
            val = float(args_raw[0][0])
            digits = int(args_raw[1][0])
            try:
                q = Decimal(10) ** (-digits)
                return float(Decimal(str(val)).quantize(q, rounding=ROUND_HALF_UP))
            except (InvalidOperation, ValueError):
                return round(val, digits)

        if name in ("AND", "И"):
            bools = [bool(x) for x in args_flat if x is not _BLANK]
            return all(bools) if bools else _UNRESOLVED

        if name in ("OR", "ИЛИ"):
            bools = [bool(x) for x in args_flat if x is not _BLANK]
            return any(bools) if bools else _UNRESOLVED

        if name in ("NOT", "НЕ"):
            if len(args_raw) != 1 or args_raw[0][0] is _BLANK:
                return _UNRESOLVED
            return not bool(args_raw[0][0])

        return _UNRESOLVED


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
    diagnostics: list[dict] = []
    title = Path(path).stem
    incomplete = False
    evaluator = _FormulaEvaluator(wb_formulas, wb_values)

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
            evaluated = (evaluator.evaluate_top(value, ws.title, row_no, col)
                         if (_is_formula(value) and cached is None) else None)
            return {"col": col, "ref": ref, "state": state, "merged_from": anchor,
                    "text": _fmt(value) if not _is_formula(value) else None,
                    "formula": value if _is_formula(value) else None,
                    "cached": None if cached is None else _fmt(cached),
                    "evaluated": None if evaluated is None else _fmt(evaluated)}

        blocks.append({"sheet": ws.title, "dimensions": ws.dimensions, "state": ws.sheet_state,
                       "merged_ranges": [str(m) for m in merged], "blank_rows": blank_rows,
                       "rows": [{"row": r, "cells": [cell_record(r, c, v, cv) for c, v, cv in cells]}
                                for r, cells in rows]})

        missing: list[str] = []
        evaluated_count = 0
        sheet_path = (f"Лист «{ws.title}»",)

        def render(row_no: int, cells) -> tuple[str, dict]:
            nonlocal evaluated_count
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
                    if cached is not None:
                        parts.append(f"{label} ({get_column_letter(col)}{row_no}): {_fmt(cached)} "
                                     f"(формула {_short(value)})")
                    else:
                        evaluated = evaluator.evaluate_top(value, ws.title, row_no, col)
                        if evaluated is not None:
                            evaluated_count += 1
                            fmt_eval = _fmt(evaluated)
                            if fmt_eval:
                                parts.append(f"{label} ({get_column_letter(col)}{row_no}): {fmt_eval} "
                                             f"(формула {_short(value)})")
                            else:
                                parts.append(f"{label} ({get_column_letter(col)}{row_no}): пусто "
                                             f"(формула {_short(value)})")
                        else:
                            parts.append(f"{label} ({get_column_letter(col)}{row_no}): формула {_short(value)}, "
                                         f"значение не сохранено в файле (не пересчитывалось)")
                            missing.append(f"{get_column_letter(col)}{row_no}")
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
        if missing or evaluated_count:
            diagnostics.append({
                "sheet": ws.title,
                "evaluated_formulas": evaluated_count,
                "uncached_formulas": missing,
            })

    if not units:
        warnings.append("В книге не найдено данных")
    wb_formulas.close()
    wb_values.close()
    return Parsed(media_type="xlsx", title=title, units=units, blocks=blocks, warnings=warnings,
                  needs_review=not units or incomplete, diagnostics=diagnostics)
