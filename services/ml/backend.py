"""BGE inference loaded explicitly at startup from a pinned local HF cache."""
import re

from navigator.schemas import EmbeddingResponse, RerankResponse, RerankScore
from navigator.rag.clients import failure


class BGEBackend:
    def __init__(self, settings):
        if settings.embedding_model != "BAAI/bge-m3" or settings.reranker_model != "BAAI/bge-reranker-v2-m3":
            raise ValueError("This backend supports only BGE-M3 and BGE reranker v2 m3")
        for revision in (settings.embedding_revision, settings.reranker_revision):
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("Pin both BGE models to full 40-character commit revisions")
        # Optional runtime dependencies, never imported by RAG or contract tests.
        import torch
        from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer

        self.settings = settings
        self.torch = torch
        self.device = settings.ml_device
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        common = {"local_files_only": True, "trust_remote_code": False}
        self.embedding_tokenizer = AutoTokenizer.from_pretrained(
            settings.embedding_model, revision=settings.embedding_revision, **common)
        self.embedding = AutoModel.from_pretrained(
            settings.embedding_model, revision=settings.embedding_revision, torch_dtype=dtype, **common
        ).to(self.device).eval()
        self.reranker_tokenizer = AutoTokenizer.from_pretrained(
            settings.reranker_model, revision=settings.reranker_revision, **common)
        self.reranker = AutoModelForSequenceClassification.from_pretrained(
            settings.reranker_model, revision=settings.reranker_revision, torch_dtype=dtype, **common
        ).to(self.device).eval()

    def _inputs(self, tokenizer, texts, pairs=None):
        # Never silently truncate source material.
        encoded = tokenizer(texts, text_pair=pairs, padding=True, truncation=False, return_tensors="pt")
        if encoded["input_ids"].shape[1] > 8192:
            raise failure("context_exceeded", "Вход BGE превышает 8192 токенов.")
        return encoded.to(self.device)

    def embed(self, request):
        vectors = []
        # Small inference batches bound peak memory independently of HTTP batch size.
        with self.torch.inference_mode():
            for start in range(0, len(request.texts), 4):
                inputs = self._inputs(self.embedding_tokenizer, request.texts[start:start + 4])
                cls = self.embedding(**inputs).last_hidden_state[:, 0].float()
                vectors.extend(self.torch.nn.functional.normalize(cls, p=2, dim=1).cpu().tolist())
        return EmbeddingResponse(fingerprint=self.settings.fingerprint, vectors=vectors)

    def rerank(self, request):
        scores = []
        with self.torch.inference_mode():
            for start in range(0, len(request.fragments), 4):
                fragments = request.fragments[start:start + 4]
                inputs = self._inputs(self.reranker_tokenizer, [request.question] * len(fragments),
                                      [fragment.text for fragment in fragments])
                logits = self.reranker(**inputs).logits.view(-1).float().cpu().tolist()
                if len(logits) != len(fragments):
                    raise ValueError("Unexpected reranker logits shape")
                scores.extend(RerankScore(fragment_id=f.id, score=s) for f, s in zip(fragments, logits))
        return RerankResponse(model=self.settings.reranker_model,
                              revision=self.settings.reranker_revision, scores=scores)
