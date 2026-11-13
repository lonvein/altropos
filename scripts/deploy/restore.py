"""Verify offline snapshot and restore into a NEW directory only."""
import argparse
import hashlib
from pathlib import Path, PurePosixPath
import sqlite3
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.destination.exists():
        parser.error("Restore destination must not exist")
    expected = args.archive.with_suffix(args.archive.suffix + ".sha256").read_text().strip()
    with args.archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise RuntimeError("Checksum mismatch")
    with zipfile.ZipFile(args.archive) as archive:
        for item in archive.infolist():
            p = PurePosixPath(item.filename)
            if p.is_absolute() or ".." in p.parts or "\\" in item.filename or ":" in item.filename or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError("Unsafe archive member")
        if archive.testzip() is not None:
            raise RuntimeError("Archive CRC failure")
        args.destination.mkdir(parents=True)
        archive.extractall(args.destination)
    db = args.destination / "navigator.sqlite"
    if db.exists():
        with sqlite3.connect(db) as con:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Restored database failed integrity check")
    print("Restored into new directory:", args.destination)


if __name__ == "__main__":
    main()
