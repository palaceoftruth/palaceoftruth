import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


SourceSubscriptionStatus = Literal["active", "paused", "deleted"]
SourceSubscriptionEntryStatus = Literal["discovered", "queued", "captured", "skipped", "failed"]


class SourceSubscriptionCreate(BaseModel):
    provider_type: str = Field(default="youtube_channel", min_length=1, max_length=80)
    source_url: str = Field(min_length=1)
    display_name: str | None = None
    auto_tags: list[str] = Field(default_factory=list)
    poll_interval_seconds: int = 3600
    backfill_enabled: bool = False
    backfill_limit: int | None = Field(default=None, ge=1, le=500)
    backfill_published_after: datetime | None = None

    @model_validator(mode="after")
    def validate_backfill_bounds(self) -> "SourceSubscriptionCreate":
        if self.backfill_enabled and self.backfill_limit is None and self.backfill_published_after is None:
            raise ValueError("Backfill requires either backfill_limit or backfill_published_after")
        return self


class SourceSubscriptionUpdate(BaseModel):
    display_name: str | None = None
    auto_tags: list[str] | None = None
    poll_interval_seconds: int | None = None
    paused_reason: str | None = None


class SourceSubscriptionOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    provider_type: str
    source_url: str
    external_id: str | None
    external_url: str | None
    display_name: str | None
    status: str
    auto_tags: list[str]
    poll_interval_seconds: int
    cursor: dict[str, Any]
    provider_metadata: dict[str, Any]
    last_checked_at: datetime | None
    last_discovered_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    paused_reason: str | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class SourceSubscriptionPreview(BaseModel):
    provider_type: str
    source_url: str
    external_id: str
    external_url: str | None
    display_name: str | None
    provider_metadata: dict[str, Any]
    no_backfill: bool = True
    backfill_enabled: bool = False
    backfill_limit: int | None = None
    backfill_published_after: datetime | None = None


class SourceSubscriptionListResponse(BaseModel):
    subscriptions: list[SourceSubscriptionOut]
    total: int


class SourceSubscriptionEntryOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    subscription_id: uuid.UUID
    provider_entry_id: str | None
    source_url: str | None
    title: str | None
    published_at: datetime | None
    discovered_at: datetime
    status: str
    skip_reason: str | None
    error_message: str | None
    item_id: uuid.UUID | None
    job_id: uuid.UUID | None
    queued_at: datetime | None
    captured_at: datetime | None
    skipped_at: datetime | None
    failed_at: datetime | None
    metadata: dict[str, Any] = Field(default={}, validation_alias="metadata_")
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True, "populate_by_name": True}


class SourceSubscriptionEntryListResponse(BaseModel):
    entries: list[SourceSubscriptionEntryOut]
    total: int


class SourceSubscriptionSyncResponse(BaseModel):
    status: Literal["queued"]
    subscription_id: uuid.UUID


class SourceSubscriptionEntryRetryResponse(BaseModel):
    status: Literal["queued"]
    subscription_id: uuid.UUID
    entry_id: uuid.UUID
