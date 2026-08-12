import uuid
from datetime import datetime
from pydantic import BaseModel, field_validator

from app.schemas.item import _bounded_labels


class FeedCreate(BaseModel):
    url: str
    name: str | None = None
    auto_tags: list[str] = []
    poll_interval: int = 3600  # floor enforced in app layer at settings.feed_poll_min_interval

    @field_validator("auto_tags")
    @classmethod
    def bounded_auto_tags(cls, values: list[str]):
        return _bounded_labels(values, "auto_tags") or []


class FeedUpdate(BaseModel):
    name: str | None = None
    auto_tags: list[str] | None = None
    poll_interval: int | None = None
    enabled: bool | None = None

    @field_validator("auto_tags")
    @classmethod
    def bounded_auto_tags(cls, values: list[str] | None):
        return _bounded_labels(values, "auto_tags")


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
