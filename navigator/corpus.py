"""Local corpus split, extraction preparation and explicit approval-only import."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from navigator.config import Settings

SUPPORTED = {".pdf", ".docx", ".xlsx", ".html", ".htm"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_manifest(source_dir: Path, manual_names: list[str], target: Path) -> dict:
    source_dir = source_dir.resolve()
    paths = sorted(p for p in source_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    names = {p.name for p in paths}
    if not set(manual_names) <= names or len(manual_names) != len(set(manual_names)):
        raise ValueError("Manual-test names are missing or duplicated")
    files = [{"name": p.name, "sha256": digest(p), "bytes": p.stat().st_size,
              "group": "manual_test" if p.name in manual_names else "initial"} for p in paths]
    hashes = {}
    for item in files:
        if item["sha256"] in hashes and hashes[item["sha256"]] != item["group"]:
            raise ValueError("Identical content cannot belong to both initial and manual-test sets")
        hashes[item["sha256"]] = item["group"]
    result = {"schema_version": 1, "source_dir": str(source_dir), "files": files}
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError("Manifest already exists; inspect it before creating a new split")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def read_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported corpus manifest")
    root = Path(manifest["source_dir"]).resolve()
    for item in manifest["files"]:
        name = item["name"]
        source = (root / name).resolve()
        if Path(name).name != name or not source.is_relative_to(root) or source.suffix.lower() not in SUPPORTED:
            raise ValueError("Unsafe corpus source path")
        if item["group"] not in {"initial", "manual_test"} or digest(source) != item["sha256"]:
            raise ValueError("Source or split changed; rebuild and review the manifest")
    return manifest


@contextmanager
def ocr_environment(settings: Settings):
    # Fingerprints and CLI parsing must resolve the same explicitly configured
    # OCR assets. Changes stay inside this preparation/publication process.
    values = {"TESSERACT_CMD": settings.tesseract_cmd, "TESSDATA_PREFIX": settings.tessdata_prefix}
    previous = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is not None:
                os.environ[key] = str(value.resolve())
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepare_initial(manifest_path: Path, out_root: Path, settings: Settings,
                    *, pilots: list[str] | None = None) -> list[dict]:
    """Prepare pending artifacts only. Never instantiate Store, clients or Indexer."""
    from navigator.ingestion import parse_file
    from navigator.ingestion.ocr import make_ocr
    from navigator.review import prepare_document, show_package, write_index

    manifest = read_manifest(manifest_path)
    selected = [item for item in manifest["files"] if item["group"] == "initial" or item["name"] in (pilots or [])]
    if set(pilots or []) - {i["name"] for i in manifest["files"]}:
        raise ValueError("Unknown pilot")
    results = []
    with ocr_environment(settings):
        for item in selected:
            source = Path(manifest["source_dir"]) / item["name"]
            directory = out_root / item["sha256"][:16]
            if directory.exists():
                info = show_package(directory)
                if info["manifest"]["original_sha256"] != item["sha256"] or info["manifest"]["group"] != item["group"]:
                    raise ValueError("Prepared package does not match corpus manifest")
            else:
                # Parse a private byte snapshot, never a mutable external path.
                staging = out_root / ".preparing" / item["sha256"]
                staging.mkdir(parents=True, exist_ok=True)
                snapshot = staging / source.name
                shutil.copyfile(source, snapshot)
                if digest(snapshot) != item["sha256"]:
                    raise ValueError("Source changed during preparation")
                kwargs = {"ocr": make_ocr(settings.tesseract_cmd, settings.tessdata_prefix)} if source.suffix.lower() == ".pdf" else {}
                parsed = parse_file(snapshot, **kwargs)
                info = prepare_document(snapshot, out_root, parsed=parsed, group=item["group"])
                if info["manifest"]["original_sha256"] != item["sha256"]:
                    raise ValueError("Prepared bytes do not match manifest")
            results.append(info)
            write_index(out_root)
            print(json.dumps({"name": item["name"], "group": item["group"], "status": info["review"]["status"],
                              "package_dir": info["package_dir"]}, ensure_ascii=False), flush=True)
    return results


def approved_inputs(manifest_path: Path, out_root: Path, settings: Settings) -> list[dict]:
    """Preflight ALL initial packages before any DB, file import or model call."""
    from navigator.review import load_approved_package, show_package
    manifest = read_manifest(manifest_path)
    approved = []
    with ocr_environment(settings):
        for item in manifest["files"]:
            if item["group"] != "initial":
                continue
            directory = out_root / item["sha256"][:16]
            info = show_package(directory)
            if info["manifest"]["group"] != "initial":
                raise ValueError("Manual-test packages cannot enter the initial search corpus")
            if info["review"]["status"] in {"rejected", "excluded"} and info["review"].get("reviewer"):
                continue
            loaded = load_approved_package(directory)
            if loaded["document"]["sha256"] != item["sha256"]:
                raise ValueError("Approved source does not match initial corpus")
            approved.append(loaded)
    return approved


def publish_initial(manifest_path: Path, out_root: Path, settings: Settings, *, apply: bool = False):
    inputs = approved_inputs(manifest_path, out_root, settings)
    if not apply:
        return {"approved": len(inputs), "published": 0, "dry_run": True}
    from navigator.db import Store
    from navigator.documents.storage import DocumentStorage
    from navigator.documents.snapshots import save_approved_snapshot
    from navigator.rag.clients import EmbeddingsClient
    from navigator.rag.indexing import Indexer
    from navigator.schemas import Document

    store = Store(settings.data_dir)
    storage = DocumentStorage(store.root, settings.max_upload_mb)
    indexer = Indexer(store, settings, EmbeddingsClient(settings) if settings.ml_enabled else None)
    results = []
    for loaded in inputs:
        document = Document.model_validate(loaded["document"])
        data = loaded["source_original"].read_bytes()
        _, _, relative, sha, _ = storage.save_original(document.name, data)
        if sha != document.sha256:
            raise ValueError("Approved original changed before publication")
        if digest(store.root / relative) != document.sha256:
            raise ValueError("Stored original differs from the approved bytes")
        document.original_path = relative
        document.status = "queued"
        # OCR approval must not mark legal validity or relations reviewed.
        canonical = store.register_document(document)
        if canonical != document.id:
            raise ValueError("Existing source has another extraction version; explicit migration required")
        save_approved_snapshot(store, document, loaded)
        results.append(indexer.index_document(document.id, loaded["fragments"]).model_dump(mode="json"))
    return {"approved": len(inputs), "published": len(results), "results": results}
