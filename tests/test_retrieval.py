"""Only artificial documents and model outputs; CPU FAISS, no weights or GPU."""
import asyncio
import hashlib
import json

import pytest

from navigator.config import Settings
from navigator.db import Store
from navigator.interfaces import ComponentError
from navigator.rag import indexing
from navigator.rag.indexing import Indexer, component_error, tokens
from navigator.rag.retrieval import Searcher, rrf
from navigator.schemas import (Document, EmbeddingResponse, Fragment, RerankResponse,
                               RerankScore, SearchRequest, Subqueries)


def settings(ml=False, **kwargs):
    return Settings(_env_file=None, ml_enabled=ml, reranker_revision="synthetic-v1", **kwargs)


class Embeddings:
    def __init__(self, config, failure=None):
        self.config, self.failure, self.calls = config, failure, []

    async def embed(self, request):
        self.calls.append(request)
        if self.failure:
            raise component_error(self.failure, "Искусственный отказ ML.")
        vectors = []
        for text in request.texts:
            # Deliberately semantic-only: 'синоним' and 'заявка' share a direction.
            vector = [0.0] * 1024
            vector[0 if any(t in text.lower() for t in ("заявка", "синоним")) else 1] = 0.999
            vectors.append(vector)
        return EmbeddingResponse(fingerprint=self.config.fingerprint, vectors=vectors)


class Reranker:
    def __init__(self, config, failure=False, invalid=False):
        self.config, self.failure, self.invalid = config, failure, invalid
        self.calls = []

    async def rerank(self, request):
        self.calls.append(request)
        if self.failure:
            raise component_error("model_unavailable", "Искусственный отказ reranker.")
        return RerankResponse(model=self.config.reranker_model, revision=self.config.reranker_revision,
            scores=[RerankScore(fragment_id="unknown" if self.invalid else f.id, score=float(i))
                    for i, f in enumerate(request.fragments)])


def document(store, doc_id="demo", texts=None, metadata=None, status="queued"):
    doc = Document(id=doc_id, version="v1", name="synthetic", title="Искусственный документ",
        sha256=hashlib.sha256(doc_id.encode()).hexdigest(), original_path=f"originals/{doc_id}.txt",
        media_type="html", status=status, metadata=metadata or {})
    store.register_document(doc)
    fragments = [Fragment(id=f"{doc_id}-f{i}", document_id=doc_id, document_version="v1", ordinal=i,
                         text=text, section=str(i + 1), locator={"kind": "html", "block": i})
                 for i, text in enumerate(texts or ["ИСКУССТВЕННЫЙ ПРИМЕР. заявка координатору 12.11.2026 №42"]) ]
    return doc, fragments


def search(store, config=None, question="заявка", embeddings=None, reranker=None, **kwargs):
    return asyncio.run(Searcher(store, config or settings(), embeddings, reranker).search(
        SearchRequest(question=question, as_of="2026-11-12", **kwargs)))


def test_bm25_restart_dedup_empty(tmp_path):
    store = Store(tmp_path)
    doc, fragments = document(store)
    indexer = Indexer(store, settings())
    result = indexer.index_document(doc.id, fragments)
    assert indexer.index_document(doc.id, fragments) == result
    assert len(store.rows("SELECT * FROM publications")) == 1
    reopened = Store(tmp_path)
    response = search(reopened, question="№42")
    assert response.mode == "bm25" and response.warnings
    assert response.hits[0].fragment == fragments[0]
    assert response.hits[0].score > 0 and response.hits[0].score_kind == "bm25"
    assert response.publication_ids == [result.publication_id]
    assert search(reopened, question="несуществующее").hits == []
    assert tokens("МФТИ Ёж 12.11.2026 №42") == ["мфти", "еж", "12", "11", "2026", "42"]


def test_validity_notices_only_name_selected_documents(tmp_path):
    store = Store(tmp_path)
    for doc_id, text, title in [("relevant", "поселения сотрудника", "Искусственный регламент поселения"),
                                ("other", "техническое оборудование", "Искусственный регламент оборудования")]:
        doc, fragments = document(store, doc_id, texts=[text], metadata={"title": title})
        Indexer(store, settings()).index_document(doc.id, fragments)
    result = search(store, question="поселения")
    assert result.hits[0].fragment.document_id == "relevant"
    notices = [w for w in result.warnings if "Период действия" in w]
    assert len(notices) == 1
    assert "Искусственный регламент поселения" in notices[0]
    assert not any("оборудования" in w for w in result.warnings)


def test_hybrid_restart_normalization_rerank(tmp_path):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store, texts=["ИСКУССТВЕННЫЙ ПРИМЕР. заявка", "ИСКУССТВЕННЫЙ ПРИМЕР. справка"])
    emb, rerank = Embeddings(config), Reranker(config)
    published = Indexer(store, config, emb).index_document(doc.id, fragments)
    assert published.mode == "hybrid"
    response = search(Store(tmp_path), config, "синоним", emb, rerank)
    assert response.mode == "hybrid" and len(response.hits) == 2
    assert all(hit.score_kind == "reranker" for hit in response.hits)
    assert response.hits[0].fragment.id == rerank.calls[0].fragments[-1].id
    manifest, shard = store.active_publications(config.fingerprint)[0]
    vectors = indexing.load_vectors(store, manifest, shard).reconstruct_n(0, 2)
    assert sum(vectors[0] ** 2) == pytest.approx(1)
    assert Indexer(store, config, emb).index_document(doc.id, fragments) == published


@pytest.mark.parametrize("failure", ["model_unavailable", "model_timeout"])
def test_index_ml_unavailable_upgrade(tmp_path, failure):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    result = Indexer(store, config, Embeddings(config, failure)).index_document(doc.id, fragments)
    assert result.mode == "bm25" and result.warnings
    assert Indexer(store, config, Embeddings(config, failure)).index_document(doc.id, fragments) == result
    assert len(store.rows("SELECT * FROM publications")) == 1
    manifest, _ = store.active_publications()[0]
    assert manifest.vectors is None and manifest.embedding_fingerprint is None
    assert Indexer(store, config, Embeddings(config)).index_document(doc.id, fragments).mode == "hybrid"


def test_incompatible_revision_and_explicit_bm25(tmp_path):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    Indexer(store, config, Embeddings(config)).index_document(doc.id, fragments)
    other = settings(True, embedding_revision="synthetic-other")
    emb = Embeddings(other)
    response = search(store, other, embeddings=emb, reranker=Reranker(other))
    assert response.mode == "bm25" and response.hits
    assert not emb.calls
    assert any("несовместима" in warning for warning in response.warnings)
    assert search(store).mode == "bm25"


@pytest.mark.parametrize("failure", ["embedding", "reranker", "invalid_rerank", "wrong_batch", "wrong_revision"])
def test_search_ml_failure_discards_dense_results(tmp_path, failure):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    Indexer(store, config, Embeddings(config)).index_document(doc.id, fragments)
    emb = Embeddings(config, "model_unavailable" if failure == "embedding" else None)
    if failure == "wrong_revision":
        emb.config = settings(True, embedding_revision="wrong")
    if failure == "wrong_batch":
        async def broken(request):
            return EmbeddingResponse(fingerprint=config.fingerprint, vectors=[[1.] + [0.] * 1023] * 2)
        emb.embed = broken
    rerank = Reranker(config, failure == "reranker", failure == "invalid_rerank")
    response = search(store, config, "синоним", emb, rerank)
    assert response.mode == "bm25" and response.hits == [] and response.warnings


@pytest.mark.parametrize("failure", ["write", "rename", "transaction", "bad_output"])
def test_failed_index_is_invisible(tmp_path, monkeypatch, failure):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    if failure == "write":
        def fail(*args):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(indexing, "_write", fail)
    if failure == "rename":
        def fail(*args):
            raise OSError("synthetic rename failure")
        monkeypatch.setattr(indexing.Path, "rename", fail)
    if failure == "transaction":
        store.execute("""CREATE TRIGGER fail_ready BEFORE UPDATE ON documents
                         BEGIN SELECT RAISE(ABORT, 'synthetic'); END""")
    emb = Embeddings(config, "invalid_model_output" if failure == "bad_output" else None)
    with pytest.raises(ComponentError):
        Indexer(store, config, emb).index_document(doc.id, fragments)
    assert store.active_publications() == []
    assert store.rows("SELECT * FROM fragments") == []
    assert search(store).hits == []


def test_failed_upgrade_preserves_publication(tmp_path):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    old = Indexer(store, settings()).index_document(doc.id, fragments)
    store.execute("""CREATE TRIGGER fail_ready BEFORE UPDATE ON documents
                     BEGIN SELECT RAISE(ABORT, 'synthetic'); END""")
    with pytest.raises(ComponentError):
        Indexer(store, config, Embeddings(config)).index_document(doc.id, fragments)
    assert search(store).publication_ids == [old.publication_id]
    assert len(store.rows("SELECT * FROM publications")) == 1


@pytest.mark.parametrize("bad", ["duplicate", "ordinal", "version", "document", "locator", "empty", "review"])
def test_reject_invalid_fragments(tmp_path, bad):
    store = Store(tmp_path)
    doc, fragments = document(store, status="needs_review" if bad == "review" else "queued")
    if bad == "duplicate":
        fragments *= 2
    elif bad == "empty":
        fragments = []
    elif bad in ("ordinal", "version", "document", "locator"):
        key, value = {"ordinal": ("ordinal", 1), "version": ("document_version", "v2"),
                      "document": ("document_id", "other"), "locator": ("locator", {"kind": "pdf", "page": 1})}[bad]
        fragments = [Fragment.model_validate({**fragments[0].model_dump(), key: value})]
    with pytest.raises(ComponentError):
        Indexer(store, settings()).index_document(doc.id, fragments)
    assert store.active_publications() == []


@pytest.mark.parametrize("artifact", ["bm25", "vectors"])
def test_corrupt_index_rejected(tmp_path, artifact):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    Indexer(store, config, Embeddings(config)).index_document(doc.id, fragments)
    manifest, _ = store.active_publications(config.fingerprint)[0]
    (tmp_path / getattr(manifest, artifact).path).write_bytes(b"synthetic corruption")
    with pytest.raises(ComponentError) as error:
        search(store, config, embeddings=Embeddings(config), reranker=Reranker(config))
    assert error.value.detail.code == "index_mismatch"
    if artifact == "vectors":
        assert search(store).hits  # Explicit BM25 never opens incompatible/unneeded vectors.


def test_revision_dates_and_confirmed_repeal(tmp_path):
    store = Store(tmp_path)
    for doc_id, metadata in [("old", {"issued": "2020-01-01"}),
                             ("new", {"issued": "2026-01-01"})]:
        doc, fragments = document(store, doc_id, metadata=metadata)
        Indexer(store, settings()).index_document(doc.id, fragments)
    assert len(search(store).hits) == 2  # A newer issued date does not repeal old text.
    metadata = {"reviewed": True, "valid_from": "2026-10-01", "relations": [
        {"kind": "repeals", "target_id": "old", "confirmed": True}]}
    store.execute("UPDATE documents SET metadata=? WHERE id='new'", (json.dumps(metadata),))
    assert [h.fragment.document_id for h in search(store).hits] == ["new"]
    before = asyncio.run(Searcher(store, settings()).search(SearchRequest(question="заявка", as_of="2026-09-01")))
    assert [h.fragment.document_id for h in before.hits] == ["old"]
    metadata["relations"][0]["confirmed"] = False
    store.execute("UPDATE documents SET metadata=? WHERE id='new'", (json.dumps(metadata),))
    assert len(search(store).hits) == 2


@pytest.mark.parametrize("section", [None, "1"])
@pytest.mark.parametrize("as_of,active", [("2026-09-30", False), ("2026-10-01", True),
                                         ("2026-11-12", True)])
def test_expired_repealer_never_revives_target(tmp_path, section, as_of, active):
    store = Store(tmp_path)
    old, fragments = document(store, "old", texts=["ИСКУССТВЕННЫЙ ПРИМЕР. заявка один",
                                                   "ИСКУССТВЕННЫЙ ПРИМЕР. заявка два"])
    Indexer(store, settings()).index_document(old.id, fragments)
    repealer, repealer_fragments = document(store, "repealer", metadata={
        "reviewed": True, "valid_from": "2026-10-01", "valid_to": "2026-10-31",
        "relations": [{"kind": "repeals", "target_id": "old", "target_section": section,
                       "confirmed": True}]})
    Indexer(store, settings()).index_document(repealer.id, repealer_fragments)
    result = asyncio.run(Searcher(store, settings()).search(SearchRequest(question="заявка", as_of=as_of)))
    ids = {hit.fragment.id for hit in result.hits}
    assert (fragments[0].id in ids) is (not active)
    assert (fragments[1].id in ids) is (not active or section is not None)
    assert (repealer_fragments[0].id in ids) is (as_of == "2026-10-01")


def test_repeal_with_unknown_effective_date_only_warns(tmp_path):
    store = Store(tmp_path)
    for doc_id, metadata in [("old", {}), ("repealer", {
        "reviewed": True, "issued": "2026-01-01", "valid_to": "2026-10-31",
        "relations": [{"kind": "repeals", "target_id": "old", "confirmed": True}]})]:
        doc, fragments = document(store, doc_id, metadata=metadata)
        Indexer(store, settings()).index_document(doc.id, fragments)
    result = search(store)
    assert [hit.fragment.document_id for hit in result.hits] == ["old"]
    assert "Дата вступления отмены в силу не подтверждена." in result.warnings


def test_rrf_duplicates_and_limits(tmp_path):
    assert rrf([["a", "b", "a"], ["b"]]) == {"a": 1 / 61, "b": 1 / 62 + 1 / 61}
    store, config = Store(tmp_path), settings(True, top_fragments=4)
    doc, fragments = document(store, texts=[f"ИСКУССТВЕННЫЙ ПРИМЕР. заявка {i}" for i in range(75)])
    emb, rerank = Embeddings(config), Reranker(config)
    Indexer(store, config, emb).index_document(doc.id, fragments)
    assert [len(call.texts) for call in emb.calls] == [64, 11]
    result = asyncio.run(Searcher(store, config, emb, rerank).search(
        SearchRequest(question="заявка", limit=8), Subqueries(queries=["заявка 74", "заявка 73", "заявка 72"])))
    assert len(result.hits) == 4
    assert len(rerank.calls[0].fragments) <= 40
    assert len({f.id for f in rerank.calls[0].fragments}) == len(rerank.calls[0].fragments)


def test_global_bm25_statistics(tmp_path):
    from navigator.rag.retrieval import _bm25
    store = Store(tmp_path)
    all_fragments = []
    for doc_id, texts in [("one", ["ИСКУССТВЕННЫЙ ПРИМЕР. заявка заявка"]),
                           ("two", ["ИСКУССТВЕННЫЙ ПРИМЕР. заявка", "ИСКУССТВЕННЫЙ ПРИМЕР. справка"])]:
        doc, fragments = document(store, doc_id, texts)
        all_fragments.extend(fragments)
        Indexer(store, settings()).index_document(doc.id, fragments)
    scores, _ = _bm25(all_fragments, ["заявка"])
    result = search(store)
    assert {h.fragment.id: h.score for h in result.hits} == pytest.approx(scores)


def test_query_pins_snapshot_during_publication(tmp_path):
    store, config = Store(tmp_path), settings(True)
    doc, fragments = document(store)
    emb = Embeddings(config)
    original = Indexer(store, config, emb).index_document(doc.id, fragments)
    # Publishing a new compatible shard during the query must not change its IDs.
    class PublishingEmbeddings(Embeddings):
        async def embed(self, request):
            if request.purpose == "query":
                # This publication is BM25 and requires no nested async model call.
                Indexer(store, settings()).index_document(doc.id, fragments)
            return await super().embed(request)
    response = search(store, config, embeddings=PublishingEmbeddings(config), reranker=Reranker(config))
    assert response.mode == "hybrid"
    assert response.publication_ids == [original.publication_id]
    assert search(store).publication_ids != response.publication_ids


def test_section_repeal_and_unconfirmed_dates(tmp_path):
    store = Store(tmp_path)
    old, fragments = document(store, "old", texts=["ИСКУССТВЕННЫЙ ПРИМЕР. заявка один",
                                                   "ИСКУССТВЕННЫЙ ПРИМЕР. заявка два"])
    Indexer(store, settings()).index_document(old.id, fragments)
    metadata = {"reviewed": True, "valid_from": "2026-01-01", "relations": [
        {"kind": "repeals", "target_id": "old", "target_section": "1", "confirmed": True}]}
    new, new_fragments = document(store, "new", metadata=metadata)
    Indexer(store, settings()).index_document(new.id, new_fragments)
    ids = {hit.fragment.id for hit in search(store).hits}
    assert fragments[0].id not in ids and fragments[1].id in ids
    metadata["reviewed"] = False
    metadata["valid_from"] = "2027-01-01"
    store.execute("UPDATE documents SET metadata=? WHERE id='new'", (json.dumps(metadata),))
    assert len(search(store).hits) == 3  # Unreviewed dates cannot hide either document.


def test_index_rejects_reusing_ids_with_new_content(tmp_path):
    store = Store(tmp_path)
    doc, fragments = document(store)
    Indexer(store, settings()).index_document(doc.id, fragments)
    changed = [fragments[0].model_copy(update={"text": "ИСКУССТВЕННЫЙ ПРИМЕР. новое содержание"})]
    with pytest.raises(ComponentError):
        Indexer(store, settings()).index_document(doc.id, changed)
    assert search(store).hits[0].fragment.text == fragments[0].text
