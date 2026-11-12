"""Разбор файла из командной строки: python scripts/inspect_file.py <файл> [--json out.json]"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from navigator.ingestion import parse_file  # noqa: E402
from navigator.ingestion.common import (  # noqa: E402
    document_ids, estimate_tokens, sha256_bytes, to_fragments)

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--json")
ap.add_argument("--limit", type=int, default=8)
args = ap.parse_args()

path = Path(args.file)
parsed = parse_file(path)
doc_id, version = document_ids(sha256_bytes(path.read_bytes()))
fragments = to_fragments(doc_id, version, parsed.build())
sizes = [estimate_tokens(f.text) for f in fragments]
print(f"{path.name}: {len(fragments)} фрагментов, ~токенов min/avg/max = "
      f"{min(sizes, default=0)}/{sum(sizes) // max(len(sizes), 1)}/{max(sizes, default=0)}, "
      f"needs_review={parsed.needs_review}")
for w in parsed.warnings:
    print("  ВНИМАНИЕ:", w)
for f in fragments[: args.limit]:
    print(f"--- {f.id} §{f.section or '-'} {f.locator.model_dump(exclude_none=True)}")
    print(f.text[:600])
if args.json:
    Path(args.json).write_text(json.dumps([f.model_dump(mode="json") for f in fragments],
                                          ensure_ascii=False, indent=2), encoding="utf-8")
