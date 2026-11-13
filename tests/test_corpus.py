"""The initial corpus can only be imported from explicit, unchanged approvals."""
import json
from pathlib import Path

import pytest

from navigator.config import Settings
from navigator.corpus import create_manifest, prepare_initial, publish_initial
from navigator.db import Store
from navigator.review import ReviewError, record_decision


@pytest.fixture
def corpus(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "initial.html").write_text("<p>ИСКУССТВЕННЫЙ ПРИМЕР: заявка координатору.</p>", encoding="utf-8")
    (sources / "manual.html").write_text("<p>ИСКУССТВЕННЫЙ ПРИМЕР: иной документ.</p>", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    split = create_manifest(sources, ["manual.html"], manifest)
    output = tmp_path / "review"
    settings = Settings(_env_file=None, data_dir=tmp_path / "published", ml_enabled=False, llm_enabled=False)
    prepared = prepare_initial(manifest, output, settings)
    return manifest, split, output, settings, prepared


def test_preparation_never_creates_search_database_and_pending_blocks_import(corpus):
    manifest, split, output, settings, prepared = corpus
    assert len(prepared) == 1
    assert len(list(output.glob("*/review.json"))) == 1
    assert prepared[0]["review"]["status"] == "pending"
    with pytest.raises(ReviewError):
        publish_initial(manifest, output, settings, apply=True)
    assert not settings.data_dir.exists()


def test_only_approved_initial_bytes_are_published_without_legal_review(corpus):
    manifest, split, output, settings, prepared = corpus
    directory = prepared[0]["package_dir"]
    record_decision(directory, "approved", reviewer="synthetic-human")
    assert publish_initial(manifest, output, settings) == {"approved": 1, "published": 0, "dry_run": True}
    assert not settings.data_dir.exists()
    result = publish_initial(manifest, output, settings, apply=True)
    assert result["published"] == 1
    store = Store(settings.data_dir)
    documents = store.rows("SELECT sha,status,metadata FROM documents")
    assert len(documents) == 1 and documents[0]["status"] == "ready"
    assert documents[0]["sha"] == next(f["sha256"] for f in split["files"] if f["group"] == "initial")
    assert json.loads(documents[0]["metadata"])["reviewed"] is not True
    assert publish_initial(manifest, output, settings, apply=True)["published"] == 1
    assert len(store.rows("SELECT id FROM documents")) == 1


def test_source_change_after_approval_requires_new_review(corpus):
    manifest, split, output, settings, prepared = corpus
    record_decision(prepared[0]["package_dir"], "approved", reviewer="synthetic-human")
    (Path(split["source_dir"]) / "initial.html").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        publish_initial(manifest, output, settings, apply=True)
    assert not settings.data_dir.exists()


def test_manual_pilot_approval_cannot_be_reclassified_for_preload(corpus):
    manifest, split, output, settings, prepared = corpus
    prepared = prepare_initial(manifest, output, settings, pilots=["manual.html"])
    for package in prepared:
        record_decision(package["package_dir"], "approved", reviewer="synthetic-human")
    changed = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in changed["files"]:
        entry["group"] = "initial"
    manifest.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="Manual-test"):
        publish_initial(manifest, output, settings, apply=True)
    assert not settings.data_dir.exists()


@pytest.mark.parametrize("decision", ["rejected", "excluded"])
def test_explicitly_declined_initial_package_is_not_imported(corpus, decision):
    manifest, split, output, settings, prepared = corpus
    record_decision(prepared[0]["package_dir"], decision, reviewer="synthetic-human")
    assert publish_initial(manifest, output, settings) == {"approved": 0, "published": 0, "dry_run": True}
    assert not settings.data_dir.exists()
