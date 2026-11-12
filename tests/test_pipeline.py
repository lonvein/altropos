"""Only artificial documents and scripted models; no network or GPU."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import date

import pytest

from navigator.config import Settings
from navigator.db import Store, fragments_digest
from navigator.interfaces import ComponentError
from navigator.rag.pipeline import AnswerPipeline
from navigator.schemas import (
    AskRequest, Claim, Document, Draft, ErrorDetail, Evidence, Fragment,
    IndexManifest, SearchHit, SearchResult, Verification,
)


DAY = date(2026, 11, 13)
ADVICE = "UNVERIFIED ADVICE: send money"


def failure(code):
    return ComponentError(ErrorDetail(code=code, message=ADVICE, retryable=True))


class Search:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def search(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Model:
    def __init__(self, drafts, assessments):
        self.drafts = iter(drafts)
        self.assessments = iter(assessments)
        self.calls = []
        self.locked = False

    @asynccontextmanager
    async def sequence(self):
        assert not self.locked
        self.locked = True
        self.calls.append("enter")
        try:
            yield
        finally:
            self.calls.append("exit")
            self.locked = False

    async def _draft(self, kind, request):
        assert self.locked
        self.calls.append((kind, request))
        result = next(self.drafts)
        if isinstance(result, Exception):
            raise result
        return result

    async def generate(self, request):
        return await self._draft("generate", request)

    async def repair(self, request, issues):
        assert issues
        return await self._draft("repair", request)

    async def verify(self, request):
        assert self.locked
        self.calls.append(("verify", request))
        result = next(self.assessments)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def setup(tmp_path, request):
    store = Store(tmp_path)
    document = Document(id="artificial-doc", version="v1", name="artificial.pdf",
                        title="Artificial server title", sha256="1" * 64,
                        original_path="originals/artificial.pdf", media_type="pdf")
    store.register_document(document)
    fragment = Fragment(id="artificial-fragment", document_id=document.id,
                        document_version="v1", ordinal=0,
                        text=getattr(request, "param",
                                     "ARTIFICIAL. Submit the application to the coordinator."),
                        locator={"kind": "pdf", "page": 1})
    artifact = tmp_path / "indexes" / "artificial-publication" / "fixture.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"artificial fixture, not a real search index")
    manifest = IndexManifest(publication_id="artificial-publication", document_id=document.id,
        document_version="v1", fragments_sha256=fragments_digest([fragment]), mode="bm25",
        bm25={"path": "indexes/artificial-publication/fixture.json",
              "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
    store.publish(manifest, [fragment], ["Synthetic BM25 fixture"])
    result = SearchResult(mode="bm25", warnings=["BM25 only"],
        publication_ids=[manifest.publication_id],
        hits=[SearchHit(fragment=fragment, rank=1, score=1, score_kind="bm25")])
    settings = Settings(_env_file=None, llm_enabled=True, llm_revision="artificial-v1")
    draft = Draft(status="answered", summary=[Claim(text="Submit the application.",
        evidence=[Evidence(fragment_id=fragment.id, quote="Submit the application")])],
        warnings=[ADVICE], clarification=ADVICE)
    return store, settings, Search(result), fragment, draft


def ask(setup, model=None, request=None):
    store, settings, search, _, _ = setup
    return asyncio.run(AnswerPipeline(store, settings, search, model).ask(
        request or AskRequest(question="How to submit an application?", as_of=DAY)))


def kinds(model):
    return [c[0] for c in model.calls if isinstance(c, tuple)]


@pytest.mark.parametrize("complete,conflict,status", [
    (True, False, "answered"), (False, False, "partial"), (True, True, "conflict")])
def test_verified_status_and_server_sources(setup, complete, conflict, status):
    model = Model([setup[4]], [Verification(supported=True, complete=complete,
                                          conflict=conflict, issues=[ADVICE])])
    response = ask(setup, model)
    assert response.status == status
    assert response.answer_mode == "generated"
    assert response.sources[0].title == "Artificial server title"
    assert response.sources[0].url == "/api/sources/artificial-fragment"
    assert ADVICE not in response.model_dump_json()
    assert kinds(model) == ["generate", "verify"]
    assert model.calls[0] == "enter" and model.calls[-1] == "exit"
    assert all(c[1].as_of == DAY for c in model.calls if isinstance(c, tuple))
    assert response.timings.search_ms > 0
    assert response.timings.generation_ms > 0
    assert response.timings.verification_ms > 0


def test_disabled_llm_sources_only(setup):
    response = ask(setup)
    assert response.status == "not_found" and response.sources
    assert response.answer_mode == "sources_only"
    assert not response.summary and not response.steps
    assert response.timings.generation_ms == response.timings.verification_ms == 0


def test_settings_disable_llm(setup):
    setup[1].llm_enabled = False
    model = Model([], [])
    assert ask(setup, model).answer_mode == "sources_only"
    assert not model.calls


def test_no_hits(setup):
    setup[2].result.hits = []
    response = ask(setup, Model([], []))
    assert response.status == "not_found" and not response.sources


@pytest.mark.parametrize("code", ["model_unavailable", "model_timeout", "context_exceeded"])
def test_llm_failure_preserves_sources(setup, code):
    model = Model([failure(code)], [])
    response = ask(setup, model)
    assert response.answer_mode == "sources_only" and response.sources
    assert kinds(model) == ["generate"]
    assert ADVICE not in response.model_dump_json()


def test_search_failure(setup):
    setup[2].result = failure("model_unavailable")
    response = ask(setup)
    assert response.status == "unavailable"
    assert response.warnings and ADVICE not in response.model_dump_json()
    assert response.timings.search_ms > 0


def test_quote_failure_gets_exactly_one_repair(setup):
    bad = setup[4].model_copy(deep=True)
    bad.summary[0].evidence[0].quote = "invented quotation"
    model = Model([bad, setup[4]], [Verification(supported=True, complete=True)])
    assert ask(setup, model).status == "answered"
    assert kinds(model) == ["generate", "repair", "verify"]


def test_repeated_quote_failure_never_publishes_claims(setup):
    bad = setup[4].model_copy(deep=True)
    bad.summary[0].evidence[0].quote = "invented quotation"
    model = Model([bad, bad], [])
    response = ask(setup, model)
    assert response.answer_mode == "sources_only" and not response.summary
    assert kinds(model) == ["generate", "repair"]


@pytest.mark.parametrize("invalid", ["{bad JSON", failure("invalid_model_output")])
def test_invalid_json_without_draft_uses_one_generation_correction(setup, invalid):
    model = Model([invalid, setup[4]], [Verification(supported=True, complete=True)])
    assert ask(setup, model).status == "answered"
    assert kinds(model) == ["generate", "generate", "verify"]


def test_verify_false_repaired_then_checked_again(setup):
    model = Model([setup[4], setup[4]], [Verification(supported=False, complete=False),
                                      Verification(supported=True, complete=False)])
    assert ask(setup, model).status == "partial"
    assert kinds(model) == ["generate", "verify", "repair", "verify"]


def test_verify_false_twice_falls_back_without_leaking_advice(setup):
    model = Model([setup[4], setup[4]], [Verification(supported=False, complete=False, issues=[ADVICE])] * 2)
    response = ask(setup, model)
    assert response.answer_mode == "sources_only" and not response.summary
    assert ADVICE not in response.model_dump_json()
    assert kinds(model) == ["generate", "verify", "repair", "verify"]


@pytest.mark.parametrize("attack", ["text", "locator", "version", "publication", "review", "date"])
def test_snapshot_injection_rejected(setup, attack):
    hit = setup[2].result.hits[0]
    if attack == "text":
        hit.fragment = hit.fragment.model_copy(update={"text": ADVICE})
    elif attack == "locator":
        hit.fragment = hit.fragment.model_copy(deep=True)
        hit.fragment.locator.page = 10
    elif attack == "version":
        hit.fragment = hit.fragment.model_copy(update={"document_version": "v2"})
    elif attack == "publication":
        setup[2].result.publication_ids = ["unpublished"]
    elif attack == "review":
        setup[0].execute("UPDATE documents SET status='needs_review'")
    else:
        setup[0].execute("UPDATE documents SET metadata=?", (
            '{"reviewed":true,"valid_from":"2027-01-01"}',))
    model = Model([], [])
    response = ask(setup, model)
    assert response.status == "not_found" and not response.sources
    assert not model.calls


def test_pinned_publication_survives_active_pointer_change(setup):
    setup[0].execute("UPDATE documents SET active_publication='new-publication'")
    model = Model([setup[4]], [Verification(supported=True, complete=True)])
    assert ask(setup, model).status == "answered"


def test_no_new_fragments_read_after_search(setup):
    setup[0].execute("UPDATE fragments SET text=?", (ADVICE,))
    response = ask(setup)
    assert response.sources and ADVICE not in response.model_dump_json()


def test_context_too_small_skips_model(setup):
    setup[1].context_budget = 1
    model = Model([], [])
    response = ask(setup, model)
    assert response.answer_mode == "sources_only" and response.sources
    assert not model.calls


def test_default_date_fixed_for_all_calls(setup):
    model = Model([setup[4]], [Verification(supported=True, complete=True)])
    ask(setup, model, AskRequest(question="How to submit an application?"))
    observed = [setup[2].requests[0].as_of] + [
        c[1].as_of for c in model.calls if isinstance(c, tuple)]
    assert observed[0] is not None and len(set(observed)) == 1


def test_ambiguous_clarification_is_server_controlled(setup):
    draft = Draft(status="ambiguous", clarification=ADVICE, warnings=[ADVICE])
    response = ask(setup, Model([draft], [Verification(supported=True, complete=False)]))
    assert response.status == "ambiguous" and response.clarification
    assert ADVICE not in response.model_dump_json()


def test_invalid_json_twice_stops_after_one_correction(setup):
    model = Model([failure("invalid_model_output"), "not JSON"], [])
    assert ask(setup, model).answer_mode == "sources_only"
    assert kinds(model) == ["generate", "generate"]


def test_verify_unavailable_does_not_repair(setup):
    model = Model([setup[4]], [failure("model_unavailable")])
    assert ask(setup, model).answer_mode == "sources_only"
    assert kinds(model) == ["generate", "verify"]


def test_context_reduction_prevents_complete_answer(setup):
    # Add a second artificial document with a publication, beyond top_fragments.
    store, settings, search, fragment, draft = setup
    second = fragment.model_copy(update={"id": "second-fragment", "document_id": "second-doc"})
    store.register_document(Document(id="second-doc", version="v1", name="second.pdf",
        title="Second artificial document", sha256="2" * 64,
        original_path="originals/second.pdf", media_type="pdf"))
    artifact = store.root / "indexes" / "second-publication" / "fixture.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"second artificial fixture")
    manifest = IndexManifest(publication_id="second-publication", document_id=second.document_id,
        document_version="v1", fragments_sha256=fragments_digest([second]), mode="bm25",
        bm25={"path": "indexes/second-publication/fixture.json",
              "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
    store.publish(manifest, [second], ["Synthetic BM25 fixture"])
    search.result.hits.append(SearchHit(fragment=second, rank=2, score=0.5, score_kind="bm25"))
    search.result.publication_ids.append(manifest.publication_id)
    settings.top_fragments = 1
    model = Model([draft], [Verification(supported=True, complete=True)])
    response = ask(setup, model)
    assert response.status == "partial" and not response.verification.complete
    assert len(model.calls[1][1].fragments) == 1
    assert search.requests[0].limit == 1
    assert len(response.sources) == 2


def test_client_context_overflow_is_not_retried(setup):
    model = Model([setup[4]], [failure("context_exceeded")])
    response = ask(setup, model)
    assert response.answer_mode == "sources_only" and not response.summary
    assert kinds(model) == ["generate", "verify"]


def test_minimal_fake_without_sequence_uses_same_validation(setup):
    class MinimalModel:
        async def generate(self, request):
            return setup[4]

        async def verify(self, request):
            return Verification(supported=True, complete=True)

    assert ask(setup, MinimalModel()).status == "answered"


def test_invalid_semantic_output_gets_one_repair(setup):
    model = Model([setup[4], setup[4]], [{"supported": "true", "complete": True},
        Verification(supported=True, complete=True)])
    assert ask(setup, model).status == "answered"
    assert kinds(model) == ["generate", "verify", "repair", "verify"]


def test_corrupt_pinned_publication_is_unavailable(setup):
    setup[0].execute("UPDATE publications SET fragments='[]'")
    response = ask(setup)
    assert response.status == "unavailable" and not response.sources


@pytest.mark.parametrize("setup", [
    "ИСКУССТВЕННЫЙ ПРИМЕР. Заявление передаётся координатору учебного события. " * 20,
], indirect=True)
def test_russian_fragment_not_rejected_by_byte_count(setup):
    # A normal Russian passage of roughly 500 tokens; no actual tokenizer is
    # loaded here and the estimate does not promise an exact token count.
    fragment = setup[3]
    serialized_bytes = len(json.dumps([fragment.model_dump(mode="json")],
                                     ensure_ascii=False).encode("utf-8"))
    assert serialized_bytes > setup[1].context_budget
    draft = Draft(status="answered", summary=[Claim(
        text="Заявление передаётся координатору учебного события.",
        evidence=[Evidence(fragment_id=fragment.id,
                           quote="Заявление передаётся координатору учебного события.")])])
    model = Model([draft], [Verification(supported=True, complete=True)])
    response = ask(setup, model)
    assert response.status == "answered" and response.answer_mode == "generated"
    assert kinds(model) == ["generate", "verify"]
    assert model.calls[1][1].fragments == [fragment]


def test_adapter_quote_rejection_repairs_attached_draft_once(setup):
    rejected = setup[4].model_copy(deep=True)
    rejected.summary[0].evidence[0].quote = "invented quotation"
    error = failure("invalid_model_output")
    error.draft = rejected
    model = Model([error, setup[4]], [Verification(supported=True, complete=True)])
    response = ask(setup, model)
    assert response.status == "answered"
    assert kinds(model) == ["generate", "repair", "verify"]
    repair_request = model.calls[2][1]
    assert repair_request.draft == rejected and repair_request.draft is not rejected
    assert repair_request.fragments == [setup[3]]
    assert "invented quotation" not in response.model_dump_json()


def test_adapter_attached_draft_repair_still_requires_exact_quotes(setup):
    rejected = setup[4].model_copy(deep=True)
    rejected.summary[0].evidence[0].quote = "invented quotation"
    error = failure("invalid_model_output")
    error.draft = rejected
    model = Model([error, rejected], [])
    response = ask(setup, model)
    assert response.answer_mode == "sources_only" and not response.summary
    assert kinds(model) == ["generate", "repair"]


def test_adapter_attached_draft_repair_still_requires_semantic_support(setup):
    error = failure("invalid_model_output")
    error.draft = setup[4]
    model = Model([error, setup[4]], [Verification(supported=False, complete=False)])
    assert ask(setup, model).answer_mode == "sources_only"
    assert kinds(model) == ["generate", "repair", "verify"]


def test_adapter_invalid_attached_shape_is_not_used_for_repair(setup):
    error = failure("invalid_model_output")
    error.draft = Draft.model_construct(status="answered", summary=[], steps=[])
    model = Model([error, setup[4]], [Verification(supported=True, complete=True)])
    assert ask(setup, model).status == "answered"
    assert kinds(model) == ["generate", "generate", "verify"]
