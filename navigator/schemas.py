from datetime import date
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ID = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Mode = Literal["hybrid", "bm25"]


class AskRequest(Schema):
    question: Text = Field(min_length=3, max_length=4000)
    as_of: date | None = None

    @model_validator(mode="after")
    def nonempty(self):
        self.question = self.question.strip()
        if len(self.question) < 3:
            raise ValueError("Введите содержательный вопрос")
        return self


class Evidence(Schema):
    fragment_id: ID
    quote: str = Field(min_length=3)


class Claim(Schema):
    text: Text = Field(max_length=1800)
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


class Draft(Schema):
    status: Literal["answered", "partial", "not_found", "ambiguous", "conflict"]
    summary: list[Claim] = Field(default_factory=list, max_length=5)
    steps: list[Claim] = Field(default_factory=list, max_length=16)
    warnings: list[str] = Field(default_factory=list, max_length=8)
    clarification: str | None = None

    @model_validator(mode="after")
    def claims_required(self):
        if self.status in ("answered", "partial") and not (self.summary or self.steps):
            raise ValueError("Ответ без подтвержденных утверждений")
        return self


class Verification(Schema):
    supported: bool
    complete: bool
    conflict: bool = False
    issues: list[str] = Field(default_factory=list)


class Subqueries(Schema):
    queries: list[str] = Field(default_factory=list, max_length=3)


class Relation(Schema):
    target_id: str
    kind: Literal["amends", "repeals", "appendix"]
    target_section: str | None = None
    confirmed: bool = False


class MetadataPatch(Schema):
    title: str | None = Field(default=None, max_length=500)
    number: str | None = None
    issued: date | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    reviewed: bool | None = None
    relations: list[Relation] | None = None

    @model_validator(mode="after")
    def valid_dates(self):
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("Начало действия позже окончания")
        return self


class ImportRequest(Schema):
    url: str = Field(max_length=2000)
    selected_urls: list[str] = Field(default_factory=list, max_length=10)



class PDFLocator(Schema):
    kind: Literal["pdf"] = "pdf"
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def valid_box(self):
        if self.bbox:
            x0, y0, x1, y1 = self.bbox
            if not (0 <= x0 < x1 and 0 <= y0 < y1):
                raise ValueError("Invalid bbox")
        return self


class DOCXLocator(Schema):
    kind: Literal["docx"] = "docx"
    block: int = Field(ge=0)


class XLSXLocator(Schema):
    kind: Literal["xlsx"] = "xlsx"
    sheet: Text
    range: str = Field(pattern=r"^[A-Z]+[1-9][0-9]*(?::[A-Z]+[1-9][0-9]*)?$")


class HTMLLocator(Schema):
    kind: Literal["html"] = "html"
    block: int = Field(ge=0)


Locator = Annotated[PDFLocator | DOCXLocator | XLSXLocator | HTMLLocator, Field(discriminator="kind")]


class Document(Schema):
    id: ID
    version: ID
    name: Text
    title: Text = Field(max_length=500)
    sha256: SHA256
    original_path: Text
    media_type: Literal["pdf", "docx", "xlsx", "html"]
    status: Literal["queued", "processing", "needs_review", "ready", "failed"] = "queued"
    source_url: str | None = None
    retrieved_at: date | None = None
    metadata: MetadataPatch = Field(default_factory=MetadataPatch)
    warnings: list[Text] = Field(default_factory=list)


class Fragment(Schema):
    id: ID
    document_id: ID
    document_version: ID
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    section: str = ""
    locator: Locator

    @model_validator(mode="after")
    def nonblank(self):
        if not self.text.strip():
            raise ValueError("Empty fragment")
        return self


class Artifact(Schema):
    path: Text
    sha256: SHA256


class IndexManifest(Schema):
    schema_version: Literal[1] = 1
    publication_id: ID
    document_id: ID
    document_version: ID
    fragments_sha256: SHA256
    mode: Mode
    bm25: Artifact
    vectors: Artifact | None = None
    embedding_fingerprint: Text | None = None

    @model_validator(mode="after")
    def vector_contract(self):
        if self.mode == "hybrid" and (self.vectors is None or self.embedding_fingerprint is None):
            raise ValueError("Hybrid requires vectors and fingerprint")
        if self.mode == "bm25" and (self.vectors is not None or self.embedding_fingerprint is not None):
            raise ValueError("BM25 must omit vectors")
        return self


class IndexResult(Schema):
    document_id: ID
    publication_id: ID
    mode: Mode
    fragment_count: int = Field(gt=0)
    warnings: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def warning_required(self):
        if self.mode == "bm25" and not self.warnings:
            raise ValueError("BM25 requires warning")
        return self


class SearchRequest(AskRequest):
    limit: int = Field(default=8, ge=1, le=8)


class SearchHit(Schema):
    fragment: Fragment
    rank: int = Field(ge=1)
    score: float
    score_kind: Literal["bm25", "rrf", "reranker"]


class SearchResult(Schema):
    mode: Mode
    hits: list[SearchHit] = Field(default_factory=list, max_length=8)
    publication_ids: list[ID] = Field(default_factory=list)
    warnings: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_hits(self):
        if self.mode == "bm25" and not self.warnings:
            raise ValueError("BM25 requires warning")
        if len({h.fragment.id for h in self.hits}) != len(self.hits):
            raise ValueError("Duplicate hits")
        if [h.rank for h in self.hits] != list(range(1, len(self.hits) + 1)):
            raise ValueError("Ranks must be consecutive from 1")
        return self


class Source(Schema):
    fragment_id: ID
    document_id: ID
    title: Text
    locator: Locator
    url: str = Field(pattern=r"^/api/sources/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

    @model_validator(mode="after")
    def correct_url(self):
        if self.url != f"/api/sources/{self.fragment_id}":
            raise ValueError("Source URL must match fragment_id")
        return self


class Timings(Schema):
    search_ms: float = Field(default=0, ge=0)
    generation_ms: float = Field(default=0, ge=0)
    verification_ms: float = Field(default=0, ge=0)


class AskResponse(Schema):
    status: Literal["answered", "partial", "not_found", "ambiguous", "conflict", "unavailable"]
    summary: list[Claim] = Field(default_factory=list, max_length=5)
    steps: list[Claim] = Field(default_factory=list, max_length=16)
    sources: list[Source] = Field(default_factory=list)
    warnings: list[Text] = Field(default_factory=list)
    clarification: Text | None = None
    mode: Mode
    answer_mode: Literal["generated", "extracts", "sources_only"]
    verification: Verification | None = None
    timings: Timings = Field(default_factory=Timings)

    @model_validator(mode="after")
    def coherent(self):
        claims = self.summary + self.steps
        ids = [s.fragment_id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate sources")
        if any(e.fragment_id not in ids for c in claims for e in c.evidence):
            raise ValueError("Evidence has no source")
        if self.status in ("answered", "partial") and not claims:
            raise ValueError("Answer requires claims")
        if self.status in ("not_found", "unavailable") and claims:
            raise ValueError("No claims allowed for this status")
        if self.answer_mode == "sources_only" and claims:
            raise ValueError("Sources-only cannot contain claims")
        if claims and (self.verification is None or not self.verification.supported):
            raise ValueError("Claims require successful verification")
        if self.status == "answered" and (
            not self.verification or not self.verification.complete or self.verification.conflict
        ):
            raise ValueError("Answered requires complete non-conflicting verification")
        if (self.mode == "bm25" or self.answer_mode != "generated" or self.status != "answered") and not self.warnings:
            raise ValueError("Limited response requires warning")
        return self


class ErrorDetail(Schema):
    code: Literal["invalid_request", "not_found", "not_ready", "index_mismatch", "index_publish_failed",
                  "model_unavailable", "model_timeout", "invalid_model_output", "context_exceeded",
                  "storage_error", "busy", "internal_error"]
    message: Text
    retryable: bool = False


class ErrorResponse(Schema):
    error: ErrorDetail


class EmbeddingRequest(Schema):
    texts: list[Text] = Field(min_length=1, max_length=64)
    purpose: Literal["query", "document"]


class EmbeddingResponse(Schema):
    fingerprint: Text
    dimension: Literal[1024] = 1024
    normalized: Literal[True] = True
    vectors: list[list[float]] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_vectors(self):
        for vector in self.vectors:
            if len(vector) != self.dimension or abs(sum(v*v for v in vector) - 1) > 0.01:
                raise ValueError("Expected normalized 1024-dimensional vectors")
        return self


class RerankRequest(Schema):
    question: Text
    fragments: list[Fragment] = Field(min_length=1, max_length=40)


class RerankScore(Schema):
    fragment_id: ID
    score: float


class RerankResponse(Schema):
    model: Text
    revision: Text
    scores: list[RerankScore] = Field(min_length=1, max_length=40)


class GenerationRequest(AskRequest):
    fragments: list[Fragment] = Field(min_length=1, max_length=8)


class VerificationRequest(GenerationRequest):
    draft: Draft
