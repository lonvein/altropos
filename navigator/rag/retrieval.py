"""Snapshot retrieval: global BM25, normalized FAISS/IP, RRF(60), pinned reranker."""
import asyncio
import json
import math
import sqlite3
from datetime import date

from pydantic import ValidationError

from navigator.config import Settings
from navigator.db import Store
from navigator.interfaces import ComponentError, EmbeddingsClient, RerankerClient
from navigator.rag.indexing import (BM25_WARNING, component_error, embed, load_vectors,
                                    tokens, validate_lexical)
from navigator.schemas import (Fragment, IndexManifest, MetadataPatch, RerankRequest,
                               RerankResponse, SearchHit, SearchRequest, SearchResult, Subqueries)


def rrf(rankings: list[list[str]]) -> dict[str, float]:
    scores = {}
    for ranking in rankings:
        for rank, fragment_id in enumerate(dict.fromkeys(ranking), 1):
            scores[fragment_id] = scores.get(fragment_id, 0.0) + 1 / (60 + rank)
    return scores


def _ordered(scores):
    return sorted(scores, key=lambda key: (-scores[key], key))


def _snapshot(store):
    # active_publications rejects hybrid fingerprints before a caller can elect BM25.
    # This equivalent ready join also pins metadata in the SAME SQL statement, so a
    # fallback never rereads a newer publication halfway through the request.
    rows = store.rows("""SELECT p.manifest,p.fragments,d.metadata FROM documents d
        JOIN publications p ON p.id=d.active_publication
        WHERE d.status='ready' ORDER BY d.id""")
    snapshots = []
    for row in rows:
        manifest = IndexManifest.model_validate_json(row["manifest"])
        fragments = sorted((Fragment.model_validate(f) for f in json.loads(row["fragments"])),
                           key=lambda f: f.ordinal)
        if (not fragments or len({f.id for f in fragments}) != len(fragments)
                or [f.ordinal for f in fragments] != list(range(len(fragments)))
                or any((f.document_id, f.document_version) !=
                       (manifest.document_id, manifest.document_version) for f in fragments)):
            raise ValueError("Invalid publication mapping")
        validate_lexical(store, manifest, fragments)
        snapshots.append((manifest, fragments, MetadataPatch.model_validate_json(row["metadata"])))
    return snapshots


def _visible(snapshots, as_of, warnings):
    eligible = {}
    for manifest, fragments, meta in snapshots:
        if meta.reviewed:
            if meta.valid_from and as_of < meta.valid_from or meta.valid_to and as_of > meta.valid_to:
                continue
        eligible[manifest.document_id] = (manifest, fragments, meta)
    excluded = {}
    # A repeal is an effective event, not a temporary property of the repealer's
    # current validity window. Expiry of the repealer does not revive its target.
    for manifest, fragments, meta in snapshots:
        for relation in meta.relations or []:
            if not relation.confirmed:
                continue
            if relation.kind == "repeals":
                # An issued date is NOT an effective repeal date.
                if not meta.reviewed or meta.valid_from is None:
                    warnings.append("Дата вступления отмены в силу не подтверждена.")
                    continue
                if as_of < meta.valid_from:
                    continue
                excluded.setdefault(relation.target_id, []).append(relation.target_section)
            elif relation.kind == "amends" and manifest.document_id in eligible:
                warnings.append("Есть подтверждённая поправка; исходный документ сохранён вместе с ней.")
    visible = []
    for doc_id, (manifest, fragments, meta) in eligible.items():
        sections = excluded.get(doc_id, [])
        fragments = [f for f in fragments if not any(
            section is None or f.section == section or f.section.startswith(section + ".")
            for section in sections)]
        visible.append((manifest, fragments))
    return visible


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
        scores = model.get_scores(tokens(query))
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
        snapshots = _snapshot(self.store)
        warnings = []
        visible = _visible(snapshots, request.as_of or date.today(), warnings)
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
                elif len(scores) > 2:
                    top_score = max(scores.values())
                    if top_score > 0:
                        if any(v < 0 for v in scores.values()) or top_score <= 1.0:
                            cutoff = max(0.05, top_score * 0.35)
                        else:
                            cutoff = max(0.0, top_score * 0.25)
                        filtered = {key: value for key, value in scores.items() if value >= cutoff}
                        if filtered:
                            scores = filtered
            except (ComponentError, TimeoutError, ImportError, ValidationError) as exc:
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
