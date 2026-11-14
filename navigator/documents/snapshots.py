"""Canonical presentation snapshots, independent of overlapping search chunks.

The immutable binding is the source version plus the published fragment digest.
Mutable extracted/*.blocks.json files are never trusted by the reader.
"""
import hashlib
import json

from navigator.db import fragments_digest
from navigator.schemas import Document, IndexManifest


def _encoded(blocks):
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        raise ValueError("Invalid presentation blocks")
    return json.dumps(blocks, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _table(store):
    store.execute("""CREATE TABLE IF NOT EXISTS presentation_snapshots (
        document_id TEXT NOT NULL, version TEXT NOT NULL, fragments_sha TEXT NOT NULL,
        source_sha TEXT NOT NULL, blocks TEXT NOT NULL, blocks_sha TEXT NOT NULL,
        PRIMARY KEY(document_id,version,fragments_sha))""")


def save_snapshot(store, document: Document, fragments, blocks):
    """Stage canonical content before publication; preserve existing versions."""
    if not fragments or any((f.document_id, f.document_version) != (document.id, document.version)
                            for f in fragments):
        raise ValueError("Presentation version differs from fragments")
    data = _encoded(blocks)
    digest = hashlib.sha256(data.encode()).hexdigest()
    key = (document.id, document.version, fragments_digest(fragments))
    values = (*key, document.sha256, data, digest)
    _table(store)
    with store.connect() as con:
        existing_for_version = con.execute(
            "SELECT source_sha,blocks,blocks_sha,fragments_sha FROM presentation_snapshots "
            "WHERE document_id=? AND version=?",
            (document.id, document.version),
        ).fetchone()
        if existing_for_version is not None and tuple(existing_for_version) != (
            document.sha256,
            data,
            digest,
            key[2],
        ):
            raise ValueError("Changed canonical content requires a new extraction version")
        con.execute("INSERT OR IGNORE INTO presentation_snapshots VALUES(?,?,?,?,?,?)", values)
        row = con.execute("SELECT source_sha,blocks,blocks_sha FROM presentation_snapshots "
                          "WHERE document_id=? AND version=? AND fragments_sha=?", key).fetchone()
        if tuple(row) != values[3:]:
            raise ValueError("Changed canonical content requires a new extraction version")


def save_approved_snapshot(store, document, approved):
    """Capture validated bytes, then validate the existing approval of that manifest."""
    from navigator.review.workflow import _validate, _digest
    from navigator.review import load_approved_package
    directory = approved["source_original"].parent
    manifest, _, snapshot, _ = _validate(directory)
    checked = load_approved_package(directory)
    source = Document.model_validate(checked["document"])
    if (_digest(manifest) != checked["review"]["manifest_digest"]
            or (source.id, source.version, source.sha256) !=
               (document.id, document.version, document.sha256)
            or fragments_digest(checked["fragments"]) != fragments_digest(approved["fragments"])):
        raise ValueError("Approved snapshot changed or differs from publication")
    save_snapshot(store, document, approved["fragments"], snapshot["blocks"])


def load_snapshot(service, row, fragments):
    manifest = IndexManifest.model_validate_json(row["manifest"])
    document = Document.model_validate_json(row["payload"])
    if ((manifest.document_id, manifest.document_version) != (document.id, document.version)
            or document.id != row["id"] or document.sha256 != row["sha"]
            or fragments_digest(fragments) != manifest.fragments_sha256):
        raise ValueError("Invalid active publication binding")
    _table(service.store)
    key = (document.id, document.version, manifest.fragments_sha256)
    def find():
        return service.store.rows("SELECT source_sha,blocks,blocks_sha FROM presentation_snapshots "
                                  "WHERE document_id=? AND version=? AND fragments_sha=?", key)
    saved = find()
    if not saved:
        # Existing reviewed publications predate the canonical snapshot table.
        # Import exactly their already approved package, with every existing check
        # (including parser fingerprint). Never parse an original or approve anew.
        from navigator.review import load_approved_package
        directory = service.settings.review_dir / document.sha256[:16]
        approved = load_approved_package(directory)
        source = Document.model_validate(approved["document"])
        if ((source.id, source.version, source.sha256) !=
                (document.id, document.version, document.sha256)
                or fragments_digest(approved["fragments"]) != manifest.fragments_sha256):
            raise ValueError("Approved package differs from active publication")
        save_approved_snapshot(service.store, document, approved)
        saved = find()
    entry = saved[0]
    if (entry["source_sha"] != document.sha256
            or hashlib.sha256(entry["blocks"].encode()).hexdigest() != entry["blocks_sha"]):
        raise ValueError("Canonical snapshot digest mismatch")
    blocks = json.loads(entry["blocks"])
    _encoded(blocks)
    return blocks
