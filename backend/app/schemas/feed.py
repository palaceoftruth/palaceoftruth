import uuid
from datetime import datetime
from pydantic import BaseModel


class FeedCreate(BaseModel):
    url: str
    name: str | None = None
    auto_tags: list[str] = []
    poll_interval: int = 3600  # floor enforced in app layer at settings.feed_poll_min_interval


class FeedUpdate(BaseModel):
    name: str | None = None
    auto_tags: list[str] | None = None
    poll_interval: int | None = None
    enabled: bool | None = None


class FeedOut(BaseModel):
    id: uuid.UUID
    url: str
    name: str | None
    auto_tags: list[str]
    poll_interval: int
    enabled: bool
    paused_reason: str | None
    last_fetched_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    feed_metadata: dict
    item_count: int = 0  # computed via COUNT subquery in API layer
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class FeedListResponse(BaseModel):
    feeds: list[FeedOut]
    total: int


class OPMLImportResponse(BaseModel):
    created: int
    skipped: int
    feeds: list[FeedOut]
