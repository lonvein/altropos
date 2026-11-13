"""Artificial local files exercise the actual parser, indexer and HTTP source path."""
import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from navigator.config import Settings
from navigator.db import Store
from navigator.documents import DocumentService
from navigator.main import create_app
from navigator.schemas import MetadataPatch


@pytest.fixture
def service(tmp_path):
    settings = Settings(data_dir=tmp_path, ml_enabled=False, llm_enabled=False)
    return DocumentService(Store(tmp_path), settings)


def wait_ready(client, job_id):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] in {"ready", "failed", "needs_review"}:
            return result
        time.sleep(0.02)
    pytest.fail("Document worker did not finish")


def test_upload_worker_ready_source_patch_and_download(service):
    app = create_app(document_service=service)
    content = '<html><body><p>ИСКУССТВЕННЫЙ ПРИМЕР: A &amp; B &lt;script&gt;.</p></body></html>'.encode()
    with TestClient(app) as client:
        uploaded = client.post('/api/documents', files={'file': ('example.html', content, 'text/html')})
        assert uploaded.status_code == 200
        data = uploaded.json()
        assert 'demo' not in data
        assert wait_ready(client, data['job_id'])['status'] == 'ready'
        row = service.store.document(data['document_id'])
        assert json.loads(row['payload'])['status'] == 'ready'
        publication = row['active_publication']
        fragments = service.store.active_publications()[0][1]
        patched = client.patch(f"/api/documents/{data['document_id']}/metadata", json={'title': '<script>title</script>'})
        assert patched.json()['status'] == 'ready'
        assert service.store.document(data['document_id'])['active_publication'] == publication
        source = client.get(f'/api/sources/{fragments[0].id}')
        assert source.status_code == 200
        assert escape(fragments[0].text) in source.text
        assert '&lt;script&gt;title&lt;/script&gt;' in source.text
        assert '<script>' not in source.text
        assert escape(json.dumps(fragments[0].locator.model_dump(mode='json'), ensure_ascii=False)) in source.text
        assert "sandbox" in source.headers['content-security-policy']
        original = client.get(f"/api/documents/{data['document_id']}/original")
        assert original.content == content
        assert original.headers['content-disposition'].startswith('attachment')
        assert 'original_path' not in client.get('/api/documents').text
        assert 'payload' not in client.get('/api/jobs').text
        # Injected services and the lazy ask pipeline must share one Store.
        answer = client.post('/api/ask', json={'question': 'ИСКУССТВЕННЫЙ ПРИМЕР'}).json()
        assert any(s['fragment_id'] == fragments[0].id for s in answer['sources'])
        service.store.execute("UPDATE documents SET status='needs_review' WHERE id=?", (data['document_id'],))
        assert client.get(f'/api/sources/{fragments[0].id}').status_code == 404


def test_review_reprocess_cannot_bypass_unreadable_text(service):
    uploaded = service.upload_file('empty.html', b'<html><body></body></html>')
    assert service.process_next_job()
    doc_id = uploaded['document_id']
    assert service.get_document(doc_id)['status'] == 'needs_review'
    assert service.patch_metadata(doc_id, MetadataPatch(reviewed=True))['status'] == 'queued'
    assert service.process_next_job()
    assert service.get_document(doc_id)['status'] == 'needs_review'
    assert service.store.active_publications() == []


def test_review_retry_publishes_only_after_parser_clears_review(service, monkeypatch):
    from navigator.documents import service as module
    parse = module.parse_file
    first = True
    def marked(path):
        nonlocal first
        parsed = parse(path)
        if first:
            parsed.needs_review = True
            first = False
        return parsed
    monkeypatch.setattr(module, 'parse_file', marked)
    uploaded = service.upload_file('review.html', '<p>ИСКУССТВЕННЫЙ ПРИМЕР: читаемый текст.</p>'.encode())
    service.process_next_job()
    assert service.get_document(uploaded['document_id'])['status'] == 'needs_review'
    service.patch_metadata(uploaded['document_id'], MetadataPatch(reviewed=True, number='DEMO-1'))
    service.process_next_job()
    doc = service.get_document(uploaded['document_id'])
    assert doc['status'] == 'ready'
    assert doc['metadata']['number'] == 'DEMO-1'


def test_original_path_cannot_escape_data_dir(service, tmp_path):
    uploaded = service.upload_file('example.html', b'<p>ARTIFICIAL EXAMPLE</p>')
    secret = tmp_path.parent / 'outside-secret.txt'
    secret.write_text('private')
    service.store.execute('UPDATE documents SET path=? WHERE id=?', (str(secret), uploaded['document_id']))
    with TestClient(create_app(document_service=service)) as client:
        response = client.get(f"/api/documents/{uploaded['document_id']}/original")
        assert response.status_code == 404
        assert 'private' not in response.text
        assert client.post('/api/documents', files={'file': ('bad.exe', b'bad')}).status_code == 422
        assert client.post('/api/import-url', json={'url': 'https://example.org'}).status_code == 422


def test_indexing_serialized_between_services(service):
    started, release = threading.Event(), threading.Event()
    indexer = service.indexer
    class BlockingIndexer:
        def index_document(self, doc_id, fragments):
            started.set()
            assert release.wait(5)
            return indexer.index_document(doc_id, fragments)
    service.indexer = BlockingIndexer()
    first = service.upload_file('one.html', b'<p>Artificial example one: readable body text.</p>')
    service.upload_file('two.html', b'<p>Artificial example two: readable body text.</p>')
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(service.process_next_job)
        try:
            assert started.wait(5)
            other = DocumentService(service.store, service.settings)
            assert other.queue.recover_stale_jobs() == 0
            assert other.process_next_job() is False
            assert service.process_next_job() is False
            assert service.queue.get_job(first['job_id'])['status'] == 'processing'
        finally:
            release.set()
        assert result.result(5)
    assert service.process_next_job()
    assert len(service.store.active_publications()) == 2


def test_health_propagates_degraded(monkeypatch):
    from navigator.ui import health
    async def degraded(url):
        return {'status': 'degraded'}
    monkeypatch.setattr(health, '_probe', degraded)
    settings = Settings(profile='a100', ml_enabled=True, llm_enabled=True,
                        reranker_revision='artificial', llm_revision='artificial')
    result = asyncio.run(health.build_health(settings, {'data_mode': 'real'}))
    assert result['status'] == 'degraded'


def test_real_ask_never_mounts_synthetic_sources():
    ask = APIRouter()
    @ask.post('/api/ask')
    def handler():
        return {}
    app = create_app(ask_router=ask, synthetic_fallback=True)
    assert app.state.integration['documents']['mode'] == 'real'
    assert app.state.synthetic_state is None


def test_changed_original_requires_new_upload(service):
    upload = service.upload_file('immutable.html', b'<p>Artificial immutable source.</p>')
    row = service.store.document(upload['document_id'])
    service.storage.get_original_path(row['path']).write_bytes(b'<p>Changed body.</p>')
    assert service.process_next_job()
    assert service.get_document(upload['document_id'])['status'] == 'failed'
    assert service.store.active_publications() == []


def test_production_discovers_real_documents_and_env_demo_is_explicit(monkeypatch):
    monkeypatch.delenv('UI_SYNTHETIC_FALLBACK', raising=False)
    assert create_app().state.integration['documents']['mode'] == 'real'
    assert create_app(synthetic_fallback=False).state.integration['documents']['mode'] == 'real'
    monkeypatch.setenv('UI_SYNTHETIC_FALLBACK', 'true')
    assert create_app().state.integration['documents']['mode'] == 'synthetic'
    assert create_app(synthetic_fallback=False).state.integration['documents']['mode'] == 'real'
