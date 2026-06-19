import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.ingest import BrowserCaptureKind


class WebSaveItemSummary(BaseModel):
    id: uuid.UUID
    title: str
    source_type: str
    status: str
    summary: str | None = None
    tags: list[str] = []
    metadata: dict[str, Any] = {}
    created_at: datetime
    updated_at: datetime


class WebSaveResponse(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    original_url: str
    normalized_url: str
    source_title: str | None = None
    source_domain: str | None = None
    capture_kind: BrowserCaptureKind
    user_tags: list[str] = []
    saved_at: datetime
    archived_at: datetime | None = None
    extension_version: str | None = None
    metadata: dict[str, Any] = {}
    item: WebSaveItemSummary


class WebSaveListResponse(BaseModel):
    web_saves: list[WebSaveResponse]
    total: int
    page: int
    per_page: int


class WebSaveUpdate(BaseModel):
    archived: bool

