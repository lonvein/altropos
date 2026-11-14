"""Dependency seams; implementations belong to subsequent feature branches."""
from typing import Protocol
from contextlib import AbstractAsyncContextManager

from navigator.schemas import (
    Draft, EmbeddingRequest, EmbeddingResponse, ErrorDetail, Fragment, GenerationRequest,
    IndexResult, RerankRequest, RerankResponse, SearchRequest, SearchResult,
    Verification, VerificationRequest,
)


class ComponentError(Exception):
    def __init__(self, detail: ErrorDetail):
        super().__init__(detail.message)
        self.detail = detail


class Indexer(Protocol):
    def index_document(self, document_id: str, fragments: list[Fragment]) -> IndexResult: ...

    def reindex_document(self, document_id: str) -> IndexResult: ...


class Searcher(Protocol):
    async def search(self, request: SearchRequest) -> SearchResult: ...


class EmbeddingsClient(Protocol):
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


class RerankerClient(Protocol):
    async def rerank(self, request: RerankRequest) -> RerankResponse: ...


class LLMClient(Protocol):
    def sequence(self) -> AbstractAsyncContextManager: ...

    async def generate(self, request: GenerationRequest) -> Draft: ...

    async def verify(self, request: VerificationRequest) -> Verification: ...

    async def repair(self, request: VerificationRequest, issues: list[str]) -> Draft: ...
