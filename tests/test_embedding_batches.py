import asyncio
from navigator.config import Settings
from navigator.rag.indexing import embed
from navigator.schemas import EmbeddingResponse


def test_cpu_sized_embedding_batches_preserve_all_fragments_and_order():
    settings = Settings(_env_file=None)
    texts = [str(i) for i in range(19)]
    batches = []
    class Client:
        async def embed(self, request):
            batches.append(request.texts)
            return EmbeddingResponse(fingerprint=settings.fingerprint,
                vectors=[[0.0]*int(t)+[1.0]+[0.0]*(1023-int(t)) for t in request.texts])
    matrix = asyncio.run(embed(Client(), texts, 'document', settings))
    assert [len(batch) for batch in batches] == [8, 8, 3]
    assert [text for batch in batches for text in batch] == texts
    assert matrix.argmax(axis=1).tolist() == list(range(19))
