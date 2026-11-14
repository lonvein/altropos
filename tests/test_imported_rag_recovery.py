"""Artificial imported regulations: no organization files or model weights."""
import asyncio
from io import BytesIO
import json

import httpx
import pytest
from docx import Document as Docx
from fastapi.testclient import TestClient

from navigator.config import Settings
from navigator.db import Store
from navigator.documents.service import DocumentService
from navigator.main import create_app
from navigator.rag.indexing import Indexer, inspect_index
from navigator.rag.pipeline import AnswerPipeline
from navigator.rag.retrieval import Searcher
from navigator.schemas import (AskRequest, Draft, EmbeddingResponse,
    RerankResponse, RerankScore, SearchRequest, Verification)

GROUND = ('2.11. Искусственный пример: при утрате ЭКД участник сообщает в течение суток '
          'в учебное бюро пропусков (тел. +7 (000) 000-00-00). Новую карту выдают в том же учебном бюро.')
QUERIES = ['Куда обратиться, если я потерял электронную карту доступа?',
           'Утеряна ЭКД, куда звонить?', 'Пропала карточка для входа, к кому идти?',
           'Потеряла пропуск: как получить другой?', 'Как восстановить электронные карты?']


class Embeddings:
    def __init__(self, settings): self.settings = settings
    async def embed(self, request):
        # An artificial semantic adapter: different strings share a direction.
        return EmbeddingResponse(fingerprint=self.settings.fingerprint, vectors=[[1.0] + [0.0]*1023 for _ in request.texts])


class Reranker:
    def __init__(self, settings): self.settings, self.calls = settings, []
    async def rerank(self, request):
        self.calls.append(request)
        return RerankResponse(model=self.settings.reranker_model, revision=self.settings.reranker_revision,
            scores=[RerankScore(fragment_id=f.id, score=-4.8 if GROUND in f.text else -8.0) for f in request.fragments])


@pytest.fixture
def imported(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, ml_enabled=True, llm_enabled=True,
                        llm_revision='artificial-llm', reranker_revision='artificial-reranker')
    store = Store(tmp_path); embeddings = Embeddings(settings)
    service = DocumentService(store, settings, indexer=Indexer(store, settings, embeddings))
    document = Docx()
    for text in ('ИСКУССТВЕННЫЙ РЕГЛАМЕНТ', 'Электронная карта доступа (ЭКД) используется в учебном центре.',
                 '2.10. Искусственный пример: просроченные документы не принимаются.', GROUND,
                 '2.12. Искусственный пример: проверка помещения выполняется отдельно.'):
        document.add_paragraph(text)
    output = BytesIO();document.save(output)
    service.http_transport = httpx.MockTransport(lambda request: httpx.Response(200,content=output.getvalue()))
    job = service.import_from_url('https://mipt.ru/artificial-regulation.docx', ['https://mipt.ru/artificial-regulation.docx'])
    assert service.process_next_job()
    finished = service.queue.get_job(job['job_id']);assert finished['status']=='ready'
    docs=service.list_documents();assert len(docs)==1
    return service,docs[0]['id'],embeddings


@pytest.mark.parametrize('question', QUERIES)
def test_imported_clauses_survive_hybrid_ranking_even_with_negative_logits(imported, question):
    service, doc_id, embeddings = imported
    index = inspect_index(service.store, doc_id)
    assert index['verified'] and index['mode']=='hybrid'
    reranker = Reranker(service.settings)
    result=asyncio.run(Searcher(service.store,service.settings,embeddings,reranker).search(SearchRequest(question=question)))
    assert reranker.calls and GROUND in result.hits[0].fragment.text
    assert '2.12.' in '\n'.join(f.text for _,fs in service.store.active_publications(service.settings.fingerprint) for f in fs)
    assert result.hits[0].score < 0


def test_found_imported_ground_reaches_generation_and_server_source(imported):
    service, doc_id, embeddings = imported
    class Model:
        async def generate(self, request):
            f=next(f for f in request.fragments if GROUND in f.text)
            return Draft(status='answered',summary=[{'text':'По пункту 2.11 следует обратиться в учебное бюро в течение суток.',
                'evidence':[{'fragment_id':f.id,'quote':GROUND}]}])
        async def verify(self, request):return Verification(supported=True,complete=True)
    result=asyncio.run(AnswerPipeline(service.store,service.settings,
        Searcher(service.store,service.settings,embeddings,Reranker(service.settings)),Model()).ask(AskRequest(question=QUERIES[0])))
    assert result.answer_mode=='generated' and result.summary and result.verification.supported
    assert result.sources[0].document_id==doc_id and result.sources[0].url.startswith('/api/sources/')
    assert '2.11' in result.summary[0].text


def test_unrelated_broken_publication_does_not_disable_healthy_import(imported):
    service,doc_id,embeddings=imported
    other=service.upload_file('artificial-other.html', b'<p>ARTIFICIAL unrelated source</p>')
    assert service.process_next_job()
    service.store.execute("UPDATE documents SET payload=json_set(payload,'$.version','unpublished-v2') WHERE id=?",(other['document_id'],))
    result=asyncio.run(Searcher(service.store,service.settings,embeddings,Reranker(service.settings)).search(SearchRequest(question=QUERIES[0])))
    assert result.hits[0].fragment.document_id==doc_id
    assert not any(other['document_id'] in w for w in result.warnings)
    assert all(h.fragment.document_id!=other['document_id'] for h in result.hits)


def test_unrelated_corrupt_artifact_is_quarantined(imported):
    service,doc_id,embeddings=imported
    other=service.upload_file('artificial-other.html', b'<p>ARTIFICIAL unrelated source</p>')
    assert service.process_next_job()
    manifest=next(m for m,_ in service.store.active_publications(service.settings.fingerprint) if m.document_id==other['document_id'])
    (service.store.root / manifest.bm25.path).write_bytes(b'ARTIFICIAL corruption')
    result=asyncio.run(Searcher(service.store,service.settings,embeddings,Reranker(service.settings)).search(SearchRequest(question=QUERIES[0])))
    assert result.hits[0].fragment.document_id==doc_id
    assert not any(other['document_id'] in w for w in result.warnings)


def test_uncalibrated_candidates_do_not_authorize_unsupported_answer(imported):
    service,_,embeddings=imported
    class Model:
        def __init__(self):self.verifications=0
        async def generate(self,request):
            f=next(f for f in request.fragments if GROUND in f.text)
            return Draft(status='answered',summary=[{'text':'ARTIFICIAL unsupported statement.',
                'evidence':[{'fragment_id':f.id,'quote':GROUND}]}])
        async def repair(self,request,issues):return await self.generate(request)
        async def verify(self,request):
            self.verifications+=1
            return Verification(supported=False,complete=False,issues=['ARTIFICIAL semantic rejection'])
    model=Model()
    result=asyncio.run(AnswerPipeline(service.store,service.settings,
        Searcher(service.store,service.settings,embeddings,Reranker(service.settings)),model).ask(AskRequest(question='ARTIFICIAL unrelated question?')))
    assert not result.summary and result.answer_mode=='sources_only'
    assert model.verifications==2


def test_reindex_repairs_artifacts_without_reparse_or_duplicate_document(imported, monkeypatch):
    service,doc_id,_=imported
    before=service.store.active_publications(service.settings.fingerprint)[0]
    (service.store.root / before[0].bm25.path).write_bytes(b'ARTIFICIAL damaged artifact')
    assert service.index_status(doc_id)['verified'] is False
    monkeypatch.setattr('navigator.documents.service.parse_file',lambda *args,**kwargs: pytest.fail('Reindex must not parse'))
    with TestClient(create_app(document_service=service)) as client:
        assert client.get(f'/api/documents/{doc_id}/index').json()['verified'] is False
        submitted=client.post(f'/api/documents/{doc_id}/reindex').json()
        # The app worker may already have acquired this job.
        import time
        deadline=time.monotonic()+5
        while service.queue.get_job(submitted['job_id'])['status'] not in {'ready','failed'}:
            assert time.monotonic()<deadline;time.sleep(.05)
        assert service.queue.get_job(submitted['job_id'])['status']=='ready'
        assert client.get(f'/api/documents/{doc_id}/index').json()['verified']
    after=service.store.active_publications(service.settings.fingerprint)[0]
    assert after[1]==before[1] and after[0].fragments_sha256==before[0].fragments_sha256
    assert len(service.list_documents())==1


def test_reindex_never_approves_pending_version(imported):
    service,doc_id,_=imported
    service.store.execute("UPDATE documents SET payload=json_set(payload,'$.version','unpublished-v2') WHERE id=?",(doc_id,))
    service.reindex_document(doc_id);assert service.process_next_job()
    assert any(j['kind']=='reindex' and j['status']=='failed' for j in service.queue.list_jobs())
    assert service.index_status(doc_id)['verified'] is False


def test_failed_hybrid_reindex_keeps_previous_publication(imported):
    service, doc_id, _ = imported
    before = inspect_index(service.store, doc_id)
    service.indexer.embeddings = None
    submitted = service.reindex_document(doc_id)
    assert service.process_next_job()
    assert service.queue.get_job(submitted['job_id'])['status'] == 'failed'
    assert inspect_index(service.store, doc_id) == before


def test_citation_decoding_preserves_numbered_clause_and_contact():
    from navigator.rag.clients import quote_choices
    text = '2.10. Искусственный предыдущий пункт. ' + GROUND + ' 2.12. Искусственный следующий пункт.'
    choices = quote_choices(text, limit=320)
    assert GROUND in choices
    assert all(quote in text and len(quote) <= 320 for quote in choices)
