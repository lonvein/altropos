"""Local review CLI. No indexing/publication; decision requires a human choice.

Prepare all (each source once):
    python -B scripts/corpus_review.py prepare a.pdf b.docx --out runtime/review
Open runtime/review/index.html, inspect every package, then record the user's
explicit decision with decision PACKAGE --decision approved --reviewer NAME.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navigator.review import (  # noqa: E402
    prepare_document, record_decision, show_package, write_index,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("sources", type=Path, nargs="+")
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--group", default="initial")
    show = commands.add_parser("show")
    show.add_argument("package", type=Path)
    decision = commands.add_parser("decision")
    decision.add_argument("package", type=Path)
    decision.add_argument("--decision", choices=["approved", "rejected", "excluded"], required=True)
    decision.add_argument("--reviewer", required=True)
    decision.add_argument("--comment", default="")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = [prepare_document(path, args.out, group=args.group) for path in args.sources]
        write_index(args.out)
    elif args.command == "show":
        result = show_package(args.package)
    else:
        result = record_decision(args.package, args.decision, reviewer=args.reviewer, comment=args.comment)
        write_index(args.package.parent)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
