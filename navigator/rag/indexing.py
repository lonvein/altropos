"""Immutable per-document artifacts.

Runtime dependencies: rank-bm25==0.2.2, faiss-cpu==1.13.2 (CPU; no model weights).

Indexer is synchronous and must run in a worker, outside an asyncio event loop.
Only unavailable/timeout ML failures permit publishing a vector-free BM25 shard.
"""
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from navigator.config import Settings
from navigator.db import Store, fragments_digest
from navigator.interfaces import ComponentError, EmbeddingsClient
from navigator.schemas import (Artifact, Document, EmbeddingRequest, EmbeddingResponse,
                               ErrorDetail, Fragment, IndexManifest, IndexResult)

BM25_WARNING = "Режим BM25: векторный поиск и повторное ранжирование недоступны."
FORMAT_VERSION = 1
TOKENIZER_VERSION = "nfkc-casefold-yo-v1"


def tokens(text: str) -> list[str]:
    """Keep dates/numbers as tokens; never alter the source fragment text."""
    normalized = unicodedata.normalize("NFKC", text.replace("№", " ")).casefold().replace("ё", "е")
    return re.findall(r"[^\W_]+", normalized)


def component_error(code: str, message: str) -> ComponentError:
    return ComponentError(ErrorDetail(code=code, message=message))


async def embed(client: EmbeddingsClient, texts: list[str], purpose: str, settings: Settings):
    import numpy as np

    vectors = []
    for start in range(0, len(texts), 64):
        batch = texts[start:start + 64]
        try:
            response = EmbeddingResponse.model_validate(await asyncio.wait_for(
                client.embed(EmbeddingRequest(texts=batch, purpose=purpose)), settings.request_timeout))
        except TimeoutError as exc:
            raise component_error("model_timeout", "Embeddings не ответили вовремя.") from exc
        except ValidationError as exc:
            raise component_error("invalid_model_output", "Некорректный ответ embeddings.") from exc
        if response.fingerprint != settings.fingerprint:
            raise component_error("index_mismatch", "Несовместимая ревизия embeddings; требуется переиндексация.")
        if len(response.vectors) != len(batch):
            raise component_error("invalid_model_output", "Количество embeddings не совпадает с запросом.")
        vectors.extend(response.vectors)
    matrix = np.ascontiguousarray(vectors, dtype="float32")
    # The contract permits a small norm tolerance; store exact unit vectors for cosine/IP.
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix


def lexical_payload(fragments: list[Fragment]) -> dict:
    return {"format": FORMAT_VERSION, "tokenizer": TOKENIZER_VERSION,
            "ids": [f.id for f in fragments], "tokens": [tokens(f.text) for f in fragments]}


def artifact_bytes(store: Store, manifest: IndexManifest, artifact: Artifact) -> bytes:
    path = store._safe_path(artifact.path)
    base = (store.root / "indexes" / manifest.publication_id).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Misplaced artifact")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("Artifact checksum mismatch")
    return data


def validate_lexical(store: Store, manifest: IndexManifest, fragments: list[Fragment]):
    if fragments_digest(fragments) != manifest.fragments_sha256:
        raise ValueError("Fragment checksum mismatch")
    if json.loads(artifact_bytes(store, manifest, manifest.bm25)) != lexical_payload(fragments):
        raise ValueError("Incompatible BM25 format or fragment mapping")


def validate_binding(document: Document, manifest: IndexManifest, fragments: list[Fragment]):
    """A ready flag does not make a pending extraction a published version."""
    if ((manifest.document_id, manifest.document_version) != (document.id, document.version)
            or not fragments or len({f.id for f in fragments}) != len(fragments)
            or [f.ordinal for f in fragments] != list(range(len(fragments)))
            or any((f.document_id, f.document_version, f.locator.kind) !=
                   (document.id, document.version, document.media_type) for f in fragments)
            or fragments_digest(fragments) != manifest.fragments_sha256):
        raise ValueError("Publication does not match the document version")


def inspect_index(store: Store, document_id: str, *, vectors=True):
    rows = store.rows("""SELECT d.payload,p.id AS publication_id,p.manifest,p.fragments FROM documents d
        JOIN publications p ON p.id=d.active_publication WHERE d.id=? AND d.status='ready'""", (document_id,))
    if not rows:
        raise component_error("not_ready", "Документ ещё не опубликован для поиска.")
    row = rows[0]
    document = Document.model_validate_json(row['payload'])
    manifest = IndexManifest.model_validate_json(row['manifest'])
    if document.id != document_id or manifest.publication_id != row['publication_id']:
        raise ValueError('Publication identity mismatch')
    fragments = [Fragment.model_validate(f) for f in json.loads(row['fragments'])]
    validate_binding(document, manifest, fragments)
    validate_lexical(store, manifest, fragments)
    if vectors and manifest.mode == 'hybrid':
        load_vectors(store, manifest, fragments)
    return {'document_id': document_id, 'publication_id': manifest.publication_id,
            'version': manifest.document_version, 'mode': manifest.mode,
            'fragment_count': len(fragments), 'verified': True}


def load_vectors(store: Store, manifest: IndexManifest, fragments: list[Fragment]):
    import faiss
    import numpy as np

    data = artifact_bytes(store, manifest, manifest.vectors)
    index = faiss.deserialize_index(np.frombuffer(data, dtype="uint8"))
    if (not isinstance(index, faiss.IndexFlatIP) or index.d != 1024
            or index.ntotal != len(fragments)):
        raise ValueError("Incompatible FAISS index")
    vectors = index.reconstruct_n(0, index.ntotal)
    if not np.isfinite(vectors).all() or not np.allclose(
            np.sum(vectors * vectors, axis=1), 1, atol=0.01):
        raise ValueError("Invalid normalized vectors")
    return index


def _write(path: Path, data: bytes):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path):
    # Windows does not expose directory fsync through os.open; files are flushed above.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Indexer:
    def __init__(self, store: Store, settings: Settings, embeddings: EmbeddingsClient | None = None):
        self.store, self.settings, self.embeddings = store, settings, embeddings

    def index_document(self, document_id: str, fragments: list[Fragment]) -> IndexResult:
        try:
            result = self._index(document_id, fragments)
            inspect_index(self.store, document_id, vectors=result.mode == 'hybrid')
            return result
        except ComponentError:
            raise
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            raise component_error("index_publish_failed", "Не удалось опубликовать индекс документа.") from exc

    def reindex_document(self, document_id: str) -> IndexResult:
        """Rebuild only immutable published text; never parse or approve a draft."""
        rows = self.store.rows("""SELECT d.payload,p.id AS publication_id,p.manifest,p.fragments FROM documents d
            JOIN publications p ON p.id=d.active_publication WHERE d.id=? AND d.status='ready'""", (document_id,))
        if not rows:
            raise component_error('not_ready', 'Переиндексация доступна только опубликованному документу.')
        row = rows[0]
        document = Document.model_validate_json(row['payload'])
        manifest = IndexManifest.model_validate_json(row['manifest'])
        if document.id != document_id or manifest.publication_id != row['publication_id']:
            raise ValueError('Publication identity mismatch')
        fragments = [Fragment.model_validate(f) for f in json.loads(row['fragments'])]
        validate_binding(document, manifest, fragments)
        if hashlib.sha256(self.store._safe_path(document.original_path).read_bytes()).hexdigest() != document.sha256:
            raise ValueError('Original source checksum mismatch')
        result = self._index(document_id, fragments, force=True, preserve_hybrid=manifest.mode == 'hybrid')
        inspect_index(self.store, document_id)
        return result

    def _index(self, document_id, fragments, *, force=False, preserve_hybrid=False):
        row = self.store.document(document_id)
        if not row or not row["payload"]:
            raise component_error("not_found", "Документ не зарегистрирован.")
        document = Document.model_validate_json(row["payload"])
        fragments = sorted((Fragment.model_validate(f) for f in fragments), key=lambda f: f.ordinal)
        if row["status"] == "needs_review":
            raise component_error("not_ready", "Документ требует проверки.")
        if (not fragments or len({f.id for f in fragments}) != len(fragments)
                or [f.ordinal for f in fragments] != list(range(len(fragments)))
                or any((f.document_id, f.document_version, f.locator.kind) !=
                       (document.id, document.version, document.media_type) for f in fragments)):
            raise component_error("invalid_request", "Некорректный полный набор фрагментов.")
        digest = fragments_digest(fragments)
        hybrid = self.settings.ml_enabled and self.embeddings is not None
        # Reuse an active compatible publication. Explicitly enabled ML upgrades BM25.
        old = None
        manifest = None
        if row["active_publication"]:
            old = self.store.rows("SELECT manifest,result FROM publications WHERE id=?",
                                  (row["active_publication"],))[0]
            manifest = IndexManifest.model_validate_json(old["manifest"])
            if manifest.document_version == document.version:
                if manifest.fragments_sha256 != digest:
                    raise component_error("invalid_request", "Изменение фрагментов требует новой версии документа.")
                if not force and row["status"] == "ready" and ((hybrid and manifest.mode == "hybrid" and
                        manifest.embedding_fingerprint == self.settings.fingerprint) or
                        (not hybrid and manifest.mode == "bm25")):
                    validate_lexical(self.store, manifest, fragments)
                    if hybrid:
                        try:
                            load_vectors(self.store, manifest, fragments)
                        except ImportError:
                            pass  # Rebuild a vector-free publication below if FAISS was removed.
                        else:
                            return IndexResult.model_validate_json(old["result"])
                    else:
                        return IndexResult.model_validate_json(old["result"])
        warnings = []
        vector_data = None
        if hybrid:
            try:
                import faiss

                matrix = asyncio.run(embed(self.embeddings, [f.text for f in fragments],
                                           "document", self.settings))
                index = faiss.IndexFlatIP(1024)
                index.add(matrix)
                vector_data = faiss.serialize_index(index).tobytes()
            except ImportError:
                warnings.append("FAISS недоступен; требуется установка faiss-cpu и переиндексация.")
            except ComponentError as exc:
                if exc.detail.code not in ("model_unavailable", "model_timeout"):
                    raise
                warnings.append(exc.detail.message)
        mode = "hybrid" if vector_data is not None else "bm25"
        if preserve_hybrid and mode != 'hybrid':
            raise component_error('model_unavailable',
                                  'Гибридный индекс не перестроен: embeddings недоступны; прежняя публикация сохранена.')
        if mode == "bm25":
            warnings.append(BM25_WARNING)
            if (not force and row["status"] == "ready" and old is not None and manifest is not None
                    and manifest.document_version == document.version and manifest.mode == "bm25"):
                validate_lexical(self.store, manifest, fragments)
                return IndexResult.model_validate_json(old["result"])
        publication_id = uuid4().hex
        staging = self.store.root / "indexes" / (".tmp-" + publication_id)
        staging.mkdir()
        payload = json.dumps(lexical_payload(fragments), ensure_ascii=False, sort_keys=True).encode("utf-8")
        _write(staging / "bm25.json", payload)
        if vector_data is not None:
            _write(staging / "vectors.faiss", vector_data)
        _sync_directory(staging)
        staging.rename(staging.with_name(publication_id))
        _sync_directory(staging.parent)
        prefix = f"indexes/{publication_id}/"
        manifest = IndexManifest(publication_id=publication_id, document_id=document.id,
            document_version=document.version, fragments_sha256=digest, mode=mode,
            bm25=Artifact(path=prefix + "bm25.json", sha256=hashlib.sha256(payload).hexdigest()),
            vectors=Artifact(path=prefix + "vectors.faiss", sha256=hashlib.sha256(vector_data).hexdigest())
                if vector_data is not None else None,
            embedding_fingerprint=self.settings.fingerprint if mode == "hybrid" else None)
        return self.store.publish(manifest, fragments, warnings)
