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


def test_full_view_and_citation_preserve_all_published_fragments(service):
    # Artificial long text forces multiple search fragments. The final paragraph
    # must remain accessible from both document cards and a middle citation.
    paragraphs = [f'<p>ARTIFICIAL paragraph {i}: ' + ('readable source text ' * 80) + '</p>'
                  for i in range(12)]
    paragraphs.append('<p>ARTIFICIAL FINAL MARKER &lt;script&gt;unsafe&lt;/script&gt; &amp; end.</p>')
    uploaded = service.upload_file('complete.html', ''.join(paragraphs).encode())
    assert service.process_next_job()
    fragments = service.store.active_publications()[0][1]
    assert len(fragments) > 2
    selected = fragments[len(fragments) // 2]
    document_id = uploaded['document_id']
    with TestClient(create_app(document_service=service)) as client:
        document = next(d for d in client.get('/api/documents').json()['documents'] if d['id'] == document_id)
        assert document['presentation_url'] == f'/api/documents/{document_id}/view'
        for path in [document['presentation_url'], f'/api/sources/{selected.id}']:
            response = client.get(path)
            assert response.status_code == 200
            assert 'ARTIFICIAL FINAL MARKER' in response.text
            assert '&lt;script&gt;unsafe&lt;/script&gt;' in response.text
            assert '<script>' not in response.text
            # Canonical blocks appear once, without chunk overlap or search headers.
            assert response.text.count("ARTIFICIAL FINAL MARKER") == 1
            assert 'sandbox' in response.headers['content-security-policy']
            assert response.headers['x-content-type-options'] == 'nosniff'
        citation = client.get(f'/api/sources/{selected.id}').text
        assert f'source-fragment--selected" id="fragment-{selected.id}"' in citation
        service.store.execute("UPDATE documents SET status='needs_review' WHERE id=?", (document_id,))
        assert client.get(document['presentation_url']).status_code == 404
        assert client.get(f'/api/sources/{selected.id}').status_code == 404
        assert client.get('/api/documents/missing/view').status_code == 404


@pytest.mark.parametrize('extension', ['docx', 'xlsx'])
def test_full_view_exposes_final_content_of_office_documents(service, extension):
    from io import BytesIO
    content = BytesIO()
    final = 'ARTIFICIAL FINAL OFFICE MARKER'
    if extension == 'docx':
        from docx import Document
        document = Document()
        for i in range(12):
            document.add_paragraph(f'ARTIFICIAL paragraph {i}: ' + 'readable body ' * 80)
        document.add_paragraph(final)
        document.save(content)
    else:
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = 'Artificial'
        for i in range(30):
            sheet.append([f'ARTIFICIAL row {i}', 'readable cell ' * 40])
        sheet.append([final, 'end'])
        workbook.save(content)
        workbook.close()
    uploaded = service.upload_file(f'artificial.{extension}', content.getvalue())
    assert service.process_next_job()
    assert service.get_document(uploaded['document_id'])['status'] == 'ready'
    fragments = service.store.active_publications()[0][1]
    assert len(fragments) > 1
    with TestClient(create_app(document_service=service)) as client:
        response = client.get(f"/api/documents/{uploaded['document_id']}/view")
        assert response.status_code == 200
        assert final in response.text
        assert response.text.count(final) == 1
        assert ("<table>" in response.text) == (extension == "xlsx")


def test_production_discovers_real_documents_and_env_demo_is_explicit(monkeypatch):
    monkeypatch.delenv('UI_SYNTHETIC_FALLBACK', raising=False)
    assert create_app().state.integration['documents']['mode'] == 'real'
    assert create_app(synthetic_fallback=False).state.integration['documents']['mode'] == 'real'
    monkeypatch.setenv('UI_SYNTHETIC_FALLBACK', 'true')
    assert create_app().state.integration['documents']['mode'] == 'synthetic'
    assert create_app(synthetic_fallback=False).state.integration['documents']['mode'] == 'real'


@pytest.mark.parametrize('kind', ['pdf', 'docx', 'xlsx'])
def test_canonical_blocks_survive_search_filtering_and_cache_changes(service, monkeypatch, kind):
    """A full viewer must not be reconstructed from search-selected units."""
    from navigator.ingestion.model import Parsed
    from navigator.ingestion.common import FragmentDraft
    from navigator.documents import service as module
    loc = {'pdf': {'kind': 'pdf', 'page': 1},
           'docx': {'kind': 'docx', 'block': 1},
           'xlsx': {'kind': 'xlsx', 'sheet': 'Artificial', 'range': 'A1:B3'}}[kind]
    omitted = 'ARTIFICIAL UNINDEXED CONTENT <script>unsafe</script>'
    if kind == 'pdf':
        blocks = [{'page': 1, 'text': omitted}, {'page': 1, 'table': [['A', 'B'], ['C', 'D']]},
                  {'page': 2, 'text': 'ARTIFICIAL FINAL PAGE'}]
    elif kind == 'docx':
        blocks = [{'index': 0, 'kind': 'paragraph', 'text': omitted},
                  {'index': 1, 'kind': 'table', 'rows': [['A', 'B'], ['C', 'D']]},
                  {'index': 2, 'kind': 'paragraph', 'text': 'ARTIFICIAL FINAL PAGE'}]
    else:
        blocks = [{'sheet': 'Artificial', 'rows': [
            {'row': 1, 'cells': [{'col': 1, 'text': omitted}, {'col': 2, 'text': 'B'}]},
            {'row': 3, 'cells': [{'col': 2, 'text': 'ARTIFICIAL FINAL PAGE'}]}]}]
    parsed = Parsed(media_type=kind, title='Artificial canonical source', blocks=blocks,
                    drafts=[FragmentDraft(text='ARTIFICIAL searchable evidence', section='', locator=loc)])
    monkeypatch.setattr(module, 'parse_file', lambda *args, **kwargs: parsed)
    upload = service.upload_file(f'artificial.{kind}', b'ARTIFICIAL FIXTURE')
    service.process_next_job()
    doc_id = upload['document_id']
    assert service.get_document(doc_id)['status'] == 'ready'
    assert omitted not in service.store.active_publications()[0][1][0].text
    # An unverified cache is irrelevant to the published presentation.
    service.storage.save_extracted_blocks(doc_id, [{'text': 'MUTABLE CACHE INJECTION'}])
    with TestClient(create_app(document_service=service)) as client:
        path = f'/api/documents/{doc_id}/view'
        response = client.get(path)
        assert response.status_code == 200
        assert response.text.count(escape(omitted)) == 1
        assert 'ARTIFICIAL FINAL PAGE' in response.text
        assert '<table>' in response.text
        assert '<script>' not in response.text
        assert 'MUTABLE CACHE INJECTION' not in response.text
        assert response.text.index(escape(omitted)) < response.text.index('ARTIFICIAL FINAL PAGE')
        if kind == 'pdf':
            assert 'Страница 1' in response.text and 'Страница 2' in response.text
        if kind == 'xlsx':
            assert '<tr><td rowspan="1" colspan="1"></td><td rowspan="1" colspan="1"></td></tr>' in response.text
        service.store.execute("UPDATE presentation_snapshots SET blocks='[]' WHERE document_id=?", (doc_id,))
        assert client.get(path).status_code == 409
        service.store.execute("UPDATE documents SET status='needs_review' WHERE id=?", (doc_id,))
        assert client.get(path).status_code == 404
