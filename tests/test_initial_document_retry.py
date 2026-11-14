"""Existing initial originals may be extracted again, never auto-approved."""
import pytest
from fastapi.testclient import TestClient
from navigator.main import create_app
from navigator.config import Settings
from navigator.db import Store
from navigator.documents.service import DocumentService, InitialCorpusReviewRequired
from navigator.corpus import create_manifest


@pytest.mark.parametrize('action', ['retry', 'reocr'])
def test_explicit_initial_retry_creates_reviewable_draft(tmp_path, action):
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    service = DocumentService(Store(tmp_path), settings)
    content = b'<h1>ARTIFICIAL</h1><p>1.1. Artificial complete provision.</p>'
    uploaded = service.upload_file('artificial.html', content)
    service.process_next_job()
    doc_id = uploaded['document_id']
    old = service.store.document(doc_id)['active_publication']
    sources = tmp_path/'sources';sources.mkdir();(sources/'artificial.html').write_bytes(content)
    manifest = tmp_path/'manifest.json';create_manifest(sources, [], manifest)
    service.settings = settings.model_copy(update={'corpus_manifest': manifest})
    if action == 'retry':
        service.store.execute("UPDATE documents SET status='failed' WHERE id=?", (doc_id,))
        job = service.retry_document(doc_id)
    else:
        job = service.reocr_document(doc_id)
    assert service.queue.get_job(job['job_id'])['kind'] == 'review_file'
    assert service.process_next_job()
    assert service.queue.get_job(job['job_id'])['status'] == 'needs_review'
    assert service.store.document(doc_id)['status'] == 'needs_review'
    assert service.store.document(doc_id)['active_publication'] == old
    assert '1.1.' in str(service.get_review_detail(doc_id)['blocks'])
    duplicate = service.upload_file('duplicate.html', content)
    assert duplicate['duplicate'] is True
    assert service.store.document(doc_id)['status'] == 'needs_review'
    fresh = DocumentService(Store(tmp_path/'fresh'), settings.model_copy(
        update={'data_dir': tmp_path/'fresh', 'corpus_manifest': manifest}))
    with pytest.raises(InitialCorpusReviewRequired):
        fresh.upload_file('new-initial.html', content)


def test_service_initialization_never_reextracts_and_mismatched_view_is_preview(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    service = DocumentService(Store(tmp_path), settings)
    job = service.upload_file('artificial.html', b'<p>1.1. ARTIFICIAL full paragraph.</p>')
    service.process_next_job()
    doc_id = job['document_id']
    service.store.execute("UPDATE documents SET payload=json_set(payload,'$.version','pending-v2') WHERE id=?", (doc_id,))
    monkeypatch.setattr(DocumentService, '_heal_inconsistent_documents', lambda self: pytest.fail('Startup must not OCR'))
    for _ in range(3):
        service = DocumentService(service.store, settings)
        assert service.store.document(doc_id)['status'] == 'needs_review'
        assert 'pending-v2' in service.store.document(doc_id)['payload']
    with TestClient(create_app(document_service=service)) as client:
        r = client.get(f'/api/documents/{doc_id}/view')
        assert r.status_code == 200
        assert 'Предварительный просмотр' in r.text and '1.1.' in r.text
        assert client.get(f'/api/sources/{doc_id}-f0000').status_code == 404
