"""Wire schemas for the canon authoring API.

The caps and the vocabularies live in `services/canon.py` and `models/canon.py`;
this module only shapes what crosses the wire. Validation that would silently
mangle human-authored text is left to the service, which raises rather than cuts.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.canon import CANON_SECTIONS, CANON_STATUSES

CanonSection = Literal["identity", "rules", "glossary", "runbook", "charter", "faq", "reference"]
CanonStatus = Literal["draft", "published", "archived"]

# Fail loudly at import if the wire literals and the model tuples drift apart.
assert set(CanonSection.__args__) == set(CANON_SECTIONS)
assert set(CanonStatus.__args__) == set(CANON_STATUSES)


class CanonPageCreate(BaseModel):
    """Create a canon page. It starts as a draft and is not visible to agents."""

    slug: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=200)
    section: CanonSection
    body: str = Field(min_length=1, max_length=20000)
    room_id: uuid.UUID | None = None
    parent_slug: str | None = None
    priority: int = Field(default=100, ge=0, le=10000)
    pinned_in_digest: bool = False
    tags: list[str] = Field(default_factory=list)

    @field_validator("title", "body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CanonPageUpdate(BaseModel):
    """Edit a canon page. Omitted fields are left alone.

    A content change on a published page demotes it to draft, so an authoritative
    page never changes under an agent without a publish step.
    """

    title: str | None = Field(default=None, min_length=1)
    summary: str | None = Field(default=None, min_length=1, max_length=200)
    section: CanonSection | None = None
    body: str | None = Field(default=None, min_length=1, max_length=20000)
    room_id: uuid.UUID | None = None
    parent_slug: str | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    pinned_in_digest: bool | None = None
    change_note: str | None = Field(default=None, max_length=500)

    def cleared_fields(self) -> set[str]:
        """Fields the client explicitly sent as null, so the service can clear them."""
        return {name for name in self.model_fields_set if getattr(self, name) is None}


class CanonPageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    title: str
    summary: str
    section: str
    status: str
    room_id: uuid.UUID | None = None
    parent_slug: str | None = None
    priority: int
    pinned_in_digest: bool
    char_count: int
    # The body lives on the item, not the page row. It is returned on a detail
    # or write response and omitted from a list, where 20 KB per row would make
    # the editor's index slow for no benefit.
    body: str | None = None
    revision_count: int = 0
    created_by_subject: str | None = None
    updated_by_subject: str | None = None
    last_confirmed_generation: int = 0
    last_confirmed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CanonPageListResponse(BaseModel):
    pages: list[CanonPageOut]
    total: int
    limit: int
    offset: int


class CanonRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    revision: int
    summary: str
    change_note: str | None = None
    created_by_subject: str | None = None
    created_at: datetime | None = None
    body_md: str


class CanonRevisionListResponse(BaseModel):
    slug: str
    revisions: list[CanonRevisionOut]
    total: int


class CanonWriteResponse(BaseModel):
    """Every mutating endpoint returns the page plus what the write actually did."""

    page: CanonPageOut
    revision: int
    embedded: bool
