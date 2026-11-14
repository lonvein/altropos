"""A shortened context must not, by itself, make an answer 'incomplete'.

Only material omissions (comparably relevant, not already covered) and truncated sources are
real limits. Artificial documents and scripted models only.
"""
import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import date

from navigator.config import Settings
from navigator.db import Store, fragments_digest
from navigator.interfaces import ComponentError
from navigator.rag.context import covered_by, is_procedural_question, material_omissions
from navigator.rag.pipeline import AnswerPipeline
from navigator.schemas import (
    AskRequest, Claim, Document, Draft, ErrorDetail, Evidence, Fragment, IndexManifest,
    SearchHit, SearchResult, Verification,
)

DAY = date(2026, 11, 13)
CONTEXT = "Контекст сокращён; полнота ответа не подтверждается."
PROCEDURE = "Подтверждена только часть ответа; полнота процедуры не установлена."
PART = "Подтверждена только часть ответа."


class Search:
    def __init__(self, result):
        self.result = result

    async def search(self, request):
        return self.result


class Model:
    def __init__(self, drafts, assessments, counter=None):
        self.drafts, self.assessments = iter(drafts), iter(assessments)
        self.generated = []
        if counter is not None:
            self.count_tokens = counter

    @asynccontextmanager
    async def sequence(self):
        yield

    async def generate(self, request):
        self.generated.append(list(request.fragments))
        result = next(self.drafts)
        if isinstance(result, Exception):
            raise result
        return result

    async def verify(self, request):
        return next(self.assessments)


def overflow():
    return ComponentError(ErrorDetail(code="context_exceeded", message="x", retryable=False))


def build(tmp_path, texts, scores, *, kind="bm25", **settings):
    store = Store(tmp_path)
    doc = Document(id="doc", version="v1", name="d.docx", title="Регламент приёма", sha256="3" * 64,
                   original_path="originals/d.docx", media_type="docx")
    store.register_document(doc)
    fragments = [Fragment(id=f"f{i}", document_id="doc", document_version="v1", ordinal=i, text=t,
                          section=str(i), locator={"kind": "docx", "block": i})
                 for i, t in enumerate(texts)]
    artifact = tmp_path / "indexes" / "pub" / "fixture.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture")
    manifest = IndexManifest(publication_id="pub", document_id="doc", document_version="v1",
                             fragments_sha256=fragments_digest(fragments), mode="bm25",
                             bm25={"path": "indexes/pub/fixture.json",
                                   "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()})
    store.publish(manifest, fragments, ["BM25 fixture"])
    result = SearchResult(mode="bm25", warnings=["BM25 only"], publication_ids=["pub"],
                          hits=[SearchHit(fragment=f, rank=i, score=s, score_kind=kind)
                                for i, (f, s) in enumerate(zip(fragments, scores), 1)])
    cfg = Settings(_env_file=None, llm_enabled=True, llm_revision="artificial-v1", **settings)
    return store, cfg, Search(result), fragments


def draft_for(fragment, quote):
    return Draft(status="answered", summary=[Claim(text="Подайте заявление в приёмную комиссию.",
                                                   evidence=[Evidence(fragment_id=fragment.id, quote=quote)])])


def ask(store, cfg, search, model, question):
    return asyncio.run(AnswerPipeline(store, cfg, search, model).ask(AskRequest(question=question, as_of=DAY)))


FACT = "Заявление о поступлении подаётся в приёмную комиссию института."
OTHER = "Стоимость обучения определяется отдельным приказом ректора на каждый учебный год."
DUPLICATE = "Заявление о поступлении подаётся в приёмную комиссию института."


def test_omitted_low_ranked_hit_does_not_limit_the_answer(tmp_path):
    store, cfg, search, frags = build(tmp_path, [FACT, OTHER], [3.0, 0.4], top_fragments=1)
    model = Model([draft_for(frags[0], "подаётся в приёмную комиссию")], [Verification(supported=True, complete=True)])
    response = ask(store, cfg, search, model, "Куда подать заявление о поступлении?")
    assert response.status == "answered" and response.verification.complete
    assert CONTEXT not in response.warnings and not any("часть ответа" in w for w in response.warnings)


def test_omitted_duplicate_of_overlapping_chunk_does_not_limit_the_answer(tmp_path):
    store, cfg, search, frags = build(tmp_path, [FACT, DUPLICATE], [3.0, 2.9], top_fragments=1)
    assert covered_by(frags[1], [frags[0]])
    model = Model([draft_for(frags[0], "приёмную комиссию")], [Verification(supported=True, complete=True)])
    response = ask(store, cfg, search, model, "Куда подать заявление о поступлении?")
    assert response.status == "answered" and CONTEXT not in response.warnings


def test_context_overflow_that_drops_only_a_duplicate_keeps_the_answer_complete(tmp_path):
    store, cfg, search, frags = build(tmp_path, [FACT, DUPLICATE], [3.0, 2.9])
    model = Model([overflow(), draft_for(frags[0], "приёмную комиссию")],
                  [Verification(supported=True, complete=True)])
    response = ask(store, cfg, search, model, "Куда подать заявление о поступлении?")
    assert [len(g) for g in model.generated] == [2, 1]
    assert response.status == "answered" and response.verification.complete
    assert CONTEXT not in response.warnings


def test_material_omission_is_still_reported(tmp_path):
    relevant = "Сроки подачи заявления о поступлении устанавливаются приказом о приёме."
    store, cfg, search, frags = build(tmp_path, [FACT, relevant], [3.0, 2.8], top_fragments=1)
    model = Model([draft_for(frags[0], "приёмную комиссию")], [Verification(supported=True, complete=True)])
    response = ask(store, cfg, search, model, "Куда и когда подать заявление о поступлении?")
    assert response.status == "partial" and not response.verification.complete
    assert CONTEXT in response.warnings
    assert [m.id for m in material_omissions(search.result.hits, [frags[0]], "Куда и когда подать заявление?")] == ["f1"]


def test_truncated_source_remains_a_real_limitation(tmp_path):
    long_text = ("Заявление о поступлении подаётся в приёмную комиссию. " + "Дополнительное условие приёма. " * 80)
    store, cfg, search, frags = build(tmp_path, [long_text], [3.0], context_budget=300,
                                      context_size=4096, top_fragments=1)
    model = Model([draft_for(frags[0], "Заявление о поступлении подаётся в приёмную комиссию.")],
                  [Verification(supported=True, complete=True)])
    response = ask(store, cfg, search, model, "Куда подать заявление о поступлении?")
    assert CONTEXT in response.warnings and not response.verification.complete


def test_exact_token_counter_keeps_fragments_the_estimate_would_have_dropped(tmp_path):
    texts = [FACT + " " + "пояснение " * 150, "Сроки подачи заявления о поступлении указаны в приказе о приёме. " * 4]
    store, cfg, search, frags = build(tmp_path, texts, [3.0, 2.9], context_budget=700, top_fragments=2)

    async def exact(text):
        return 600  # the model's tokenizer says both fragments fit

    model = Model([draft_for(frags[0], "подаётся в приёмную комиссию")],
                  [Verification(supported=True, complete=True)], counter=exact)
    response = ask(store, cfg, search, model, "Куда и когда подать заявление о поступлении?")
    assert len(model.generated[0]) == 2
    assert response.status == "answered" and CONTEXT not in response.warnings


def test_partial_fact_answer_does_not_claim_procedure_incompleteness(tmp_path):
    store, cfg, search, frags = build(tmp_path, [FACT], [3.0])
    model = Model([draft_for(frags[0], "приёмную комиссию")],
                  [Verification(supported=True, complete=False, issues=["Нет часов работы комиссии."])])
    response = ask(store, cfg, search, model, "Кто принимает заявление о поступлении?")
    assert response.status == "partial"
    assert PART in response.warnings and PROCEDURE not in response.warnings


def test_partial_procedure_answer_keeps_procedure_warning(tmp_path):
    store, cfg, search, frags = build(tmp_path, [FACT], [3.0])
    model = Model([draft_for(frags[0], "приёмную комиссию")],
                  [Verification(supported=True, complete=False)])
    response = ask(store, cfg, search, model, "Как подать заявление о поступлении?")
    assert response.status == "partial" and PROCEDURE in response.warnings


def test_unconfirmed_hit_is_reported_by_its_own_warning_not_as_context_cut(tmp_path):
    store, cfg, search, frags = build(tmp_path, [FACT], [3.0])
    ghost = frags[0].model_copy(update={"id": "ghost", "text": "Другой текст, которого нет в публикации."})
    search.result.hits.append(SearchHit(fragment=ghost, rank=2, score=2.0, score_kind="bm25"))
    model = Model([draft_for(frags[0], "приёмную комиссию")], [Verification(supported=True, complete=True)])
    response = ask(store, cfg, search, model, "Куда подать заявление о поступлении?")
    assert any("не подтверждена закреплёнными" in w for w in response.warnings)
    assert CONTEXT not in response.warnings


def test_relevance_helpers():
    assert is_procedural_question("Как оформить внутреннее заимствование?")
    assert is_procedural_question("Каков порядок подачи заявления?")
    assert not is_procedural_question("Кто возглавляет приёмную комиссию?")
    assert not is_procedural_question("Что такое КФО?")
