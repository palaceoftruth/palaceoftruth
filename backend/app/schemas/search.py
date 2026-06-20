import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.artifact_citation import ArtifactCitation
from app.schemas.retrieval_provenance import (
    RetrievalDerivedRawClass,
    RetrievalFreshnessClass,
    RetrievalProvenance,
    RetrievalSourceSupportState,
    RetrievalTrustClass,
)
from app.services.retrieval_lenses import validate_retrieval_lens_name

TagsMode = Literal["any", "all"]
SYSTEM_PROVENANCE_TAG_PREFIXES = (
    "skill-",
    "scope-",
    "workspace-",
    "session-",
    "hermes-memory-",
)


def split_system_provenance_tags(tags: list[str]) -> tuple[list[str], list[str]]:
    system_tags: list[str] = []
    semantic_tags: list[str] = []
    for tag in tags:
        if tag.startswith(SYSTEM_PROVENANCE_TAG_PREFIXES):
            system_tags.append(tag)
        else:
            semantic_tags.append(tag)
    return system_tags, semantic_tags


class SearchContextChunk(BaseModel):
    chunk_index: int
    chunk_text: str
    relation: Literal["previous", "matched", "next"]


class SearchResult(BaseModel):
    item_id: uuid.UUID
    title: str
    summary: str | None
    source_type: str
    source_url: str | None
    tags: list[str]
    system_tags: list[str] = Field(default_factory=list)
    semantic_tags: list[str] = Field(default_factory=list)
    source_project: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_scope_type: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_scope_key: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_scope_label: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_item_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    source_span: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: datetime
    chunk_text: str
    chunk_index: int
    score: float
    artifact_citation: ArtifactCitation | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieval_provenance: RetrievalProvenance | None = Field(default=None, exclude_if=lambda value: value is None)
    trust_class: RetrievalTrustClass | None = Field(default=None, exclude_if=lambda value: value is None)
    source_support_state: RetrievalSourceSupportState | None = Field(default=None, exclude_if=lambda value: value is None)
    freshness: RetrievalFreshnessClass | None = Field(default=None, exclude_if=lambda value: value is None)
    derived_raw_classification: RetrievalDerivedRawClass | None = Field(default=None, exclude_if=lambda value: value is None)
    context_chunks: list[SearchContextChunk] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class SearchResponse(BaseModel):
    results: list[SearchResult]
    total: int
    trace: dict | None = None


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(10, ge=1, le=50, alias="top_k")
    candidate_limit: int | None = Field(None, ge=1, le=200)
    include_neighbor_chunks: bool = False
    neighbor_chunk_window: int = Field(1, ge=1, le=5)
    context_budget_chars: int | None = Field(None, ge=200, le=20000)
    source_type: str | None = None
    retrieval_lens: str | None = None
    tags: list[str] | None = None
    tags_mode: TagsMode = "any"
    date_from: datetime | None = None
    date_to: datetime | None = None
    min_score: float | None = None

    model_config = {"populate_by_name": True}

    @field_validator("retrieval_lens")
    @classmethod
    def validate_retrieval_lens(cls, value: str | None) -> str | None:
        return validate_retrieval_lens_name(value)
