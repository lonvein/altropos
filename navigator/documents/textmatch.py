"""Word-level alignment of a quoted text against positioned page text.

The matcher works on *tokens*: normalized words that carry their own box and the index
of the visual line they sit on. Matching is done on the token sequence, never on lines,
so a quote that starts or ends in the middle of a line highlights only its own words,
a hyphenated word split across lines still matches, and OCR slips (``пуикт`` for
``пункт``) or reviewer corrections do not push the match onto neighbouring lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher, get_close_matches
from typing import Any, Iterable, Sequence

_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_HYPHENS = "-‐‑­¬"
_MIN_FUZZY_COVERAGE = 0.6
_FUZZY_TOKEN_CUTOFF = 0.82
_FUZZY_BUDGET = 80_000

Box = list[float]


def normalize(text: str) -> str:
    cleaned = (text or "").replace(" ", " ").replace(" ", " ").replace(" ", " ").replace("­", "")
    cleaned = cleaned.replace("ё", "е").replace("Ё", "Е")
    cleaned = cleaned.replace("—", "-").replace("–", "-").replace("‑", "-")
    cleaned = (cleaned.replace("«", '"').replace("»", '"').replace("“", '"').replace("”", '"')
               .replace("’", "'").replace("ﬁ", "fi").replace("ﬂ", "fl"))
    return _SPACE_RE.sub(" ", cleaned).strip().lower()


def words_of(text: str) -> list[str]:
    return _WORD_RE.findall(normalize(text))


@dataclass
class Token:
    norm: str
    line: int
    boxes: list[Box]
    text: str = ""
    # ``True`` when the token is the first half of a word hyphenated across a line break.
    joins_next: bool = False


@dataclass
class Span:
    tokens: list[Token]
    coverage: float
    exact: bool
    first: int = 0
    last: int = 0
    notes: list[str] = field(default_factory=list)


def _sub_box(box: Box, start: int, end: int, total: int) -> Box:
    """Slice a horizontal box proportionally to a character range (single-line text)."""
    if total <= 0:
        return list(box)
    width = box[2] - box[0]
    x0 = box[0] + width * start / total
    x1 = box[0] + width * end / total
    return [round(x0, 2), box[1], round(max(x1, x0 + 0.5), 2), box[3]]


def tokens_from_text(text: str, box: Box, line: int, *, vertical: bool = False) -> list[Token]:
    """Split one positioned text run into word tokens with proportional sub-boxes."""
    if vertical:
        # Rotated text: boxes cannot be sliced reliably along x; keep a single token per run.
        parts = words_of(text)
        return [Token(norm=part, line=line, boxes=[list(box)], text=part) for part in parts]
    raw = text or ""
    total = len(raw)
    out: list[Token] = []
    for chunk in re.finditer(r"\S+", raw):
        matches = list(_WORD_RE.finditer(chunk.group(0)))
        for position, match in enumerate(matches):
            start, end = chunk.start() + match.start(), chunk.start() + match.end()
            # Leading/trailing punctuation of the printed word belongs to its highlight.
            if position == 0:
                start = chunk.start()
            if position == len(matches) - 1:
                end = chunk.end()
            norm = normalize(match.group(0))
            for piece in _WORD_RE.findall(norm):
                out.append(Token(norm=piece, line=line, boxes=[_sub_box(box, start, end, total)],
                                 text=match.group(0)))
    if out and raw.rstrip()[-1:] in _HYPHENS and len(raw.rstrip()) > 1:
        out[-1].joins_next = True
    return out


def merge_hyphenation(tokens: Sequence[Token]) -> list[Token]:
    """Fuse ``сло-`` / ``во`` (line break) into one token that still owns both boxes."""
    merged: list[Token] = []
    skip = False
    for index, token in enumerate(tokens):
        if skip:
            skip = False
            continue
        nxt = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.joins_next and nxt is not None and nxt.line != token.line and nxt.norm[:1].islower():
            merged.append(Token(norm=token.norm + nxt.norm, line=token.line,
                                boxes=[*token.boxes, *nxt.boxes], text=token.text + nxt.text))
            skip = True
        else:
            merged.append(token)
    return merged


def _exact_runs(cand: list[str], query: list[str]) -> list[int]:
    if not query or len(query) > len(cand):
        return []
    first, size = query[0], len(query)
    return [i for i in range(len(cand) - size + 1) if cand[i] == first and cand[i:i + size] == query]


def _snap_tokens(cand: list[str], query: list[str]) -> list[str]:
    """Replace near-miss candidate tokens (OCR slips) with the query token they resemble."""
    qset = set(query)
    unique = sorted(qset)
    if len(cand) * len(unique) > _FUZZY_BUDGET:
        return cand
    snapped: list[str] = []
    cache: dict[str, str] = {}
    for token in cand:
        if token in qset or len(token) < 4:
            snapped.append(token)
            continue
        if token not in cache:
            close = get_close_matches(token, [q for q in unique if abs(len(q) - len(token)) <= 2],
                                      n=1, cutoff=_FUZZY_TOKEN_CUTOFF)
            cache[token] = close[0] if close else token
        snapped.append(cache[token])
    return snapped


def _fuzzy_span(cand: list[str], query: list[str]) -> tuple[int, int, float] | None:
    if len(query) < 3 or not cand:
        return None
    work = _snap_tokens(cand, query)
    blocks = [b for b in SequenceMatcher(None, work, query, autojunk=False).get_matching_blocks() if b.size]
    if not blocks:
        return None
    # Keep the densest chain: blocks of one token are only trusted next to a larger neighbour.
    kept = [b for b in blocks if b.size >= 2] or []
    if not kept and len(query) >= 3:
        return None
    first, last = kept[0], kept[-1]
    start, end = first.a, last.a + last.size
    matched = sum(b.size for b in kept)
    coverage = matched / len(query)
    span_len = end - start
    if coverage < _MIN_FUZZY_COVERAGE or span_len > max(len(query) * 1.6, len(query) + 6):
        return None
    return start, end, coverage


def align(tokens: Sequence[Token], query_text: str, *, near: Box | None = None) -> Span | None:
    """Find the token span that carries ``query_text``.

    ``near`` is an optional box (same frame as the tokens): among several exact
    occurrences the one closest to it wins, so repeated phrases highlight the cited one.
    """
    query = words_of(query_text)
    if not query or not tokens:
        return None
    best: Span | None = None
    for variant, seq in (("plain", list(tokens)), ("joined", merge_hyphenation(tokens))):
        cand = [t.norm for t in seq]
        hits = _exact_runs(cand, query)
        if hits:
            index = _closest(hits, seq, len(query), near)
            chosen = seq[index:index + len(query)]
            return Span(tokens=chosen, coverage=1.0, exact=True, first=index, last=index + len(query) - 1)
        fuzzy = _fuzzy_span(cand, query)
        if fuzzy is not None:
            start, end, coverage = fuzzy
            span = Span(tokens=list(seq[start:end]), coverage=coverage, exact=False, first=start, last=end - 1)
            if best is None or span.coverage > best.coverage:
                best = span
    return best


def _closest(hits: list[int], seq: Sequence[Token], size: int, near: Box | None) -> int:
    if near is None or len(hits) == 1:
        return hits[0]
    cx, cy = (near[0] + near[2]) / 2, (near[1] + near[3]) / 2

    def distance(i: int) -> float:
        box = seq[i].boxes[0]
        return abs((box[0] + box[2]) / 2 - cx) + 2 * abs((box[1] + box[3]) / 2 - cy)

    return min(hits, key=distance)


def line_boxes(span: Span) -> list[tuple[Box, str]]:
    """One rectangle per visual line covered by the span (union of its tokens' boxes)."""
    rows: list[list[tuple[Box, str]]] = []
    for token in span.tokens:
        for box in token.boxes:
            center = (box[1] + box[3]) / 2
            for row in rows:
                ref = row[0][0]
                if abs((ref[1] + ref[3]) / 2 - center) < 0.5 * min(ref[3] - ref[1], box[3] - box[1]):
                    row.append((box, token.text or token.norm))
                    break
            else:
                rows.append([(box, token.text or token.norm)])
    result: list[tuple[Box, str]] = []
    for row in rows:
        boxes = [b for b, _ in row]
        result.append(([round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2),
                        round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2)],
                       " ".join(t for _, t in row)))
    result.sort(key=lambda item: (item[0][1], item[0][0]))
    return result


def tokens_for_lines(lines: Iterable[dict[str, Any]]) -> list[Token]:
    """Tokens for stored/native line dicts: ``{"text","bbox"[, "words":[{"text","bbox"}]]}``."""
    tokens: list[Token] = []
    for index, line in enumerate(lines):
        box = line.get("bbox")
        if not box:
            continue
        words = [w for w in (line.get("words") or []) if isinstance(w, dict) and w.get("bbox") and w.get("text")]
        line_tokens: list[Token] = []
        if words:
            for word in words:
                line_tokens.extend(tokens_from_text(str(word["text"]), list(word["bbox"]), index))
        else:
            line_tokens = tokens_from_text(str(line.get("text") or ""), list(box), index)
        if line_tokens and str(line.get("text") or "").rstrip()[-1:] in _HYPHENS:
            line_tokens[-1].joins_next = True
        tokens.extend(line_tokens)
    return tokens
