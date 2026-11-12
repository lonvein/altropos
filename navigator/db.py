import json
import sqlite3
import hashlib
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from navigator.schemas import Document, Fragment, IndexManifest, IndexResult


def fragments_digest(fragments: list[Fragment]) -> str:
    payload = [f.model_dump(mode="json") for f in sorted(fragments, key=lambda f: f.ordinal)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, root: Path):
        self.root = root
        for folder in (root, root / "originals", root / "extracted", root / "indexes"):
            folder.mkdir(parents=True, exist_ok=True)
        self.path = root / "navigator.sqlite"
        with self.connect() as con:
            con.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS documents (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, sha TEXT NOT NULL UNIQUE,
              path TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
              metadata TEXT NOT NULL DEFAULT '{}', url TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS fragments (
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
              ordinal INTEGER NOT NULL, text TEXT NOT NULL, locator TEXT NOT NULL,
              section TEXT NOT NULL DEFAULT '', vector BLOB);
            CREATE INDEX IF NOT EXISTS frag_doc ON fragments(document_id,ordinal);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', error TEXT, result TEXT,
              created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS publications (
              id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
              manifest TEXT NOT NULL, fragments TEXT NOT NULL, result TEXT NOT NULL);
            """)
            # Additive migration of the initial local scaffold; never drop user data.
            columns = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
            for name in ("payload", "active_publication"):
                if name not in columns:
                    con.execute(f"ALTER TABLE documents ADD COLUMN {name} TEXT")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def rows(self, sql, args=()):
        with self.connect() as con:
            return [dict(row) for row in con.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as con:
            con.execute(sql, args)

    def document(self, doc_id):
        rows = self.rows("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not rows:
            return None
        row = rows[0]
        row["metadata"] = json.loads(row["metadata"])
        return row

    def enqueue(self, kind, payload):
        job_id = uuid4().hex
        self.execute("INSERT INTO jobs(id,kind,payload) VALUES(?,?,?)",
                     (job_id, kind, json.dumps(payload, ensure_ascii=False)))
        return job_id

    def set_state(self, key, value):
        self.execute("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, value))

    def get_state(self, key):
        rows = self.rows("SELECT value FROM state WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def register_document(self, document: Document) -> str:
        """Immutable source version; identical input hash returns existing document ID."""
        if document.status == "ready":
            raise ValueError("Only publication can mark a document ready")
        self._safe_path(document.original_path)
        payload = document.model_dump_json()
        with self.connect() as con:
            existing = con.execute("SELECT id,sha,payload FROM documents WHERE id=? OR sha=?",
                                   (document.id, document.sha256)).fetchall()
            for row in existing:
                if row["id"] == document.id and row["payload"] != payload:
                    raise ValueError("Document ID already used; create a new version ID")
            if existing:
                return existing[0]["id"]
            con.execute("""INSERT INTO documents(id,name,sha,path,title,status,metadata,url,payload)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (document.id, document.name, document.sha256, document.original_path,
                         document.title, document.status, document.metadata.model_dump_json(),
                         document.source_url, payload))
        return document.id

    def _safe_path(self, relative: str) -> Path:
        path = Path(relative)
        target = (self.root / path).resolve()
        if path.is_absolute() or not target.is_relative_to(self.root.resolve()):
            raise ValueError("Path must stay inside data_dir")
        return target

    def publish(self, manifest: IndexManifest, fragments: list[Fragment],
                warnings: list[str] | None = None) -> IndexResult:
        """Publish prepared artifacts, not build an index. Caller is the sole index writer."""
        if not fragments or len({f.id for f in fragments}) != len(fragments):
            raise ValueError("Expected nonempty unique fragments")
        if sorted(f.ordinal for f in fragments) != list(range(len(fragments))):
            raise ValueError("Ordinals must be consecutive from zero")
        if any((f.document_id, f.document_version) != (manifest.document_id, manifest.document_version)
               for f in fragments):
            raise ValueError("Fragment version does not match manifest")
        if fragments_digest(fragments) != manifest.fragments_sha256:
            raise ValueError("Fragment digest mismatch")
        result = IndexResult(document_id=manifest.document_id, publication_id=manifest.publication_id,
                             mode=manifest.mode, fragment_count=len(fragments), warnings=warnings or [])
        # Files are immutable under indexes/<publication_id>/ and durable BEFORE this transaction.
        for artifact in (manifest.bm25, manifest.vectors):
            if artifact is None:
                continue
            target = self._safe_path(artifact.path)
            base = (self.root / "indexes" / manifest.publication_id).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                raise ValueError("Missing or misplaced index artifact")
            if hashlib.sha256(target.read_bytes()).hexdigest() != artifact.sha256:
                raise ValueError("Artifact digest mismatch")
        ordered = sorted(fragments, key=lambda f: f.ordinal)
        fragment_json = json.dumps([f.model_dump(mode="json") for f in ordered], ensure_ascii=False)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload,status,active_publication FROM documents WHERE id=?",
                              (manifest.document_id,)).fetchone()
            if not row or not row["payload"]:
                raise ValueError("Document must be registered with typed metadata")
            document = Document.model_validate_json(row["payload"])
            if document.version != manifest.document_version or row["status"] == "needs_review":
                raise ValueError("Document is not eligible for publication")
            if any(f.locator.kind != document.media_type for f in fragments):
                raise ValueError("Locator kind must match document media type")
            old = con.execute("SELECT manifest,result FROM publications WHERE id=?",
                              (manifest.publication_id,)).fetchone()
            if old:
                if IndexManifest.model_validate_json(old["manifest"]) != manifest:
                    raise ValueError("Publication ID already used")
                if row["active_publication"] != manifest.publication_id:
                    raise ValueError("Cannot reactivate an obsolete publication")
                return IndexResult.model_validate_json(old["result"])
            # Reindexing can change artifacts, never the meaning of an existing fragment ID.
            for historical in con.execute("SELECT fragments FROM publications WHERE document_id=?",
                                          (manifest.document_id,)):
                old_fragments = [Fragment.model_validate(f) for f in json.loads(historical["fragments"])]
                if fragments_digest(old_fragments) != manifest.fragments_sha256:
                    raise ValueError("Changed fragments require a new document version and ID")
            con.execute("INSERT INTO publications VALUES(?,?,?,?,?)",
                        (manifest.publication_id, manifest.document_id, manifest.model_dump_json(),
                         fragment_json, result.model_dump_json()))
            con.execute("DELETE FROM fragments WHERE document_id=?", (manifest.document_id,))
            for fragment in ordered:
                con.execute("INSERT INTO fragments(id,document_id,ordinal,text,locator,section) VALUES(?,?,?,?,?,?)",
                            (fragment.id, fragment.document_id, fragment.ordinal, fragment.text,
                             json.dumps(fragment.locator.model_dump(mode="json")), fragment.section))
            con.execute("UPDATE documents SET status='ready',active_publication=? WHERE id=?",
                        (manifest.publication_id, manifest.document_id))
        return result

    def active_publications(self, fingerprint: str | None = None) -> list[tuple[IndexManifest, list[Fragment]]]:
        """One SQL snapshot. A loader must also recheck artifact hashes before opening files."""
        rows = self.rows("""SELECT p.manifest,p.fragments FROM documents d JOIN publications p
                            ON p.id=d.active_publication WHERE d.status='ready' ORDER BY d.id""")
        snapshots = []
        for row in rows:
            manifest = IndexManifest.model_validate_json(row["manifest"])
            if manifest.mode == "hybrid" and manifest.embedding_fingerprint != fingerprint:
                raise ValueError("Embedding fingerprint mismatch; reindex or explicitly use BM25")
            fragments = [Fragment.model_validate(f) for f in json.loads(row["fragments"])]
            if fragments_digest(fragments) != manifest.fragments_sha256:
                raise ValueError("Stored fragment digest mismatch")
            snapshots.append((manifest, fragments))
        return snapshots
