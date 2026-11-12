#!/usr/bin/env python3
"""Download pinned model weights into the Docker models volume.

Run inside a disposable container (see download-models.sh). Weights are not
copied into the image and are not committed to Git. The lock file records the
repository revision and SHA-256 of every large weight file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

LOCK_PATH = Path(__file__).resolve().parents[1] / "config" / "models.lock.json"

ML_ALLOW_PATTERNS = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "1_Pooling/config.json",
    "pytorch_model.bin",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expected: str) -> None:
    if not path.is_file():
        raise SystemExit(f"missing downloaded file: {path}")
    actual = sha256(path)
    if actual != expected:
        raise SystemExit(
            f"checksum mismatch for {path.name}: expected {expected}, got {actual}"
        )
    print(f"verified {path.name} ({path.stat().st_size / 2**30:.2f} GiB)")


def download_llm(lock: dict, profile: str, root: Path) -> None:
    item = lock["profiles"][profile]
    target = root / "gguf"
    target.mkdir(parents=True, exist_ok=True)
    print(f"\n== {profile}: {item['repo']} @ {item['revision']} ({item['quantization']})")
    for filename, expected in item["files"].items():
        print(f"downloading {filename}")
        path = hf_hub_download(
            repo_id=item["repo"],
            filename=filename,
            revision=item["revision"],
            local_dir=str(target),
        )
        verify(Path(path), expected)
    print(f"LLM shard for {profile}: {target / item['first_shard']}")


def download_ml(lock: dict, root: Path) -> None:
    cache_dir = root / "hf" / "hub"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for key, label in (("embedding", "BGE-M3 embeddings"), ("reranker", "BGE reranker")):
        item = lock["ml"][key]
        print(f"\n== {label}: {item['repo']} @ {item['revision']}")
        snapshot = Path(
            snapshot_download(
                repo_id=item["repo"],
                revision=item["revision"],
                cache_dir=str(cache_dir),
                allow_patterns=ML_ALLOW_PATTERNS,
            )
        )
        for filename, expected in item["files"].items():
            verify(snapshot / filename, expected)
        print(f"snapshot: {snapshot}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="4b", choices=["a100", "3060ti", "4b", "ml", "all"])
    parser.add_argument("--root", default="/models")
    args = parser.parse_args()

    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    targets = ["a100", "3060ti", "4b"] if args.target == "all" else (
        [] if args.target == "ml" else [args.target]
    )
    for profile in targets:
        download_llm(lock, profile, root)
    if args.target in {"ml", "all"}:
        download_ml(lock, root)

    print("\nDone. Start with: ./scripts/start.sh %s" % ("4b" if args.target == "all" else args.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
