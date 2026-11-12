import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from navigator import schemas as s
from navigator.config import Settings
from navigator.db import Store, fragments_digest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/demo_fragments.json").read_text(encoding="utf-8"))
EXAMPLES = re.findall(r"<!-- schema: (\w+) -->\s*```json\s*(.*?)\s*```",
                      (ROOT / "docs/contracts.md").read_text(encoding="utf-8"), re.S)


@pytest.mark.parametrize("name,payload", EXAMPLES)
def test_documented_examples(name, payload):
    model = getattr(s, name).model_validate_json(payload)
    assert getattr(s, name).model_validate_json(model.model_dump_json()) == model
    if isinstance(model, (s.Draft, s.AskResponse)):
        fragments = {f["id"]: f for f in FIXTURE["fragments"]}
        for claim in model.summary + model.steps:
            for evidence in claim.evidence:
                assert evidence.quote in fragments[evidence.fragment_id]["text"]


def test_fixture_integrity():
    assert FIXTURE["synthetic"] is True
    docs = {d["id"]: s.Document.model_validate(d) for d in FIXTURE["documents"]}
    assert len(docs) == 4
    for item in FIXTURE["fragments"]:
        fragment = s.Fragment.model_validate(item)
        assert "ИСКУССТВЕННЫЙ ПРИМЕР" in fragment.text
        assert fragment.document_version == docs[fragment.document_id].version
        assert fragment.locator.kind == docs[fragment.document_id].media_type


@pytest.mark.parametrize("model,payload", [
    (s.AskRequest, {"question": "   "}),
    (s.AskRequest, {"question": "Вопрос", "extra": True}),
    (s.PDFLocator, {"page": 0}),
    (s.PDFLocator, {"page": 1, "bbox": [4, 2, 1, 3]}),
    (s.DOCXLocator, {"block": -1}),
    (s.HTMLLocator, {"block": -1}),
    (s.XLSXLocator, {"sheet": "demo", "range": "A0"}),
    (s.MetadataPatch, {"valid_from": "2026-11-12", "valid_to": "2026-01-01"}),
    (s.Claim, {"text": "Вывод", "evidence": []}),
    (s.Draft, {"status": "answered"}),
    (s.SearchHit, {"fragment": FIXTURE["fragments"][0], "rank": 1,
                   "score": float("nan"), "score_kind": "rrf"}),
    (s.EmbeddingResponse, {"fingerprint": "demo", "vectors": [[0.0]*1024]}),
    (s.EmbeddingResponse, {"fingerprint": "demo", "vectors": [[1.0]]}),
    (s.Source, {"fragment_id": "f1", "document_id": "d1", "title": "demo",
                "locator": {"kind": "pdf", "page": 1}, "url": "/api/sources/f2"}),
])
def test_invalid_contracts(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_embedding_valid():
    assert s.EmbeddingResponse(fingerprint="demo", vectors=[[1.0] + [0.0]*1023]).normalized


def test_response_requires_verified_sources():
    example = next(json.loads(p) for n, p in EXAMPLES if n == "AskResponse")
    for patch in ({"sources": []}, {"verification": None}, {"status": "answered"},
                  {"answer_mode": "sources_only"}, {"warnings": []}):
        with pytest.raises(ValidationError):
            s.AskResponse.model_validate({**example, **patch})


@pytest.mark.parametrize("profile,context,budget,output,top,device", [
    ("a100", 16384, 8000, 1536, 8, "cuda"),
    ("3060ti", 4096, 2200, 768, 4, "cpu"),
    ("4b", 4096, 2200, 768, 4, "cpu"),
])
def test_profiles(profile, context, budget, output, top, device, monkeypatch):
    # Avoid ambient deployment variables changing defaults under test.
    for key in Settings.model_fields:
        monkeypatch.delenv(key.upper(), raising=False)
    config = Settings(_env_file=None, profile=profile)
    assert (config.context_size, config.context_budget, config.max_output_tokens,
            config.top_fragments, config.ml_device) == (context, budget, output, top, device)
    assert not config.llm_enabled and not config.ml_enabled
    assert config.llm_concurrency == 1 and not config.thinking_enabled


def test_env_example_and_overrides():
    config = Settings(_env_file=ROOT / ".env.example")
    assert not config.ml_enabled and not config.llm_enabled
    assert Settings(_env_file=None, profile="a100", top_fragments=3).top_fragments == 3
    for kwargs in ({"context_size": 20}, {"profile": "unknown"}, {"llm_enabled": True},
                   {"ml_enabled": True}, {"request_timeout": 0}, {"embedding_dimension": 768}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **kwargs)


def prepared(tmp_path, mode="bm25"):
    store = Store(tmp_path)
    document = s.Document.model_validate(FIXTURE["documents"][0])
    fragment = s.Fragment.model_validate(FIXTURE["fragments"][0])
    store.register_document(document)
    directory = tmp_path / "indexes/demo-p1"
    directory.mkdir()
    artifact = directory / "bm25.json"
    artifact.write_bytes(b"synthetic placeholder, not a searchable index")
    ref = s.Artifact(path="indexes/demo-p1/bm25.json", sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    manifest = s.IndexManifest(publication_id="demo-p1", document_id=document.id,
                               document_version=document.version, fragments_sha256=fragments_digest([fragment]),
                               mode=mode, bm25=ref, vectors=ref if mode == "hybrid" else None,
                               embedding_fingerprint="demo@v1:1024:normalized" if mode == "hybrid" else None)
    return store, document, fragment, manifest


def test_publish_reopen_idempotence(tmp_path):
    store, document, fragment, manifest = prepared(tmp_path)
    assert store.active_publications() == []
    result = store.publish(manifest, [fragment], ["Только BM25"])
    assert store.publish(manifest, [fragment], ["Только BM25"]) == result
    reopened = Store(tmp_path)
    assert reopened.document(document.id)["status"] == "ready"
    assert reopened.active_publications() == [(manifest, [fragment])]
    assert len(reopened.rows("SELECT * FROM fragments")) == 1


def test_dedup_and_path_traversal(tmp_path):
    store, document, fragment, manifest = prepared(tmp_path)
    assert store.register_document(document.model_copy(update={"id": "other-id"})) == document.id
    with pytest.raises(ValueError):
        store.register_document(document.model_copy(update={"original_path": "../outside"}))
    escaped = manifest.model_copy(update={"bm25": s.Artifact(path="../outside", sha256="0"*64)})
    with pytest.raises(ValueError):
        store.publish(escaped, [fragment], ["BM25"])


@pytest.mark.parametrize("failure", ["hash", "fragments", "version", "review", "missing"])
def test_failed_publication_stays_invisible(tmp_path, failure):
    store, document, fragment, manifest = prepared(tmp_path)
    if failure == "hash":
        (tmp_path / manifest.bm25.path).write_text("changed")
    elif failure == "fragments":
        fragment = fragment.model_copy(update={"text": "different"})
    elif failure == "version":
        manifest = manifest.model_copy(update={"document_version": "v2"})
    elif failure == "review":
        store.execute("UPDATE documents SET status='needs_review' WHERE id=?", (document.id,))
    else:
        (tmp_path / manifest.bm25.path).unlink()
    with pytest.raises(ValueError):
        store.publish(manifest, [fragment], ["BM25"])
    assert store.active_publications() == []
    assert store.document(document.id)["status"] != "ready"


def test_transaction_rollback_preserves_old_snapshot(tmp_path):
    store, document, fragment, manifest = prepared(tmp_path)
    store.publish(manifest, [fragment], ["BM25"])
    directory = tmp_path / "indexes/demo-p2"
    directory.mkdir()
    (directory / "bm25.json").write_bytes((tmp_path / manifest.bm25.path).read_bytes())
    second = manifest.model_copy(update={"publication_id": "demo-p2", "bm25":
        manifest.bm25.model_copy(update={"path": "indexes/demo-p2/bm25.json"})})
    store.execute("""CREATE TRIGGER fail_ready BEFORE UPDATE OF active_publication ON documents
                     BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.publish(second, [fragment], ["BM25"])
    assert store.active_publications() == [(manifest, [fragment])]
    assert len(store.rows("SELECT * FROM publications")) == 1


def test_fingerprint_mismatch(tmp_path):
    store, document, fragment, manifest = prepared(tmp_path, "hybrid")
    store.publish(manifest, [fragment])
    with pytest.raises(ValueError, match="fingerprint"):
        store.active_publications("other")
    assert store.active_publications(manifest.embedding_fingerprint) == [(manifest, [fragment])]


def test_reindex_cannot_change_source_version(tmp_path):
    store, document, fragment, manifest = prepared(tmp_path)
    store.publish(manifest, [fragment], ["BM25"])
    directory = tmp_path / "indexes/demo-p2"
    directory.mkdir()
    (directory / "bm25.json").write_bytes((tmp_path / manifest.bm25.path).read_bytes())
    changed = fragment.model_copy(update={"id": "changed-fragment", "text": "changed synthetic content"})
    second = manifest.model_copy(update={"publication_id": "demo-p2",
        "fragments_sha256": fragments_digest([changed]),
        "bm25": manifest.bm25.model_copy(update={"path": "indexes/demo-p2/bm25.json"})})
    with pytest.raises(ValueError, match="new document version"):
        store.publish(second, [changed], ["BM25"])
    assert store.active_publications() == [(manifest, [fragment])]


def test_additive_migration(tmp_path):
    with sqlite3.connect(tmp_path / "navigator.sqlite") as con:
        con.execute("""CREATE TABLE documents(id TEXT PRIMARY KEY, name TEXT, sha TEXT UNIQUE,
                       path TEXT, title TEXT, status TEXT, metadata TEXT, url TEXT, created TEXT)""")
        con.execute("INSERT INTO documents(id,name,status,metadata) VALUES('legacy','demo','ready','{}')")
    store = Store(tmp_path)
    assert store.document("legacy")["name"] == "demo"
    assert store.active_publications() == []
