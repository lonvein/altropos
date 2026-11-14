"""Context-window packing and the *material* limits of an answer.

Dropping a retrieved fragment from the prompt is not, by itself, a reason to doubt an
answer: low-ranked hits, near-duplicates of overlapping chunks and fragments the model
did not need say nothing about completeness. Only an omission that can change the
answer is a real limitation:

* a comparably relevant fragment that is *not* already covered by the selected text; or
* a selected fragment that had to be cut (its text is no longer whole).

Everything else must not weaken ``Verification.complete`` or raise a warning.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from math import ceil, exp

from navigator.schemas import Fragment, SearchHit

# Qwen-family tokenizers spend ~2.6-3.2 characters per token on Russian prose. The real
# tokenizer (``/tokenize``) is preferred; this estimate is only the offline fallback and
# is deliberately a little pessimistic, not 2x pessimistic.
CHARS_PER_TOKEN = 2.4
MATERIAL_RELATIVE_SCORE = 0.5
COVERED_SHARE = 0.8

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset({
    "как", "что", "это", "для", "или", "при", "про", "без", "над", "под", "его", "её", "они",
    "какой", "какая", "какие", "каком", "каких", "который", "которая", "которые", "быть", "если",
})


def estimate_tokens(text: str) -> int:
    return ceil(len(text) / CHARS_PER_TOKEN)


def payload_json(fragments: Sequence[Fragment]) -> str:
    """Exactly what the LLM client measures against ``context_budget``."""
    return json.dumps([f.model_dump(mode="json") for f in fragments], ensure_ascii=False)


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower().replace("ё", "е")) if len(w) > 1]


def _shingles(words: list[str], size: int = 3) -> set[tuple[str, ...]]:
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def covered_by(omitted: Fragment, selected: Iterable[Fragment]) -> bool:
    """True when the omitted text is (almost) entirely present in the selected fragments.

    Overlapping search chunks and repeated clauses make this common; such an omission
    loses no information.
    """
    own = _shingles(_words(omitted.text))
    if not own:
        return True
    known: set[tuple[str, ...]] = set()
    for fragment in selected:
        known |= _shingles(_words(fragment.text))
    return len(own & known) / len(own) >= COVERED_SHARE


def _relevance(hit: SearchHit) -> float:
    """Comparable relevance in [0, 1] for every score kind."""
    if hit.score_kind == "reranker":
        # Cross-encoder logits: >0 means "relevant" in the model's own calibration.
        return 1.0 / (1.0 + exp(-max(-30.0, min(30.0, hit.score))))
    return hit.score


def _shares_question_terms(fragment: Fragment, question: str, title: str = "") -> bool:
    terms = [w for w in _words(question) if w not in _STOP and len(w) >= 3]
    if not terms:
        return True
    haystack = set(_words(fragment.text)) | set(_words(title))
    for term in terms:
        stem = term[:max(4, len(term) - 2)] if len(term) >= 5 else term
        if term in haystack or any(word.startswith(stem) for word in haystack):
            return True
    return False


def material_omissions(
    hits: Sequence[SearchHit],
    selected: Sequence[Fragment],
    question: str,
    titles: dict[str, str] | None = None,
) -> list[Fragment]:
    """Retrieved fragments that were left out of the prompt *and* could change the answer."""
    selected_ids = {f.id for f in selected}
    omitted = [h for h in hits if h.fragment.id not in selected_ids]
    if not omitted:
        return []
    scores = [_relevance(h) for h in hits]
    top = max(scores) if scores else 0.0
    result: list[Fragment] = []
    for hit in omitted:
        relevance = _relevance(hit)
        if hit.score_kind == "reranker":
            relevant = relevance >= 0.5 and relevance >= MATERIAL_RELATIVE_SCORE * top
        else:
            relevant = top > 0 and relevance >= MATERIAL_RELATIVE_SCORE * top
        if not relevant:
            continue
        if not _shares_question_terms(hit.fragment, question, (titles or {}).get(hit.fragment.id, "")):
            continue
        if covered_by(hit.fragment, selected):
            continue
        result.append(hit.fragment)
    return result


_PROCEDURE_MARKERS = (
    "как ", "каков порядок", "порядок ", "процедур", "алгоритм", "этап", "шаг", "последовательност",
    "что нужно сделать", "что необходимо", "какие документы", "куда обратиться", "как получить",
    "как оформить", "как подать", "в какие сроки", "в течение какого",
)


def is_procedural_question(question: str) -> bool:
    low = " " + " ".join((question or "").lower().split())
    return any(marker in low for marker in _PROCEDURE_MARKERS)
