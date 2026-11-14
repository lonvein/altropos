"""Static guarantees for the web UI: import grid, selection model, upload queue, role gating."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "navigator"


def read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_import_results_use_a_two_column_container_grid_that_collapses_to_one():
    css = read("static", "css", "app.css")
    assert "container-type: inline-size" in css
    assert "@container import-result (min-width: 42rem)" in css
    block = css[css.index("@container import-result"):]
    assert "repeat(2, minmax(0, 1fr))" in block[:200]
    assert ".import-links {\n  display: grid;\n  grid-template-columns: minmax(0, 1fr);" in css
    html = read("templates", "documents.html")
    for element in ('id="import-links"', 'id="import-select-all"', 'id="import-select-none"', 'id="import-selected-submit"',
                    'class="checkbox import-link__label"'):
        assert element in html


def test_import_selection_lives_outside_the_dom_and_is_sent_in_batches():
    js = read("static", "js", "documents.js")
    assert "const importSelection = new Set()" in js
    assert "importBatchSize = 10" in js and "selected.slice(i, i + importBatchSize)" in js
    assert "checkbox.checked = importSelection.has(normalized.url)" in js


def test_upload_is_multi_file_with_progress_and_retry():
    html = read("templates", "documents.html")
    assert 'id="document-file"' in html and " multiple" in html and ".html,.htm" in html
    js = read("static", "js", "documents.js")
    for marker in ("xhr.upload.onprogress", "maxParallelUploads", "maxAutoRetries", "retryUploadItem", "pollProcessing"):
        assert marker in js


def test_review_controls_are_rendered_for_administrators_only():
    html = read("templates", "documents.html")
    assert "{% if is_admin %}" in html and "document-card__review" in html
    base = read("templates", "base.html")
    assert "{% if is_admin %}" in base and 'href="/review"' in base
    js = read("static", "js", "documents.js")
    assert "if (isAdmin) {" in js and "patch.reviewed = metaReviewed.checked" in js
