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


class CanonDigestOut(BaseModel):
    """The tenant digest, with the caps it was built under made visible.

    `next` is the cursor to resume from when `truncated` is true. A truncated
    digest without a working cursor would strand the caller, so the two travel
    together.
    """

    text: str
    char_count: int
    page_count: int
    truncated: bool
    next: str | None = None


class CanonPageSliceOut(BaseModel):
    """A window of one page body, capped by the canon service, never by a route."""

    slug: str
    section: str
    summary: str
    body: str
    total_char_count: int
    truncated: bool
    next: str | None = None


class CanonSearchHitOut(BaseModel):
    slug: str
    section: str
    summary: str
    snippet: str
    score: float
    truncated: bool


class CanonSearchResponse(BaseModel):
    query: str
    hits: list[CanonSearchHitOut]
    total: int


# --- Folder import ---------------------------------------------------------
#
# The preview and the commit are two phases of one operator decision. A preview
# says what each file would do and hands back a content-addressed `plan_id`; the
# commit must quote that id, so it cannot apply a folder the operator never saw.


class CanonImportFileOut(BaseModel):
    """What one uploaded file would do, as the preview shows it."""

    path: str
    slug: str
    title: str
    section: str
    # create | update | skip-unchanged | conflict. Plain `str`, like every other
    # canon output field: the vocabulary belongs to the service, and an output
    # schema that re-declared it would be a second owner of the same list.
    action: str
    content_hash: str
    existing_status: str | None = None
    reason: str


class CanonImportPlanOut(BaseModel):
    plan_id: str
    files: list[CanonImportFileOut]
    counts: dict[str, int]


class CanonImportCommitIn(BaseModel):
    """The operator's answer to a preview: which conflicts to apply, rename, or skip."""

    plan_id: str = Field(min_length=1)
    decisions: dict[str, str] = Field(default_factory=dict)
    publish: bool = False


class CanonImportResultOut(BaseModel):
    """What a commit did to one file: created | updated | skipped | renamed."""

    slug: str
    action: str
    new_slug: str | None = None


class CanonImportCommitOut(BaseModel):
    results: list[CanonImportResultOut]
    applied: int
    skipped: int


# --- Drift -----------------------------------------------------------------
#
# The wire shapes for the drift read and the detection run. `state` stays a plain
# `str`, like every other canon output field: the vocabulary belongs to
# `services/canon_drift.py`, and a schema that re-declared it would be a second
# owner of the same list.


class CanonDriftPageOut(BaseModel):
    """One page's drift verdict as the drift screen shows it."""

    slug: str
    title: str
    room_id: uuid.UUID | None = None
    room_slug: str
    state: str
    confirmed_generation: int
    current_generation: int
    new_memberships: int
    artifact_id: uuid.UUID | None = None
    reason: str


class CanonDriftListOut(BaseModel):
    pages: list[CanonDriftPageOut]
    counts: dict[str, int]


class CanonDriftDetectOut(BaseModel):
    """What one detection run did: how much it looked at and what it filed."""

    evaluated: int
    created: int
    deduplicated: int
    counts: dict[str, int]


class CanonSyncRunOut(BaseModel):
    """One sync run, as a person reads it: when, what it read, what it wrote."""

    id: uuid.UUID
    sync_source_id: uuid.UUID
    status: str
    triggered_by: str
    commit_sha: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    files_seen: int
    files_changed: int
    files_skipped: int
    pages_created: int
    pages_updated: int
    pages_archived: int
    pages_conflicted: int
    error_message: str | None = None


class CanonSyncFileOut(BaseModel):
    """One path in a run, and what the run did to it.

    ``action`` is what the run took, recorded at the time. It is deliberately not
    derived from the page's current state: a page edited by a person after a sync
    must not retroactively read as "created by the repo".
    """

    relative_path: str
    page_slug: str | None = None
    action: str | None = None
    status: str
    last_error: str | None = None


class CanonSyncRunDetailOut(CanonSyncRunOut):
    files: list[CanonSyncFileOut]


class CanonSyncRunListOut(BaseModel):
    runs: list[CanonSyncRunOut]
    limit: int

class CanonSyncBindingOut(BaseModel):
    """One canon binding, with the last run a person needs to judge it."""

    id: uuid.UUID
    name: str
    root_path: str
    source_kind: str
    status: str
    scan_interval_seconds: int
    canon_section_default: str
    canon_room_id: uuid.UUID | None = None
    canon_owns_pages: bool = True
    last_synced_at: datetime | None = None
    last_error: str | None = None
    latest_run: CanonSyncRunOut | None = None


class CanonConflictPairOut(BaseModel):
    """Both sides of a conflict, read live from the checkout and the page."""

    path: str
    slug: str
    commit_sha: str | None = None
    file_hash: str | None = None
    repo_text: str
    page_text: str
    page_status: str
    missing: bool = False
    truncated: bool = False


class CanonConflictAcceptIn(BaseModel):
    """A person choosing the repo's text for one conflicted page."""

    source_id: uuid.UUID
    path: str = Field(min_length=1, max_length=1024)



class CanonSyncWebhookOut(BaseModel):
    """A new push hook. The secret is returned here and nowhere else."""

    token: str
    secret: str
    path: str
    registered: bool
    hint: str


class CanonSyncWebhookHookOut(BaseModel):
    """What a delivery did. Never a stack trace: a hook is a machine caller."""

    status: Literal["queued", "coalesced", "ignored", "pong"]
    run_id: uuid.UUID | None = None
    reason: str | None = None
