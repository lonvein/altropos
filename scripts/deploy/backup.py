"""Offline snapshot; refuses a running deployment. Restore into NEW empty dir."""
import argparse
import hashlib
import sqlite3
import zipfile
from pathlib import Path
from serve import offline_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    data, archive = args.data.resolve(), args.archive.resolve()
    if not data.is_dir() or archive.is_relative_to(data):
        parser.error("Existing DATA_DIR and archive outside DATA_DIR required")
    with offline_lock(data):
        db = data / "navigator.sqlite"
        if db.exists():
            with sqlite3.connect(db) as con:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Database integrity check failed")
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as out:
            for file in data.rglob("*"):
                if file.is_symlink():
                    raise RuntimeError("Symlinks are not supported in DATA_DIR")
                if file.is_file() and file.name != ".deployment.lock":
                    out.write(file, file.relative_to(data))
    with zipfile.ZipFile(archive) as result:
        if result.testzip() is not None:
            raise RuntimeError("Archive verification failed")
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(archive.suffix + ".sha256").write_text(checksum + "\n", encoding="ascii")
    print("Verified snapshot:", archive)


if __name__ == "__main__":
    main()
