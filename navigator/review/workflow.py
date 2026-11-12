"""Self-contained, offline review packages; no storage/indexer/model imports."""
from __future__ import annotations

import hashlib
import html
import importlib.metadata
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from navigator.ingestion import Parsed, parse_file
from navigator.ingestion.common import to_fragments
from navigator.schemas import Document, Fragment


class ReviewError(ValueError):
    """Package is unapproved, incomplete, stale, or modified."""


def _json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _canonical(value):
    return json.dumps(_json(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return _sha(_canonical(value))


def _write_json(path, value):
    # Replace the decision file atomically; an interrupted write must fail closed.
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_canonical(value))
    temporary.replace(path)


def parser_fingerprint():
    """Code, parser libraries and local OCR assets, without starting OCR/models."""
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "ingestion").glob("*.py"))
    files += [root / "schemas.py", Path(__file__).resolve()]
    versions = {}
    for name in ("pdfplumber", "pypdfium2", "python-docx", "openpyxl",
                 "pytesseract", "Pillow", "pydantic", "beautifulsoup4"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    from navigator.ingestion.ocr import find_tesseract, find_tessdata_dir
    command, data = find_tesseract(), find_tessdata_dir()
    assets = {}
    for path in ([Path(command)] if command else []) + (
        sorted(Path(data).glob("*.traineddata")) if data else []
    ):
        assets[str(path.resolve())] = _sha(path.read_bytes())
    return {"code": {str(p.relative_to(root)): _sha(p.read_bytes().replace(b"\r\n", b"\n")) for p in files},
            "libraries": versions, "ocr_assets": assets,
            "ocr_environment": {key: os.environ.get(key) for key in
                                ("TESSERACT_CMD", "TESSDATA_PREFIX")}}


def _escape(value):
    return html.escape(str(value), quote=True)


def _pre(value):
    return "<pre>" + _escape(value) + "</pre>"


def _table(rows):
    return "<table>" + "".join("<tr>" + "".join(
        "<td>" + _escape("" if cell is None else cell) + "</td>"
        for cell in row) + "</tr>" for row in rows) + "</table>"


def _block(block):
    label = _pre(json.dumps({k: v for k, v in block.items()
                            if k not in ("text", "rows", "table")}, ensure_ascii=False))
    if "sheet" in block and isinstance(block.get("rows"), list):
        # Sparse XLSX blocks: preserve blank positions between cells and rows.
        rows = block["rows"]
        by_row = {row["row"]: row for row in rows}
        width = max((c["col"] for row in rows for c in row["cells"]), default=0)
        if width * max(by_row, default=0) > 200_000:
            # Avoid expanding a very sparse workbook to millions of HTML cells.
            # Explicit coordinates keep omitted positions unambiguous.
            return label + "<p>Большой разреженный лист: ячейки с точными адресами; " \
                "пропущенные позиции пусты. Полный снимок — snapshot.json.</p>" + _table([
                    [cell.get("ref", f'row={row["row"]}, col={cell["col"]}'),
                     json.dumps(cell, ensure_ascii=False)] for row in rows for cell in row["cells"]])
        grid = []
        for r in range(1, max(by_row, default=0) + 1):
            cells = {c["col"]: c for c in by_row.get(r, {}).get("cells", [])}
            line = []
            for col in range(1, width + 1):
                c = cells.get(col, {})
                value = c.get("text")
                if c.get("formula") is not None:
                    value = f'{c["formula"]} [cached: {c.get("cached")}]'
                line.append(value)
            grid.append(line)
        return label + _table(grid)
    rows = block.get("table", block.get("rows"))
    return label + (_table(rows) if rows is not None else _pre(block.get("text", "")))


def _pdf_previews(original, directory):
    diagnostics = []
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(str(original))
        try:
            for index in range(len(pdf)):
                item = {"page": index + 1, "rotation": None}
                try:
                    page = pdf[index]
                    try:
                        item["rotation"] = page.get_rotation()
                        bitmap = page.render(scale=1.25)
                        try:
                            image = bitmap.to_pil()
                            try:
                                name = f"page-{index + 1:04d}.png"
                                image.save(directory / name)
                                item["preview"] = name
                            finally:
                                image.close()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                except Exception as exc:
                    item["error"] = f"{type(exc).__name__}: {exc}"
                diagnostics.append(item)
        finally:
            pdf.close()
    except Exception as exc:
        diagnostics.append({"error": f"{type(exc).__name__}: {exc}"})
    return diagnostics


_STYLE = """<meta charset="utf-8"><meta http-equiv="Content-Security-Policy"
content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<style>body{font:16px system-ui;margin:2rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}
table{border-collapse:collapse}td{border:1px solid #888;padding:.4rem;min-width:2rem;white-space:pre-wrap}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.pair img{width:100%}
article{border-top:1px solid #888;margin-top:1rem}a{overflow-wrap:anywhere}</style>"""


def _render(manifest, snapshot, fragments):
    body = ["<!doctype html><html lang='ru'><head>", _STYLE,
            "<title>Проверка распознавания</title></head><body>",
            "<h1>Проверка распознавания</h1>",
            "<p>Решение хранится в review.json. Эта страница — снимок для проверки. "
            "Одобрение распознавания не подтверждает актуальность документа или metadata.reviewed.</p>",
            f'<a download href="{_escape(manifest["original"])}">Скачать оригинал</a>',
            "<h2>Предупреждения и диагностика</h2>",
            _pre(json.dumps(snapshot["warnings"], ensure_ascii=False, indent=2)),
            _pre(json.dumps(snapshot.get("diagnostics", []), ensure_ascii=False, indent=2)),
            _pre(json.dumps(manifest["diagnostics"], ensure_ascii=False, indent=2))]
    if snapshot["needs_review"]:
        body.append("<p>needs_review: одобрение запрещено.</p>")
    if snapshot["media_type"] == "pdf":
        for page in manifest["diagnostics"]:
            body += [f'<article><h2>Страница {_escape(page.get("page", "?"))}</h2>',
                     _pre(json.dumps(page, ensure_ascii=False)), '<div class="pair"><div>']
            if page.get("preview"):
                body.append(f'<img alt="Оригинальная страница" src="{_escape(page["preview"])}">')
            body.append("</div><div>")
            body.extend(_pre(json.dumps(d, ensure_ascii=False))
                        for d in snapshot.get("diagnostics", []) if d.get("page") == page.get("page"))
            body.extend(_block(b) for b in snapshot["blocks"] if b.get("page") == page.get("page"))
            body.append("</div></div></article>")
    else:
        body.append("<h2>Структурные блоки</h2>")
        body.extend("<article>" + _block(b) + "</article>" for b in snapshot["blocks"])
    body.append("<h2>Извлечённые единицы, включая сноски</h2>")
    for unit in snapshot["units"]:
        body += [_pre(json.dumps({k: v for k, v in unit.items() if k != "text"}, ensure_ascii=False)),
                 _pre(unit["text"])]
    body.append("<h2>Итоговые фрагменты и локаторы</h2>")
    for f in fragments:
        body += ["<article>", _pre(f['id'] + " " + json.dumps(f["locator"], ensure_ascii=False)),
                 _pre(f["text"]), "</article>"]
    return "".join(body) + "</body></html>"


def prepare_document(source: Path, out_root: Path, *, parsed: Parsed | None = None,
                     group: str = "initial") -> dict:
    """Create a pending package. Never overwrite an existing package or run OCR twice.

    A supplied Parsed must describe this source; callers own that provenance.
    Package identity is the original SHA prefix; use a fresh out_root to reparse.
    """
    source, out_root = Path(source), Path(out_root)
    data = source.read_bytes()
    sha = _sha(data)
    directory = out_root / sha[:16]
    directory.mkdir(parents=True, exist_ok=False)
    original = directory / ("original" + source.suffix.lower())
    original.write_bytes(data)
    # Parse the snapshot, so changing the external source cannot race the parse.
    parsed = parsed if parsed is not None else parse_file(original)
    drafts = parsed.build()
    snapshot = _json(vars(parsed))
    extraction_digest = _digest(snapshot)
    doc_id = f"doc-{sha[:16]}-{extraction_digest[:16]}"
    version = f"v-{extraction_digest[:16]}"
    fragments = [_json(f) for f in to_fragments(doc_id, version, drafts)]
    diagnostics = _pdf_previews(original, directory) if parsed.media_type == "pdf" else []
    document = Document(id=doc_id, version=version, name=source.name,
                        title=(parsed.title or source.stem)[:500], sha256=sha,
                        original_path=str(original.resolve()), media_type=parsed.media_type,
                        status="needs_review" if parsed.needs_review else "processing",
                        metadata=parsed.metadata or {}, warnings=parsed.warnings)
    manifest = {"schema_version": 1, "group": group, "original": original.name,
                "source_name": source.name, "original_sha256": sha,
                "document": _json(document), "diagnostics": diagnostics,
                "parser_fingerprint": parser_fingerprint(),
                "extraction_digest": extraction_digest,
                "fragments_digest": _digest(fragments), "blocks_digest": _digest(parsed.blocks)}
    _write_json(directory / "snapshot.json", snapshot)
    _write_json(directory / "fragments.json", fragments)
    (directory / "readable.txt").write_text("\n\n".join(u.text for u in parsed.units), encoding="utf-8")
    (directory / "index.html").write_text(_render(manifest, snapshot, fragments), encoding="utf-8")
    manifest["files"] = {p.name: _sha(p.read_bytes()) for p in sorted(directory.iterdir()) if p.is_file()}
    _write_json(directory / "manifest.json", manifest)
    review = {"status": "pending", "reviewer": None, "comment": "",
              "manifest_digest": _digest(manifest)}
    _write_json(directory / "review.json", review)
    return {"package_dir": str(directory.resolve()), "review": review, "manifest": manifest}


def _validate(package_dir):
    directory = Path(package_dir)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        review = json.loads((directory / "review.json").read_text(encoding="utf-8"))
        if review["manifest_digest"] != _digest(manifest):
            raise ReviewError("Manifest changed; prepare a new pending package")
        if manifest["parser_fingerprint"] != parser_fingerprint():
            raise ReviewError("Parser fingerprint changed; approval is invalid")
        for name, digest in manifest["files"].items():
            if Path(name).name != name or (directory / name).is_symlink():
                raise ReviewError("Unsafe artifact path")
            if _sha((directory / name).read_bytes()) != digest:
                raise ReviewError(f"Artifact changed: {name}")
        snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
        fragments = json.loads((directory / "fragments.json").read_text(encoding="utf-8"))
        if (_digest(snapshot) != manifest["extraction_digest"] or
                _digest(fragments) != manifest["fragments_digest"] or
                _digest(snapshot["blocks"]) != manifest["blocks_digest"] or
                _sha((directory / manifest["original"]).read_bytes()) != manifest["original_sha256"]):
            raise ReviewError("Extraction or original changed")
        return manifest, review, snapshot, fragments
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ReviewError("Incomplete or invalid review package") from exc


def show_package(package_dir: Path) -> dict:
    manifest, review, _, _ = _validate(package_dir)
    return {"package_dir": str(Path(package_dir).resolve()), "review": review, "manifest": manifest,
            "html": str((Path(package_dir) / "index.html").resolve())}


def record_decision(package_dir: Path, decision: str, *, reviewer: str, comment: str = "") -> dict:
    """Record an explicit user's decision. Never infer approval from metadata."""
    if decision not in {"approved", "rejected", "excluded"}:
        raise ReviewError("Decision must be approved, rejected, or excluded")
    if not reviewer or not reviewer.strip():
        raise ReviewError("An explicit reviewer is required")
    manifest, _, snapshot, fragments = _validate(package_dir)
    if decision == "approved" and (snapshot["needs_review"] or not fragments or
                                   any(d.get("error") for d in manifest["diagnostics"])):
        raise ReviewError("Technical needs_review or preview errors prevent approval")
    review = {"status": decision, "reviewer": reviewer.strip(), "comment": comment,
              "decided_at": datetime.now(timezone.utc).isoformat(), "manifest_digest": _digest(manifest)}
    _write_json(Path(package_dir) / "review.json", review)
    return review


def load_approved_package(package_dir: Path) -> dict:
    """Return validated publication inputs; leave publication to the caller."""
    manifest, review, snapshot, fragments = _validate(package_dir)
    if (review["status"] != "approved" or not review.get("reviewer", "").strip() or
            snapshot["needs_review"] or not fragments or
            any(d.get("error") for d in manifest["diagnostics"])):
        raise ReviewError("Explicit approval of a technically complete snapshot is required")
    return {"document": manifest["document"],
            "fragments": [Fragment.model_validate(f) for f in fragments],
            "source_original": Path(package_dir).resolve() / manifest["original"],
            "review": review}


def write_index(out_root: Path) -> Path:
    """Write a local HTML entry point for all prepared packages; no decisions."""
    root = Path(out_root)
    rows = []
    for path in sorted(root.glob("*/review.json")):
        info = show_package(path.parent)
        document = info["manifest"]["document"]
        technical = ("требуется исправление перед одобрением" if document["status"] == "needs_review"
                     else "ожидает проверки человеком")
        rows.append(f'<li><a href="{_escape(path.parent.name)}/index.html">'
                    f'{_escape(info["manifest"]["source_name"])}</a> — '
                    f'{_escape(info["review"]["status"])}; '
                    f'{_escape(info["manifest"]["group"])}; {technical}</li>')
    target = root / "index.html"
    target.write_text("<!doctype html><html><head>" + _STYLE +
                      "<title>Корпус для проверки</title></head><body><h1>Корпус для проверки</h1>"
                      "<p>Пакеты ещё не добавлены в поиск. initial — стартовые документы; "
                      "manual_test — отложенные для ручной загрузки. pending — решение не принято.</p><ul>" +
                      "".join(rows) + "</ul></body></html>", encoding="utf-8")
    return target
