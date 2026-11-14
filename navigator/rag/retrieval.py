"""Snapshot retrieval: global BM25, normalized FAISS/IP, RRF(60), pinned reranker."""
import asyncio
import json
import logging
import math
import sqlite3
from datetime import date

from pydantic import ValidationError

from navigator.config import Settings
from navigator.db import Store
from navigator.interfaces import ComponentError, EmbeddingsClient, RerankerClient
from navigator.rag.indexing import (BM25_WARNING, component_error, embed, load_vectors,
                                    tokens, validate_lexical, validate_binding)
from navigator.schemas import (Document, Fragment, IndexManifest, MetadataPatch, RerankRequest,
                               RerankResponse, SearchHit, SearchRequest, SearchResult, Subqueries)

log = logging.getLogger(__name__)

QUERY_STOPWORDS = frozenset({
    "как", "кто", "что", "это", "такое", "такой", "такая", "такие", "где",
    "когда", "куда", "откуда", "почему", "зачем", "какой", "какая", "какое",
    "какие", "каков", "какова", "каковы", "чей", "чья", "чье", "чьи", "зовут",
    "ли", "же", "бы", "не", "ни", "на", "по", "из", "от", "до", "за", "при",
    "про", "без", "под", "над", "об", "обо", "во", "со", "ко", "для", "или",
    "и", "а", "но", "да", "в", "с", "у", "к", "о",
})


def _substantive_tokens(query: str) -> list[str]:
    return [tok for tok in tokens(query) if tok not in QUERY_STOPWORDS]


def _token_stem_matches(query_token: str, doc_tokens: set[str]) -> bool:
    if query_token in doc_tokens:
        return True
    if len(query_token) >= 5:
        stem = query_token[:max(4, len(query_token) - 2)]
        return any(tok.startswith(stem) for tok in doc_tokens)
    return False


def _has_substantive_match(fragment: Fragment, title: str, queries: list[str]) -> bool:
    doc_tokens = set(tokens(fragment.text)) | set(tokens(title))
    any_substantive = False
    for query in queries:
        sub = _substantive_tokens(query)
        if not sub:
            continue
        any_substantive = True
        if any(_token_stem_matches(tok, doc_tokens) for tok in sub):
            return True
    return not any_substantive


def rrf(rankings: list[list[str]]) -> dict[str, float]:
    scores = {}
    for ranking in rankings:
        for rank, fragment_id in enumerate(dict.fromkeys(ranking), 1):
            scores[fragment_id] = scores.get(fragment_id, 0.0) + 1 / (60 + rank)
    return scores


def _ordered(scores):
    return sorted(scores, key=lambda key: (-scores[key], key))


def _snapshot(store, warnings=None, *, vectors=False):
    # active_publications rejects hybrid fingerprints before a caller can elect BM25.
    # This equivalent ready join also pins metadata in the SAME SQL statement, so a
    # fallback never rereads a newer publication halfway through the request.
    rows = store.rows("""SELECT p.id AS publication_id,p.manifest,p.fragments,d.metadata,d.payload,d.id FROM documents d
        JOIN publications p ON p.id=d.active_publication
        WHERE d.status='ready' ORDER BY d.id""")
    snapshots = []
    for row in rows:
        try:
            manifest = IndexManifest.model_validate_json(row["manifest"])
            if manifest.document_id != row['id'] or manifest.publication_id != row['publication_id']:
                raise ValueError('Publication identity mismatch')
            fragments = [Fragment.model_validate(f) for f in json.loads(row["fragments"])]
            validate_binding(Document.model_validate_json(row['payload']), manifest, fragments)
            validate_lexical(store, manifest, fragments)
            if vectors and manifest.mode == 'hybrid':
                try:
                    load_vectors(store, manifest, fragments)
                except ImportError:
                    pass  # BM25 remains usable without FAISS installed.
            snapshots.append((manifest, fragments, MetadataPatch.model_validate_json(row["metadata"])))
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            log.warning('Excluded inconsistent ready publication: %s', row['id'])
            if warnings is not None:
                warnings.append(f"Документ {row['id']} исключён из поиска: индекс или версия не согласованы; проверьте индекс документа.")
    if rows and not snapshots:
        raise ValueError('No valid ready publication remains')
    return snapshots


def _visible(snapshots, as_of):
    eligible = {}
    for manifest, fragments, meta in snapshots:
        if meta.reviewed:
            if meta.valid_from and as_of < meta.valid_from or meta.valid_to and as_of > meta.valid_to:
                continue
        eligible[manifest.document_id] = (manifest, fragments, meta)
    excluded = {}
    repeal_warnings: list[tuple[str, str]] = []
    amend_pairs: list[tuple[str, str]] = []
    # A repeal is an effective event, not a temporary property of the repealer's
    # current validity window. Expiry of the repealer does not revive its target.
    for manifest, fragments, meta in snapshots:
        for relation in meta.relations or []:
            if not relation.confirmed:
                continue
            if relation.kind == "repeals":
                # An issued date is NOT an effective repeal date.
                if not meta.reviewed or meta.valid_from is None:
                    repeal_warnings.append((relation.target_id, "Дата вступления отмены в силу не подтверждена."))
                    continue
                if as_of < meta.valid_from:
                    continue
                excluded.setdefault(relation.target_id, []).append(relation.target_section)
            elif relation.kind == "amends" and manifest.document_id in eligible:
                amend_pairs.append((manifest.document_id, relation.target_id))
    visible = []
    for doc_id, (manifest, fragments, meta) in eligible.items():
        sections = excluded.get(doc_id, [])
        fragments = [f for f in fragments if not any(
            section is None or f.section == section or f.section.startswith(section + ".")
            for section in sections)]
        visible.append((manifest, fragments))
    return visible, repeal_warnings, amend_pairs


def _bm25(fragments, queries):
    from rank_bm25 import BM25Okapi

    corpus = [tokens(f.text) for f in fragments]
    if not corpus or not any(corpus):
        return {}, [[] for _ in queries]
    model = BM25Okapi(corpus)
    # Positive Robertson IDF avoids negative scores for small/common-term corpora.
    frequency = {}
    for row in corpus:
        for term in set(row):
            frequency[term] = frequency.get(term, 0) + 1
    model.idf = {term: math.log1p((len(corpus) - count + 0.5) / (count + 0.5))
                 for term, count in frequency.items()}
    rankings, aggregate = [], {}
    for query in queries:
        q_tokens = _substantive_tokens(query) or tokens(query)
        scores = model.get_scores(q_tokens)
        current = {f.id: float(score) for f, score in zip(fragments, scores) if score > 0}
        ranking = _ordered(current)[:30]
        rankings.append(ranking)
        for fragment_id in ranking:
            aggregate[fragment_id] = max(aggregate.get(fragment_id, 0), current[fragment_id])
    return aggregate, rankings


class Searcher:
    def __init__(self, store: Store, settings: Settings, embeddings: EmbeddingsClient | None = None,
                 reranker: RerankerClient | None = None):
        self.store, self.settings = store, settings
        self.embeddings, self.reranker = embeddings, reranker

    async def search(self, request: SearchRequest, subqueries: Subqueries | None = None) -> SearchResult:
        try:
            return await self._search(request, subqueries)
        except ComponentError:
            raise
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            raise component_error("index_mismatch", "Индекс повреждён или несовместим; требуется переиндексация.") from exc
        except ImportError as exc:
            raise component_error("model_unavailable", "Для BM25 требуется пакет rank-bm25.") from exc

    async def _search(self, request, subqueries):
        warnings = []
        snapshots = _snapshot(self.store, warnings, vectors=self.settings.ml_enabled)
        visible, repeal_warnings, amend_pairs = _visible(snapshots, request.as_of or date.today())
        fragments = [f for _, shard in visible for f in shard]
        by_id = {f.id: f for f in fragments}
        if len(by_id) != len(fragments):
            raise ValueError("Duplicate fragment IDs across publications")
        queries = list(dict.fromkeys([request.question] + (subqueries.queries if subqueries else [])))
        lexical, rankings = _bm25(fragments, queries)
        scores, score_kind = lexical, "bm25"
        hybrid = (self.settings.ml_enabled and self.embeddings is not None and self.reranker is not None
                  and all(m.mode == "hybrid" and m.embedding_fingerprint == self.settings.fingerprint
                          for m, _, _ in snapshots))
        if not hybrid:
            warnings.append(BM25_WARNING)
            if any(m.mode == "hybrid" and m.embedding_fingerprint != self.settings.fingerprint
                   for m, _, _ in snapshots):
                warnings.append("Ревизия embeddings несовместима; векторы не используются.")
        elif fragments:
            try:
                import faiss
                import numpy as np

                # Validate every shard before combining only as_of-visible vectors.
                matrices = []
                for manifest, shard, _ in snapshots:
                    index = load_vectors(self.store, manifest, shard)
                    matrices.extend(index.reconstruct(i) for i, f in enumerate(shard) if f.id in by_id)
                index = faiss.IndexFlatIP(1024)
                index.add(np.ascontiguousarray(matrices, dtype="float32"))
                query_vectors = await embed(self.embeddings, queries, "query", self.settings)
                _, positions = index.search(query_vectors, min(30, len(fragments)))
                rankings += [[fragments[int(i)].id for i in row if i >= 0] for row in positions]
                scores, score_kind = rrf(rankings), "rrf"
                candidates = [by_id[key] for key in _ordered(scores)[:40]]
                response = RerankResponse.model_validate(await asyncio.wait_for(self.reranker.rerank(
                    RerankRequest(question=request.question, fragments=candidates)), self.settings.request_timeout))
                ids = [s.fragment_id for s in response.scores]
                if (response.model != self.settings.reranker_model or
                        response.revision != self.settings.reranker_revision or
                        len(ids) != len(set(ids)) or set(ids) != {f.id for f in candidates}):
                    raise component_error("invalid_model_output", "Некорректные ID или ревизия reranker.")
                scores = {s.fragment_id: s.score for s in response.scores}
                score_kind = "reranker"
                if self.settings.threshold_calibrated:
                    scores = {key: value for key, value in scores.items()
                              if value >= self.settings.rerank_threshold}
                else:
                    # Logits have no calibrated relevance boundary. In particular,
                    # a negative top score must not discard a semantic match.
                    # Admission of claims belongs to evidence/semantic validation.
                    warnings.append("Порог reranker не откалиброван; качество выдачи не подтверждено.")
            except (ComponentError, TimeoutError, ImportError, ValidationError) as exc:
                log.warning("Hybrid search fell back to BM25 (%s)", type(exc).__name__)
                hybrid = False
                scores, score_kind = lexical, "bm25"
                warnings.append(BM25_WARNING)
                if isinstance(exc, ComponentError):
                    warnings.append(f"ML: {exc.detail.code}. Векторная выдача отброшена.")
                else:
                    warnings.append("ML недоступен или вернул некорректный ответ; векторная выдача отброшена.")
        limit = min(request.limit, self.settings.top_fragments)
        selected_ids = _ordered(scores)[:limit]
        selected_documents = {by_id[key].document_id for key in selected_ids}
        eligible_ids = {manifest.document_id for manifest, _ in visible}
        for target_id, msg in repeal_warnings:
            if target_id in selected_documents or (selected_documents and target_id in eligible_ids):
                warnings.append(msg)
        for src_id, target_id in amend_pairs:
            if src_id in selected_documents or target_id in selected_documents:
                warnings.append("Есть подтверждённая поправка; исходный документ сохранён вместе с ней.")
                break
        # Validity and revision warnings apply only to selected documents with missing dates
        # or unresolved competing amendments/repeals, never unrelated corpus entries.
        for manifest, _, meta in snapshots:
            if manifest.document_id in selected_documents and meta.valid_from is None and meta.issued is None:
                warnings.append(f"Период действия «{meta.title or manifest.document_id}» подтверждён не полностью.")
            for relation in meta.relations or []:
                if relation.confirmed or relation.kind not in ("repeals", "amends"):
                    continue
                if relation.target_id in selected_documents or (
                    manifest.document_id in selected_documents and relation.target_id in eligible_ids
                ):
                    warnings.append("Есть неподтверждённые связи редакций; выбор редакции требует проверки.")
                    break
        return SearchResult(mode="hybrid" if hybrid else "bm25",
            hits=[SearchHit(fragment=by_id[key], rank=i, score=scores[key], score_kind=score_kind)
                  for i, key in enumerate(selected_ids, 1)],
            publication_ids=[m.publication_id for m, _, _ in snapshots],
            warnings=list(dict.fromkeys(warnings)))
