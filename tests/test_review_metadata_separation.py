"""Recognized-text decisions must not grant or revoke legal metadata review."""
import pytest

from navigator.config import Settings
from navigator.db import Store
from navigator.documents import DocumentService
from navigator.schemas import Document


@pytest.mark.parametrize('reviewed', [False, None, True])
@pytest.mark.parametrize('decision', ['approved', 'needs_review', 'rejected'])
def test_recognition_review_preserves_independent_metadata_state(tmp_path, reviewed, decision):
    service = DocumentService(Store(tmp_path), Settings(_env_file=None, data_dir=tmp_path,
        ml_enabled=False, llm_enabled=False))
    uploaded = service.upload_file('artificial.html', b'<p>ARTIFICIAL recognized clause.</p>')
    assert service.process_next_job()
    document_id = uploaded['document_id']
    row = service.store.document(document_id)
    document = Document.model_validate_json(row['payload'])
    document.metadata.reviewed = reviewed
    service.store.execute('UPDATE documents SET metadata=?,payload=? WHERE id=?',
        (document.metadata.model_dump_json(), document.model_dump_json(), document_id))
    result = service.submit_review(document_id, decision=decision, reviewer='Artificial operator',
        title='ARTIFICIAL revised title')
    assert result['document']['metadata']['reviewed'] is reviewed
    assert result['document']['title'] == 'ARTIFICIAL revised title'
    assert result['review_history'][-1]['decision'] == decision
    assert result['document']['status'] == ('ready' if decision == 'approved' else 'needs_review')
    saved = service.store.document(document_id)
    assert saved['metadata']['reviewed'] is reviewed
    assert Document.model_validate_json(saved['payload']).metadata.reviewed is reviewed
