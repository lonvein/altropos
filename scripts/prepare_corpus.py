"""Prepare local review artifacts; publishing always requires prior approvals."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from navigator.config import Settings
from navigator.corpus import create_manifest, prepare_initial, publish_initial

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--env-file", type=Path, default=Path(".env"))
sub = parser.add_subparsers(dest="command", required=True)
split = sub.add_parser("split")
split.add_argument("source", type=Path)
split.add_argument("manifest", type=Path)
split.add_argument("--manual", action="append", default=[])
for command in ("prepare", "publish"):
    child = sub.add_parser(command)
    child.add_argument("manifest", type=Path)
    child.add_argument("output", type=Path)
    if command == "prepare":
        child.add_argument("--pilot", action="append", default=[])
    else:
        child.add_argument("--apply", action="store_true")
args = parser.parse_args()
settings = Settings(_env_file=args.env_file)
if args.command == "split":
    result = create_manifest(args.source, args.manual, args.manifest)
    print(json.dumps({"initial": sum(f["group"] == "initial" for f in result["files"]),
                      "manual_test": sum(f["group"] == "manual_test" for f in result["files"])}, ensure_ascii=False))
elif args.command == "prepare":
    prepare_initial(args.manifest, args.output, settings, pilots=args.pilot)
else:
    print(json.dumps(publish_initial(args.manifest, args.output, settings, apply=args.apply), ensure_ascii=False))
