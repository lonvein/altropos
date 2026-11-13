"""Request orchestration over pinned publications; no model or device loading."""
import json
import sqlite3
from contextlib import nullcontext
from datetime import date
from math import ceil
from time import perf_counter

from pydantic import ValidationError

from navigator.config import Settings
from navigator.db import Store, fragments_digest
from navigator.interfaces import ComponentError, LLMClient, Searcher
from navigator.rag.validation import validate_answer, validate_draft
from navigator.schemas import (
    AskRequest, AskResponse, Document, Draft, Fragment, GenerationRequest, IndexManifest,
    MetadataPatch, SearchRequest, SearchResult, Source, Timings, Verification,
    VerificationRequest,
)


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
    compact = [
        {"id": f.id, "section": f.section, "text": f.text}
        for f in candidate
    ]
    size = len(json.dumps(compact, ensure_ascii=False))
    return ceil(size / 2)


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
        rows = self.store.rows(f"""SELECT p.id,p.manifest,p.fragments,d.payload,
            d.title,d.metadata FROM publications p JOIN documents d ON d.id=p.document_id
            WHERE p.id IN ({placeholders}) AND d.status='ready'""", result.publication_ids)
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

        def fallback(message, status="not_found"):
            return AskResponse(status=status, mode=mode, answer_mode="sources_only",
                               sources=sources, timings=timings,
                               warnings=list(dict.fromkeys(warnings + [message])))

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
        except (ComponentError, OSError, sqlite3.Error, ValueError, TimeoutError):
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

        # ceil(UTF-8 bytes / 3) is only a rough token estimate for admission;
        # it is not a token count or a guarantee that the materials fit.
        # The concrete client checks its real tokenizer for every model call.
        is_definitional = _is_definitional_question(request.question)
        selected = []
        for fragment in fragments[:self.settings.top_fragments]:
            candidate = selected + [fragment]
            if _context_tokens(candidate) <= self.settings.context_budget:
                selected = candidate
        if is_definitional and selected and not rejected:
            limited = False
        else:
            limited = rejected or len(selected) < len(fragments)
        if limited:
            warnings.append("Контекст сокращён; полнота ответа не подтверждается.")
        if not selected:
            return fallback("Материалы не помещаются в бюджет контекста; приведены только источники.")

        generation = GenerationRequest(question=request.question, as_of=as_of, fragments=selected)
        client = self.llm
        protocol_failed = False

        async def timed_call(stage, operation):
            start = perf_counter()
            try:
                return await operation
            finally:
                setattr(timings, stage, getattr(timings, stage) + (perf_counter() - start) * 1000)

        try:
            async with client.sequence() if hasattr(client, "sequence") else nullcontext():
                draft = None
                issues = []
                # Exactly two attempts: initial generation and one correction.
                # If invalid JSON left no Draft, retry generate once unchanged;
                # the client prompt already requires valid JSON and exact quotes.
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
                        # Model warnings, issues and clarification are unverified prose.
                        # Only controlled server messages leave this boundary.
                        verification = Verification(supported=True,
                            complete=assessment.complete and not limited,
                            conflict=assessment.conflict or draft.status == "conflict")
                        if not claims:
                            status = "ambiguous" if draft.status == "ambiguous" else "not_found"
                            return AskResponse(status=status, mode=mode, answer_mode="sources_only",
                                sources=sources, timings=timings,
                                warnings=list(dict.fromkeys(warnings + ["Модель не подтвердила ответ по источникам."])),
                                clarification="Уточните вопрос и необходимые условия." if status == "ambiguous" else None)
                        if is_definitional and not verification.conflict and not limited and draft.status == "answered" and claims and not verification.complete:
                            if _definition_complete_override(getattr(assessment, "issues", None)):
                                verification = verification.model_copy(update={"complete": True})
                        cited_ids = {ev.fragment_id for c in claims for ev in c.evidence}
                        cited_titles = {s.title for s in sources if s.fragment_id in cited_ids and s.title}
                        filtered_warnings = _filter_answer_warnings(warnings, cited_titles)
                        if verification.conflict:
                            status = "conflict"
                            filtered_warnings.append("В источниках обнаружено противоречие; окончательный вывод не подтверждён.")
                        elif verification.complete and draft.status == "answered":
                            status = "answered"
                        else:
                            status = "partial"
                            if not is_definitional or limited:
                                filtered_warnings.append("Подтверждена только часть ответа; полнота процедуры не установлена.")
                            else:
                                filtered_warnings.append("Подтверждена только часть ответа.")
                        return AskResponse(status=status, summary=draft.summary, steps=draft.steps,
                            mode=mode, answer_mode="generated", sources=sources,
                            verification=verification, warnings=list(dict.fromkeys(filtered_warnings)), timings=timings)
                    except ComponentError as exc:
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
        except ComponentError as exc:
            if exc.detail.code == "context_exceeded":
                return fallback("Контекст модели превышен; приведены только источники без проверки полноты.",
                                "unavailable")
            return fallback("Локальная LLM временно недоступна; повторите запрос. Приведены только источники.",
                            "unavailable")
        except (OSError, TimeoutError):
            return fallback("Локальная LLM временно недоступна; повторите запрос. Приведены только источники.",
                            "unavailable")
        return fallback("Ответ не прошёл проверку после одной попытки исправления; приведены только источники.",
                        "unavailable" if protocol_failed else "not_found")
