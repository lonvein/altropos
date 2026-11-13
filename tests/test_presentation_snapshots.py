"""Canonical views must stay bound to the ready extraction, not a mutable cache."""
from io import BytesIO
import json

import pytest
from docx import Document
from fastapi.testclient import TestClient

from navigator.config import Settings
from navigator.db import Store
from navigator.documents import DocumentService
from navigator.main import create_app


@pytest.fixture
def published(tmp_path):
    service = DocumentService(Store(tmp_path), Settings(_env_file=None, data_dir=tmp_path,
        review_dir=tmp_path / 'missing-approved-packages', ml_enabled=False, llm_enabled=False))
    document = Document()
    document.add_heading('ARTIFICIAL CANONICAL HEADING', 1)
    document.add_paragraph('ARTIFICIAL FIRST PARAGRAPH')
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = 'ARTIFICIAL TABLE HEADER'
    table.cell(0, 1).text = 'value'
    table.cell(1, 0).text = 'ARTIFICIAL UNIQUE CELL'
    table.cell(1, 1).text = '<script>unsafe</script>'
    document.add_paragraph('ARTIFICIAL FINAL PARAGRAPH')
    buffer = BytesIO()
    document.save(buffer)
    uploaded = service.upload_file('artificial.docx', buffer.getvalue())
    assert service.process_next_job()
    document_id = uploaded['document_id']
    assert service.get_document(document_id)['status'] == 'ready'
    return service, document_id


def test_canonical_body_retains_order_tables_and_escaping(published):
    service, document_id = published
    with TestClient(create_app(document_service=service)) as client:
        response = client.get(f'/api/documents/{document_id}/view')
    assert response.status_code == 200
    page = response.text
    assert page.count('ARTIFICIAL UNIQUE CELL') == 1
    assert page.count('<table>') == 1
    assert page.index('ARTIFICIAL FIRST PARAGRAPH') < page.index('ARTIFICIAL UNIQUE CELL')
    assert page.index('ARTIFICIAL UNIQUE CELL') < page.index('ARTIFICIAL FINAL PARAGRAPH')
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in page and '<script>' not in page


@pytest.mark.parametrize('corruption', ['blocks', 'source_sha', 'version', 'missing'])
def test_reader_refuses_unbound_snapshot_and_mutable_cache(published, corruption):
    service, document_id = published
    # Even a cache containing plausible full text cannot replace a missing or bad
    # immutable snapshot and its explicit approval/version binding.
    service.storage.save_extracted_blocks(document_id, [{'text': 'ARTIFICIAL UNTRUSTED CACHE'}])
    if corruption == 'blocks':
        service.store.execute("UPDATE presentation_snapshots SET blocks='[]' WHERE document_id=?", (document_id,))
    elif corruption == 'source_sha':
        service.store.execute("UPDATE presentation_snapshots SET source_sha=? WHERE document_id=?", ('0' * 64, document_id))
    elif corruption == 'missing':
        service.store.execute('DELETE FROM presentation_snapshots WHERE document_id=?', (document_id,))
    else:
        row = service.store.document(document_id)
        manifest_row = service.store.rows('SELECT manifest FROM publications WHERE id=?', (row['active_publication'],))[0]
        manifest = json.loads(manifest_row['manifest'])
        manifest['document_version'] = 'different-version'
        service.store.execute('UPDATE publications SET manifest=? WHERE id=?',
            (json.dumps(manifest), row['active_publication']))
    fragment = service.store.rows('SELECT id FROM fragments WHERE document_id=? ORDER BY ordinal', (document_id,))[0]['id']
    with TestClient(create_app(document_service=service)) as client:
        for path in [f'/api/documents/{document_id}/view', f'/api/sources/{fragment}']:
            response = client.get(path)
            assert response.status_code == 409
            assert 'ARTIFICIAL UNTRUSTED CACHE' not in response.text
            assert response.json()['error']['code'] == 'not_ready'
