"""Request orchestration over pinned publications; no model or device loading."""
import json
import logging
import sqlite3
from contextlib import nullcontext
from datetime import date
from math import ceil
from time import perf_counter

from pydantic import ValidationError

from navigator.config import Settings
from navigator.db import Store, fragments_digest
from navigator.interfaces import ComponentError, LLMClient, Searcher
from navigator.rag.context import (
    estimate_tokens, is_procedural_question, material_omissions, payload_json,
)
from navigator.rag.retrieval import _substantive_tokens
from navigator.rag.validation import validate_answer, validate_draft
from navigator.schemas import (
    AskRequest, AskResponse, Document, Draft, Fragment, GenerationRequest, IndexManifest,
    MetadataPatch, SearchRequest, SearchResult, Source, Timings, Verification,
    VerificationRequest,
)

log = logging.getLogger(__name__)

CONTEXT_WARNING = "Контекст сокращён; полнота ответа не подтверждается."


def _is_definitional_question(question: str) -> bool:
    low = " ".join(question.lower().split())
    markers = (
        "что такое ",
        "что означает ",
        "что значит ",
        "что понимается под ",
        "что подразумевается под ",
        "что представляет собой ",
        "кто такой ",
        "кто такая ",
        "кто такие ",
        "каково определение ",
        "определение термина ",
        "расшифруй ",
        "расшифровка ",
    )
    return any(low.startswith(m) or f" {m}" in f" {low}" for m in markers)


def _context_tokens(candidate: list[Fragment]) -> int:
    """Offline estimate of what the client measures against ``context_budget``."""
    return estimate_tokens(payload_json(candidate))


def _compact_fragment_for_context(fragment: Fragment, question: str, max_chars: int) -> Fragment:
    """Return a verbatim contiguous sub-slice of fragment.text fits within max_chars."""
    text = fragment.text
    if len(text) <= max_chars:
        return fragment
    q_terms = [t for t in _substantive_tokens(question) if len(t) >= 3]
    lines = text.splitlines(keepends=True)
    if len(lines) > 1:
        best_idx = 0
        best_score = -1
        for idx, line in enumerate(lines):
            low = line.lower().replace("ё", "е")
            score = sum(
                1 for term in q_terms
                if (term[:max(4, len(term) - 2)] if len(term) >= 5 else term) in low
            )
            if score > best_score:
                best_score = score
                best_idx = idx
        start_idx = best_idx
        end_idx = best_idx + 1
        while True:
            cur_len = sum(len(lines[i]) for i in range(start_idx, end_idx))
            expanded = False
            if start_idx > 0 and cur_len + len(lines[start_idx - 1]) <= max_chars:
                start_idx -= 1
                cur_len += len(lines[start_idx])
                expanded = True
            if end_idx < len(lines) and cur_len + len(lines[end_idx]) <= max_chars:
                cur_len += len(lines[end_idx])
                end_idx += 1
                expanded = True
            if not expanded:
                break
        slice_text = "".join(lines[start_idx:end_idx]).strip()
        if slice_text and slice_text in text and len(slice_text) >= min(80, max_chars // 2):
            return fragment.model_copy(update={"text": slice_text})

    low_full = text.lower().replace("ё", "е")
    anchor = 0
    for term in q_terms:
        stem = term[:max(4, len(term) - 2)] if len(term) >= 5 else term
        pos = low_full.find(stem)
        if pos >= 0:
            anchor = pos
            break
    start = max(0, anchor - max_chars // 3)
    end = min(len(text), start + max_chars)
    if start > 0:
        ws = text.find(" ", start, min(len(text), start + 40))
        if ws > start:
            start = ws + 1
    if end < len(text):
        ws = text.rfind(" ", max(start + 1, end - 40), end)
        if ws > start:
            end = ws
    excerpt = text[start:end].strip()
    if not excerpt or excerpt not in text:
        excerpt = text[:max_chars].strip()
    return fragment.model_copy(update={"text": excerpt})


def _definition_complete_override(issues: list[str] | None) -> bool:
    if not issues:
        return True
    blockers = ("отсутств", "нет определ", "не указан", "не привед", "противореч", "неясно", "двусмысл")
    joined = " ".join(issues).lower()
    if any(b in joined for b in blockers):
        return False
    overreach = ("процедур", "шаг", "порядок", "этап", "инструкц", "разряд", "применен", "оформл")
    return any(o in joined for o in overreach)


def _filter_answer_warnings(warnings: list[str], cited_titles: set[str]) -> list[str]:
    prefix = "Период действия «"
    suffix = "» подтверждён не полностью."
    kept: list[str] = []
    for item in warnings:
        if item.startswith(prefix) and item.endswith(suffix):
            title = item[len(prefix):-len(suffix)]
            if title not in cited_titles:
                continue
        kept.append(item)
    return list(dict.fromkeys(kept))


class AnswerPipeline:
    def __init__(self, store: Store, settings: Settings, searcher: Searcher,
                 llm: LLMClient | None):
        self.store, self.settings, self.searcher, self.llm = store, settings, searcher, llm

    def _sources(self, result: SearchResult, as_of: date):
        """Read only named immutable publications, never newer active fragments.

        Metadata and readiness are checked in the same SQL statement. A reindex
        may change active_publication while the old publication stays readable.
        """
        if not result.hits:
            return [], [], False
        if not result.publication_ids:
            return [], [], bool(result.hits)
        placeholders = ",".join("?" for _ in result.publication_ids)
        document_ids = sorted({hit.fragment.document_id for hit in result.hits})
        doc_placeholders = ",".join("?" for _ in document_ids)
        rows = self.store.rows(f"""SELECT p.id,p.manifest,p.fragments,d.payload,
            d.title,d.metadata FROM publications p JOIN documents d ON d.id=p.document_id
            WHERE p.id IN ({placeholders}) AND d.id IN ({doc_placeholders})
            AND d.status='ready'""", [*result.publication_ids, *document_ids])
        trusted = {}
        for row in rows:
            manifest = IndexManifest.model_validate_json(row["manifest"])
            document = Document.model_validate_json(row["payload"])
            metadata = MetadataPatch.model_validate_json(row["metadata"])
            fragments = [Fragment.model_validate(f) for f in json.loads(row["fragments"])]
            if (manifest.publication_id != row["id"] or
                    (manifest.document_id, manifest.document_version) != (document.id, document.version) or
                    fragments_digest(fragments) != manifest.fragments_sha256 or
                    len({f.id for f in fragments}) != len(fragments) or
                    sorted(f.ordinal for f in fragments) != list(range(len(fragments))) or
                    any((f.document_id, f.document_version) != (document.id, document.version)
                        or f.locator.kind != document.media_type for f in fragments)):
                raise ValueError("Invalid publication snapshot")
            if metadata.reviewed and (
                metadata.valid_from and as_of < metadata.valid_from or
                metadata.valid_to and as_of > metadata.valid_to
            ):
                continue
            for fragment in fragments:
                if fragment.id in trusted and trusted[fragment.id][0] != fragment:
                    raise ValueError("Ambiguous fragment ID")
                trusted[fragment.id] = (fragment, row["title"])
        accepted, sources = [], []
        rejected = False
        for hit in result.hits:
            pair = trusted.get(hit.fragment.id)
            if pair is None or pair[0] != hit.fragment:
                rejected = True
                continue
            fragment, title = pair
            accepted.append(fragment)
            sources.append(Source(fragment_id=fragment.id, document_id=fragment.document_id,
                                  title=title, locator=fragment.locator,
                                  url=f"/api/sources/{fragment.id}"))
        return accepted, sources, rejected

    async def ask(self, request: AskRequest) -> AskResponse:
        as_of = request.as_of or date.today()
        timings = Timings()
        warnings = []
        mode = "bm25"
        sources = []

        def fallback(message, status="not_found", *, cited_titles: set[str] | None = None):
            cleaned_warnings = _filter_answer_warnings(warnings, cited_titles or set())
            if status == "not_found":
                cleaned_warnings = [
                    w for w in cleaned_warnings
                    if w != CONTEXT_WARNING
                ]
            return AskResponse(status=status, mode=mode, answer_mode="sources_only",
                               sources=sources, timings=timings,
                               warnings=list(dict.fromkeys(cleaned_warnings + [message])))

        started = perf_counter()
        try:
            result = await self.searcher.search(SearchRequest(
                question=request.question, as_of=as_of, limit=self.settings.top_fragments))
            # Revalidate injected adapters too; do not trust constructed models.
            # Python-mode preserves typed locator tuples for strict validation.
            payload = result.model_dump(mode="python") if isinstance(result, SearchResult) else result
            result = SearchResult.model_validate(payload, strict=True)
            mode, warnings = result.mode, list(result.warnings)
            fragments, sources, rejected = self._sources(result, as_of)
        except (ComponentError, OSError, sqlite3.Error, ValueError, TimeoutError, RuntimeError) as exc:
            log.warning("Search stage failed (%s)", type(exc).__name__)
            return fallback("Поиск временно недоступен или снимок индекса повреждён; повторите запрос.",
                            "unavailable")
        finally:
            timings.search_ms = (perf_counter() - started) * 1000
        if rejected:
            warnings.append("Часть выдачи не подтверждена закреплёнными ready-публикациями и исключена.")
        if not fragments:
            return fallback("Подходящих подтверждённых источников не найдено.")
        if self.llm is None or not self.settings.llm_enabled:
            return fallback("LLM отключена; приведены только источники без выводов и проверки полноты.")

        # Fit as many ranked fragments as the profile budget allows. The *client's* tokenizer is
        # authoritative when available; the character estimate is only an offline fallback.
        # Leaving a fragment out is recorded, but it limits the answer only when it is material.
        is_definitional = _is_definitional_question(request.question)
        effective_budget = min(
            self.settings.context_budget,
            max(1, self.settings.context_size - self.settings.max_output_tokens - self.settings.prompt_reserve_tokens),
        )
        titles = {s.fragment_id: s.title for s in sources}
        counter = getattr(self.llm, "count_tokens", None)

        async def payload_tokens(candidate: list[Fragment]) -> int:
            text = payload_json(candidate)
            if counter is not None:
                try:
                    return int(await counter(text))
                except (ComponentError, OSError, TimeoutError, RuntimeError, ValueError):
                    pass
            return estimate_tokens(text)

        selected = []
        for fragment in fragments[:self.settings.top_fragments]:
            candidate = selected + [fragment]
            budget_limit = self.settings.context_budget if len(candidate) == 1 else effective_budget
            if await payload_tokens(candidate) <= budget_limit:
                selected = candidate
        truncated = False
        if not selected and fragments and self.settings.context_budget >= 256:
            max_single_chars = max(350, int(self.settings.context_budget * 1.4))
            compacted = _compact_fragment_for_context(fragments[0], request.question, max_single_chars)
            if await payload_tokens([compacted]) <= self.settings.context_budget:
                selected = [compacted]
                truncated = True
        if not selected:
            return fallback("Материалы не помещаются в бюджет контекста; приведены только источники.")
        hits_by_id = {h.fragment.id: h for h in result.hits}
        retrieved = [hits_by_id[f.id] for f in fragments if f.id in hits_by_id]

        def material_limit() -> bool:
            """A real limit of the answer: relevant material left out, or a source cut short."""
            if truncated or rejected:
                return True
            whole = [f for f in selected]
            omitted = material_omissions(retrieved, whole, request.question, titles)
            if omitted:
                log.info("Context omitted %d material fragment(s): %s", len(omitted),
                         ", ".join(f.id for f in omitted))
            return bool(omitted)

        limited = material_limit()

        client = self.llm
        protocol_failed = False
        compaction_steps = 0

        async def timed_call(stage, operation):
            start = perf_counter()
            try:
                return await operation
            finally:
                setattr(timings, stage, getattr(timings, stage) + (perf_counter() - start) * 1000)

        try:
            async with client.sequence() if hasattr(client, "sequence") else nullcontext():
                while selected:
                    generation = GenerationRequest(question=request.question, as_of=as_of, fragments=selected)
                    draft = None
                    issues = []
                    context_retried = False
                    # Exactly two attempts per context slice: initial generation and one correction.
                    for attempt in range(2):
                        try:
                            if attempt and draft is not None:
                                if not hasattr(client, "repair"):
                                    break
                                raw = await timed_call("generation_ms", client.repair(
                                    VerificationRequest(**generation.model_dump(), draft=draft), issues))
                            else:
                                raw = await timed_call("generation_ms", client.generate(
                                    generation.model_copy(deep=True)))
                            # Preserve a structurally valid but quote-invalid draft for repair.
                            draft = (Draft.model_validate_json(raw) if isinstance(raw, str) else
                                     Draft.model_validate(raw.model_dump(mode="json")
                                                          if isinstance(raw, Draft) else raw))
                            draft = validate_draft(draft, selected)
                            protocol_failed = False
                            checked = await timed_call("verification_ms", validate_answer(
                                draft, selected, question=request.question, client=client, as_of=as_of))
                            assessment = checked.model_assessment
                            if not assessment.supported:
                                issues = ["Смысловая проверка не подтвердила все утверждения."] + assessment.issues
                                continue
                            draft = checked.checked_draft
                            claims = draft.summary + draft.steps
                            effective_limited = limited
                            run_warnings = list(warnings)
                            if effective_limited and not rejected and CONTEXT_WARNING not in run_warnings:
                                run_warnings.append(CONTEXT_WARNING)
                            # Model warnings, issues and clarification are unverified prose.
                            # Only controlled server messages leave this boundary.
                            verification = Verification(supported=True,
                                complete=assessment.complete and not effective_limited,
                                conflict=assessment.conflict or draft.status == "conflict")
                            if not claims:
                                status = "ambiguous" if draft.status == "ambiguous" else "not_found"
                                filtered_warnings = _filter_answer_warnings(run_warnings, set())
                                if status == "not_found":
                                    filtered_warnings = [
                                        w for w in filtered_warnings
                                        if w != CONTEXT_WARNING
                                    ]
                                    filtered_warnings.append(
                                        "В предоставленных документах недостаточно сведений для ответа на вопрос."
                                    )
                                else:
                                    filtered_warnings.append("Модель не подтвердила ответ по источникам.")
                                return AskResponse(status=status, mode=mode, answer_mode="sources_only",
                                    sources=sources, timings=timings,
                                    warnings=list(dict.fromkeys(filtered_warnings)),
                                    clarification="Уточните вопрос и необходимые условия." if status == "ambiguous" else None)
                            if is_definitional and not verification.conflict and not effective_limited and draft.status == "answered" and claims and not verification.complete:
                                if _definition_complete_override(getattr(assessment, "issues", None)):
                                    verification = verification.model_copy(update={"complete": True})
                            cited_ids = {ev.fragment_id for c in claims for ev in c.evidence}
                            cited_titles = {s.title for s in sources if s.fragment_id in cited_ids and s.title}
                            filtered_warnings = _filter_answer_warnings(run_warnings, cited_titles)
                            if verification.conflict:
                                status = "conflict"
                                filtered_warnings.append("В источниках обнаружено противоречие; окончательный вывод не подтверждён.")
                            elif verification.complete and draft.status == "answered":
                                status = "answered"
                            else:
                                status = "partial"
                                # "Procedure completeness" is a statement about procedures: use it only for
                                # procedural questions/answers, never for facts, definitions or conditions.
                                if is_procedural_question(request.question) or draft.steps:
                                    filtered_warnings.append("Подтверждена только часть ответа; полнота процедуры не установлена.")
                                else:
                                    filtered_warnings.append("Подтверждена только часть ответа.")
                            return AskResponse(status=status, summary=draft.summary, steps=draft.steps,
                                mode=mode, answer_mode="generated", sources=sources,
                                verification=verification, warnings=list(dict.fromkeys(filtered_warnings)), timings=timings)
                        except ComponentError as exc:
                            if exc.detail.code == "context_exceeded":
                                log.warning(
                                    "Context exceeded with %d fragments (top_chars=%d); trying reduction",
                                    len(selected),
                                    len(selected[0].text),
                                )
                                if len(selected) > 1:
                                    selected = selected[:-1]
                                    limited = material_limit()
                                    context_retried = True
                                    break
                                if len(selected) == 1 and len(selected[0].text) > 500 and compaction_steps < 2:
                                    compaction_steps += 1
                                    next_chars = max(350, len(selected[0].text) // 2)
                                    compacted = _compact_fragment_for_context(selected[0], request.question, next_chars)
                                    if len(compacted.text) < len(selected[0].text):
                                        selected = [compacted]
                                        truncated = True
                                        limited = True
                                        context_retried = True
                                        break
                                raise
                            if exc.detail.code != "invalid_model_output":
                                raise
                            protocol_failed = True
                            # The adapter can reject evidence after parsing a valid
                            # Draft. Keep a freshly shape-validated copy for repair,
                            # never as an approved answer or trusted evidence.
                            rejected_draft = getattr(exc, "draft", None)
                            if isinstance(rejected_draft, Draft):
                                try:
                                    draft = Draft.model_validate(
                                        rejected_draft.model_dump(mode="json"), strict=True)
                                except ValidationError:
                                    pass
                            issues = ["Исправьте структуру ответа, ID и дословные цитаты; используйте только переданные материалы."]
                        except ValidationError:
                            protocol_failed = True
                            issues = ["Верните корректный JSON по схеме Draft с дословными цитатами."]
                    if not context_retried:
                        break
        except ComponentError as exc:
            log.warning("LLM stage ComponentError (%s)", exc.detail.code)
            if exc.detail.code == "context_exceeded":
                return fallback("Контекст модели превышен; приведены только источники без проверки полноты.",
                                "unavailable")
            return fallback("Локальная LLM временно недоступна; повторите запрос. Приведены только источники.",
                            "unavailable")
        except (OSError, TimeoutError, RuntimeError) as exc:
            log.warning("LLM stage failure (%s)", type(exc).__name__)
            return fallback("Локальная LLM временно недоступна; повторите запрос. Приведены только источники.",
                            "unavailable")
        return fallback("Ответ не прошёл проверку после одной попытки исправления; приведены только источники.",
                        "unavailable" if protocol_failed else "not_found")
