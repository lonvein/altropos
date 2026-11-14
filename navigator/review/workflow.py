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
from navigator.ingestion.common import Unit, to_fragments
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


def _cell_text(cell):
    if cell.get("formula") is not None:
        cached = cell.get("cached")
        if cached is not None:
            return str(cached)
        evaluated = cell.get("evaluated")
        if evaluated is not None:
            return str(evaluated)
        return "нет сохранённого результата (не пересчитывалось)"
    return cell.get("text")


def _block(block):
    label = ("<details class='block-details'><summary>Адрес и диагностика блока</summary>" +
             _pre(json.dumps({k: v for k, v in block.items()
                             if k not in ("text", "rows", "table")}, ensure_ascii=False)) + "</details>")
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
                     _cell_text(cell)] for row in rows for cell in row["cells"]])
        grid = []
        for r in range(1, max(by_row, default=0) + 1):
            cells = {c["col"]: c for c in by_row.get(r, {}).get("cells", [])}
            line = []
            for col in range(1, width + 1):
                c = cells.get(col, {})
                value = _cell_text(c)
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
content="default-src 'none'; script-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<style>body{font:16px system-ui;margin:2rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}
table{border-collapse:collapse}td{border:1px solid #888;padding:.4rem;min-width:2rem;white-space:pre-wrap}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.pair img{width:100%}
article{border-top:1px solid #888;margin-top:1rem}a{overflow-wrap:anywhere}
details{margin:1rem 0}summary{cursor:pointer;font-weight:600}
.review-nav{display:flex;flex-wrap:wrap;gap:.75rem;align-items:center;margin-bottom:1.25rem}
.review-back{display:inline-flex;align-items:center;gap:.4rem;padding:.45rem .9rem;border:1px solid #888;border-radius:6px;text-decoration:none;color:inherit;font-weight:500}
.review-back:hover{background:rgba(127,127,127,.12)}
@media(max-width:800px){.pair{grid-template-columns:1fr}}</style>"""


def _render(manifest, snapshot, fragments):
    body = ["<!doctype html><html lang='ru'><head>", _STYLE,
            "<title>Проверка распознавания</title></head><body>",
            "<nav class='review-nav' aria-label='Навигация'>"
            "<a href='../index.html' class='review-back' data-review-back data-review-scope='package' data-back-fallback='../index.html'>← Назад</a>"
            "<a href='/documents' class='review-back'>Документы</a>"
            "</nav>",
            "<h1>Проверка распознавания</h1>",
            f'<h2>{_escape(manifest["source_name"])}</h2>',
            "<p>Решение хранится в review.json. Эта страница — снимок для проверки. "
            "Одобрение распознавания не подтверждает актуальность документа или metadata.reviewed.</p>",
            f'<a download href="{_escape(manifest["original"])}">Скачать оригинал</a>',
            "<h2>Предупреждения</h2>",
            _pre(json.dumps(snapshot["warnings"], ensure_ascii=False, indent=2)),
            "<details id='diagnostics'><summary>Техническая диагностика документа</summary>",
            _pre(json.dumps(snapshot.get("diagnostics", []), ensure_ascii=False, indent=2)),
            _pre(json.dumps(manifest["diagnostics"], ensure_ascii=False, indent=2)), "</details>"]
    if snapshot["needs_review"]:
        body.append("<p>needs_review: одобрение запрещено.</p>")
    body.append("<main id='source-content'>")
    if snapshot["media_type"] == "pdf":
        for page in manifest["diagnostics"]:
            body += [f'<article><h2>Страница {_escape(page.get("page", "?"))}</h2>',
                     '<div class="pair"><div>']
            if page.get("preview"):
                body.append(f'<img alt="Оригинальная страница" src="{_escape(page["preview"])}">')
            body.append("</div><div>")
            body.extend(_block(b) for b in snapshot["blocks"] if b.get("page") == page.get("page"))
            body.append("</div></div></article>")
    else:
        body.append("<h2>Распознанное содержание</h2>")
        body.extend("<article>" + _block(b) + "</article>" for b in snapshot["blocks"])
    body.append("</main><details id='technical-units'><summary>Техническая структура извлечения</summary>"
                "<p><a href='snapshot.json' download>Скачать полный снимок извлечения (JSON)</a></p>")
    for index, unit in enumerate(snapshot["units"]):
        metadata = {k: v for k, v in unit.items()
                    if k not in ("text", "rows", "row_locators", "table_header")}
        metadata.update(index=index, text_chars=len(unit["text"]), rows_count=len(unit.get("rows") or []))
        body.append(_pre(json.dumps(metadata, ensure_ascii=False)))
    body.append("</details><details id='search-fragments'><summary>Фрагменты для поиска и их локаторы</summary>"
                "<p>Это отдельное представление для RAG. Шапки таблиц и контекст могут повторяться "
                "в разных фрагментах, чтобы каждый сохранял смысл.</p>"
                "<p><a href='fragments.json' download>Скачать фрагменты (JSON)</a></p>")
    for f in fragments:
        body += ["<article>", _pre(f['id'] + " " + json.dumps(f["locator"], ensure_ascii=False)),
                 _pre(f["text"]), "</article>"]
    return "".join(body) + "</details><script src='/static/js/review-nav.js' defer></script></body></html>"


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
              "history": [], "manifest_digest": _digest(manifest)}
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


def _append_review_history(existing_review: dict, *, action: str | None = None) -> list[dict]:
    history = list(existing_review.get("history") or [])
    if existing_review.get("status") != "pending" or existing_review.get("reviewer"):
        entry = {
            "status": existing_review.get("status"),
            "reviewer": existing_review.get("reviewer"),
            "comment": existing_review.get("comment", ""),
            "decided_at": existing_review.get("decided_at"),
            "manifest_digest": existing_review.get("manifest_digest"),
        }
        if action:
            entry["action"] = action
        history.append(entry)
    return history


def record_decision(package_dir: Path, decision: str, *, reviewer: str, comment: str = "") -> dict:
    """Record an explicit user's decision. Preserve history; approval does not lock re-review."""
    if decision not in {"approved", "rejected", "excluded"}:
        raise ReviewError("Decision must be approved, rejected, or excluded")
    if not reviewer or not reviewer.strip():
        raise ReviewError("An explicit reviewer is required")
    manifest, existing_review, snapshot, fragments = _validate(package_dir)
    if decision == "approved" and (snapshot["needs_review"] or not fragments or
                                   any(d.get("error") for d in manifest["diagnostics"])):
        raise ReviewError("Technical needs_review or preview errors prevent approval")
    history = _append_review_history(existing_review)
    review = {"status": decision, "reviewer": reviewer.strip(), "comment": comment,
              "decided_at": datetime.now(timezone.utc).isoformat(),
              "history": history, "manifest_digest": _digest(manifest)}
    _write_json(Path(package_dir) / "review.json", review)
    return review


def reopen_package_review(package_dir: Path, *, reviewer: str = "operator", comment: str = "") -> dict:
    """Re-open an approved or decided package for another review while keeping original and history."""
    if not reviewer or not reviewer.strip():
        raise ReviewError("An explicit reviewer is required")
    manifest, existing_review, _, _ = _validate(package_dir)
    history = _append_review_history(existing_review)
    history.append({
        "action": "reopen",
        "status": "pending",
        "reviewer": reviewer.strip(),
        "comment": comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": manifest and _digest(manifest),
    })
    review = {"status": "pending", "reviewer": None, "comment": comment,
              "history": history, "manifest_digest": _digest(manifest)}
    _write_json(Path(package_dir) / "review.json", review)
    return review


def _rebuild_package(directory: Path, manifest: dict, parsed: Parsed, diagnostics: list[dict],
                     history: list[dict], *, comment: str = "") -> dict:
    original = directory / manifest["original"]
    sha = manifest["original_sha256"]
    if _sha(original.read_bytes()) != sha:
        raise ReviewError("Original changed; cannot rebuild review package")
    drafts = parsed.build()
    snapshot = _json(vars(parsed))
    extraction_digest = _digest(snapshot)
    doc_id = manifest["document"]["id"]
    prev_version = manifest["document"].get("version", "v-1")
    rev_num = len(history) + 1
    version = f"v-{extraction_digest[:12]}-r{rev_num}"
    if version == prev_version:
        version = f"{version}-next"
    fragments = [_json(f) for f in to_fragments(doc_id, version, drafts)]
    document = Document(
        id=doc_id,
        version=version,
        name=manifest["source_name"],
        title=(parsed.title or Path(manifest["source_name"]).stem)[:500],
        sha256=sha,
        original_path=str(original.resolve()),
        media_type=parsed.media_type,
        status="needs_review" if parsed.needs_review else "processing",
        metadata=parsed.metadata or {},
        warnings=parsed.warnings,
    )
    new_manifest = {
        "schema_version": 1,
        "group": manifest["group"],
        "original": manifest["original"],
        "source_name": manifest["source_name"],
        "original_sha256": sha,
        "document": _json(document),
        "diagnostics": diagnostics,
        "parser_fingerprint": parser_fingerprint(),
        "extraction_digest": extraction_digest,
        "fragments_digest": _digest(fragments),
        "blocks_digest": _digest(parsed.blocks),
    }
    _write_json(directory / "snapshot.json", snapshot)
    _write_json(directory / "fragments.json", fragments)
    (directory / "readable.txt").write_text("\n\n".join(u.text for u in parsed.units), encoding="utf-8")
    (directory / "index.html").write_text(_render(new_manifest, snapshot, fragments), encoding="utf-8")
    new_manifest["files"] = {
        p.name: _sha(p.read_bytes())
        for p in sorted(directory.iterdir())
        if p.is_file() and p.name not in {"manifest.json", "review.json"}
    }
    _write_json(directory / "manifest.json", new_manifest)
    review = {"status": "pending", "reviewer": None, "comment": comment,
              "history": history, "manifest_digest": _digest(new_manifest)}
    _write_json(directory / "review.json", review)
    return {"package_dir": str(directory.resolve()), "review": review, "manifest": new_manifest}


def update_package_extraction(
    package_dir: Path,
    *,
    units_text: list[str] | None = None,
    blocks: list[dict] | None = None,
    warnings: list[str] | None = None,
    needs_review: bool | None = None,
    reviewer: str = "operator",
    comment: str = "",
) -> dict:
    """Save edited recognition results without re-running OCR; keep original & approval history."""
    directory = Path(package_dir)
    manifest, existing_review, snapshot, _ = _validate(directory)
    history = _append_review_history(existing_review)
    history.append({
        "action": "edit_results",
        "status": "pending",
        "reviewer": reviewer.strip() or "operator",
        "comment": comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": _digest(manifest),
    })

    old_units = [
        Unit(
            text=u["text"],
            locator=dict(u["locator"]),
            section=u.get("section", ""),
            path=tuple(u.get("path") or ()),
            kind=u.get("kind", "paragraph"),
            atomic=bool(u.get("atomic", False)),
            table_header=u.get("table_header", ""),
            rows=u.get("rows"),
            row_locators=u.get("row_locators"),
            note_refs=tuple(u.get("note_refs") or ()),
            note_id=u.get("note_id", ""),
        )
        for u in snapshot.get("units", [])
    ]
    new_blocks = list(blocks) if blocks is not None else list(snapshot.get("blocks", []))
    if units_text is not None:
        if len(units_text) != len(old_units):
            raise ReviewError("Length of units_text must match existing units")
        for u, txt in zip(old_units, units_text):
            u.text = txt
        if blocks is None:
            for b, txt in zip(new_blocks, units_text):
                if "text" in b:
                    b["text"] = txt
    elif blocks is not None:
        rebuilt: list[Unit] = []
        media = manifest["document"]["media_type"]
        for i, b in enumerate(new_blocks):
            loc = {"kind": media}
            if media == "pdf":
                loc["page"] = int(b.get("page", 1))
                if b.get("bbox"):
                    loc["bbox"] = b["bbox"]
            elif media in {"docx", "html"}:
                loc["block"] = int(b.get("index", i))
            elif media == "xlsx":
                loc["sheet"] = b.get("sheet", "Sheet1")
                loc["range"] = b.get("range", "A1")
            rows_data = b.get("table", b.get("rows"))
            if rows_data and isinstance(rows_data, list):
                if media == "xlsx" and rows_data and isinstance(rows_data[0], dict):
                    row_texts = [" | ".join(str(c.get("text", c.get("cached", ""))) for c in r.get("cells", [])) for r in rows_data]
                else:
                    row_texts = [" | ".join(str(c) for c in r) for r in rows_data if isinstance(r, list)]
                header = row_texts[0] if row_texts else ""
                body_rows = row_texts[1:] if len(row_texts) > 1 else row_texts
                rebuilt.append(Unit(
                    text="\n".join(row_texts) or b.get("text", ""),
                    locator=loc,
                    kind="table",
                    table_header=header,
                    rows=body_rows,
                ))
            elif b.get("text"):
                rebuilt.append(Unit(
                    text=str(b["text"]),
                    locator=loc,
                    section=str(b.get("section", "")),
                    kind=str(b.get("kind", "paragraph") or "paragraph"),
                    atomic=len(str(b["text"])) < 900,
                ))
        if rebuilt:
            old_units = rebuilt

    parsed = Parsed(
        media_type=snapshot["media_type"],
        title=snapshot.get("title", ""),
        units=old_units,
        blocks=new_blocks,
        metadata=snapshot.get("metadata") or {},
        warnings=list(warnings) if warnings is not None else list(snapshot.get("warnings", [])),
        needs_review=bool(needs_review) if needs_review is not None else False,
        diagnostics=list(snapshot.get("diagnostics", [])),
    )
    return _rebuild_package(directory, manifest, parsed, manifest["diagnostics"], history, comment=comment)


def reocr_package(
    package_dir: Path,
    *,
    ocr=None,
    parsed: Parsed | None = None,
    reviewer: str = "operator",
    comment: str = "",
) -> dict:
    """Re-run OCR/parsing on the untouched original file, keeping approval history."""
    directory = Path(package_dir)
    manifest, existing_review, _, _ = _validate(directory)
    history = _append_review_history(existing_review)
    history.append({
        "action": "reocr",
        "status": "pending",
        "reviewer": reviewer.strip() or "operator",
        "comment": comment,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "manifest_digest": _digest(manifest),
    })
    original = directory / manifest["original"]
    new_parsed = parsed if parsed is not None else (parse_file(original, ocr=ocr) if ocr is not None else parse_file(original))
    diagnostics = _pdf_previews(original, directory) if new_parsed.media_type == "pdf" else []
    return _rebuild_package(directory, manifest, new_parsed, diagnostics, history, comment=comment)


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
    target.write_text("<!doctype html><html lang='ru'><head>" + _STYLE +
                      "<title>Корпус для проверки</title></head><body>"
                      "<nav class='review-nav' aria-label='Навигация'>"
                      "<a href='/documents' class='review-back' data-review-back data-review-scope='index' data-back-fallback='/documents'>← Назад</a>"
                      "<a href='/' class='review-back'>Задать вопрос</a>"
                      "</nav>"
                      "<h1>Корпус для проверки</h1>"
                      "<p>Пакеты ещё не добавлены в поиск. initial — стартовые документы; "
                      "manual_test — отложенные для ручной загрузки. pending — решение не принято.</p><ul>" +
                      "".join(rows) + "</ul><script src='/static/js/review-nav.js' defer></script></body></html>", encoding="utf-8")
    return target
