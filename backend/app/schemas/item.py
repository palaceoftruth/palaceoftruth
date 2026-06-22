import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ItemResponse(BaseModel):
    id: uuid.UUID
    source_type: str
    source_url: str | None
    title: str
    summary: str | None
    raw_content: str | None
    content_chunks: Any | None
    # ORM model uses metadata_ to avoid SQLAlchemy MetaData collision
    metadata: dict = Field(default={}, validation_alias="metadata_")
    tags: list[str] = []
    categories: list[str] = []
    status: str
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    effective_date: datetime | None = None
    effective_date_source: str | None = None
    effective_date_quality: str | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class ItemListResponse(BaseModel):
    items: list[ItemResponse]
    total: int
    page: int
    per_page: int
    next_cursor: str | None = None


class ItemUpdate(BaseModel):
    title: str | None = None
    tags: list[str] | None = None
    summary: str | None = None
    categories: list[str] | None = None
    raw_content: str | None = None
    metadata: dict[str, Any] | None = None


class ItemCreate(BaseModel):
    title: str
    source_type: str
    raw_content: str | None = None
    summary: str | None = None
    tags: list[str] = []
    source_url: str | None = None
    metadata: dict[str, Any] = {}
    effective_date: datetime | None = None
    effective_date_source: str | None = None
    effective_date_quality: str | None = None
    skip_ai_enrichment: bool = False


class ItemCreateResponse(BaseModel):
    item_id: uuid.UUID
    status: str
    embedding_queued: bool


class BatchActionRequest(BaseModel):
    action: Literal["delete", "tag", "untag"]
    ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    tags: list[str] | None = None


class BatchActionResponse(BaseModel):
    affected: int
    action: str


class ItemDeleteResponse(BaseModel):
    deleted: bool
    item_id: uuid.UUID
    status: str
    deleted_at: datetime


class ItemRestoreResponse(BaseModel):
    restored: bool
    item: ItemResponse
