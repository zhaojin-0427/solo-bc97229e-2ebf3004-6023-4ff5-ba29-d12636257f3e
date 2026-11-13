"""Response models (also drive the OpenAPI / Swagger documentation)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Rect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class HitSpan(BaseModel):
    block_id: str = Field(..., description="OCR block containing this part of the hit")
    page: int = Field(..., description="1-based scan page number")
    rect: Rect = Field(..., description="block rectangle on the scan image")
    char_start: int = Field(
        ..., description="start offset of the highlight inside the original block text"
    )
    char_end: int = Field(
        ..., description="exclusive end offset inside the original block text"
    )
    block_order: int = Field(..., description="reading-order index of the block")


class Hit(BaseModel):
    compact_start: int = Field(..., description="hit start in the whitespace-stripped text")
    compact_end: int = Field(..., description="exclusive hit end in the stripped text")
    matched_text: str = Field(..., description="original characters covered by the hit")
    crosses_blocks: bool
    crosses_pages: bool
    spans: list[HitSpan] = Field(
        ..., description="block ids, pages, rectangles and original char ranges for highlighting"
    )


class SearchResponse(BaseModel):
    query: str
    compact_query: str
    version_id: int
    limit: int
    result_count: int
    has_more: bool = Field(..., description="more than `limit` matches exist")
    hits: list[Hit]


class DocumentInfo(BaseModel):
    doc_id: str
    active_version_id: int | None
    active_state: str | None
    block_count: int | None
    updated_at: float


class VersionInfo(BaseModel):
    version_id: int
    doc_id: str
    state: str
    created_at: float
    finalized_at: float | None
    block_count: int
    error: str | None


class ReplacementAccepted(BaseModel):
    doc_id: str
    version_id: int
    state: str
    detail: str


class RecoveryReport(BaseModel):
    recovered: list[str]


class Health(BaseModel):
    status: str
    db_path: str


# ---------------------------------------------------------------------------
# Citation models
# ---------------------------------------------------------------------------
class CitationCreate(BaseModel):
    version_id: int = Field(
        ..., description="document version the excerpt was taken from; must be the currently active version"
    )
    block_id: str = Field(..., description="id of the OCR block containing the excerpt")
    char_start: int = Field(
        ..., description="inclusive character offset in the block's original text"
    )
    char_end: int = Field(
        ..., description="exclusive character offset in the block's original text; must be greater than char_start"
    )


class CitationConfirm(BaseModel):
    block_id: str = Field(
        ..., description="block of the CURRENT version that contains the excerpt"
    )
    char_start: int = Field(
        ..., description="inclusive character offset in that block's original text"
    )
    char_end: int = Field(..., description="exclusive character offset")


class CitationView(BaseModel):
    citation_id: int
    doc_id: str
    created_version_id: int = Field(
        ..., description="version against which the citation was originally captured"
    )
    version_id: int = Field(
        ..., description="version the current placement (if any) belongs to"
    )
    state: str = Field(
        ...,
        description="located (coordinates valid) | pending_review (needs human confirmation) | relocating",
    )
    quote: str = Field(..., description="cited excerpt, updated when a human confirms a new range")
    block_id: str | None = Field(
        None, description="current-version block id; null unless state is located"
    )
    page: int | None = Field(None, description="scan page; null unless state is located")
    rect: Rect | None = Field(None, description="block rectangle; null unless state is located")
    char_start: int | None = Field(
        None, description="original-text character range; null unless state is located"
    )
    char_end: int | None = None
    review_reason: str | None = Field(
        None, description="why the citation awaits human review (block_missing/quote_not_found/quote_ambiguous)"
    )
    updated_at: float


class CitationPage(BaseModel):
    doc_id: str
    total: int
    limit: int
    offset: int
    has_more: bool
    items: list[CitationView]


class RelocationJobInfo(BaseModel):
    job_id: int
    doc_id: str
    from_version_id: int | None
    to_version_id: int
    state: str
    total_citations: int
    processed: int
    last_citation_id: int
    error: str | None
    created_at: float
    updated_at: float
