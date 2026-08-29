import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ITEM_TAGS = 50
MAX_TAG_LENGTH = 100


# Recommendation 1: accountably-owned knowledge. These mirrors of the DB
# check constraints keep the wire format and the migration schema in lockstep.
ItemGovernanceVerificationState = Literal["unverified", "verified", "stale", "rejected"]
ItemGovernanceRiskClass = Literal["low", "moderate", "high", "critical"]


# Wire keys the API exposes. The ORM attribute names live in
# ``backend/app/models/item.py`` and carry the ``governance_`` prefix; the
# helper below maps between the two so the validator, the API, and the SQL
# column all agree on which name means what.
_GOVERNANCE_ORM_KEYS = {
    "owner_subject": "governance_owner_subject",
    "reviewer_subject": "governance_reviewer_subject",
    "verification_state": "governance_verification_state",
    "verified_at": "governance_verified_at",
    "verified_by_subject": "governance_verified_by_subject",
    "verification_deadline": "governance_verification_deadline",
    "risk_class": "governance_risk_class",
    "supersession_reason": "governance_supersession_reason",
    "superseded_by_item_id": "governance_superseded_by_item_id",
    "superseded_at": "governance_superseded_at",
}


def _bounded_labels(values: list[str] | None, field_name: str) -> list[str] | None:
    if values is None:
        return None
    if len(values) > MAX_ITEM_TAGS:
        raise ValueError(f"{field_name} must contain at most {MAX_ITEM_TAGS} values")
    normalized = [value.strip() for value in values]
    if any(not value or len(value) > MAX_TAG_LENGTH for value in normalized):
        raise ValueError(f"{field_name} values must be 1 to {MAX_TAG_LENGTH} characters")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} values must be unique")
    return normalized


def _bounded_subject(values: str | None, field_name: str) -> str | None:
    """Subjects are stable, non-secret identifiers (api_keys.id, MCP client
    ids, or human operator handles). They must never be free-form prose; the
    API logs and audit events echo subjects back, so a 200-character limit
    keeps a malformed write from leaking huge blobs into the audit log.
    """
    if values is None:
        return None
    normalized = values.strip()
    if not normalized:
        return None
    if len(normalized) > 200:
        raise ValueError(f"{field_name} must be 1 to 200 characters")
    return normalized


class ItemGovernance(BaseModel):
    """Nullable per-item governance surface. All fields are optional; existing
    rows default to "unassigned / unverified" until an operator touches them."""

    owner_subject: str | None = None
    reviewer_subject: str | None = None
    verification_state: ItemGovernanceVerificationState | None = None
    verified_at: datetime | None = None
    verified_by_subject: str | None = None
    verification_deadline: datetime | None = None
    risk_class: ItemGovernanceRiskClass | None = None
    supersession_reason: str | None = None
    superseded_by_item_id: uuid.UUID | None = None
    superseded_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _accept_orm_attributes(cls, value: Any) -> Any:
        """Translate ``governance_*`` ORM attribute names into the wire-level
        keys so ``ItemResponse.model_validate(row)`` populates this block from
        a SQLAlchemy row without forcing every call site to build the dict by
        hand. Plain dicts and already-normalized governance payloads pass
        through unchanged."""
        if not isinstance(value, dict):
            return value
        # Bail out fast when the dict already uses wire-level keys.
        if any(key in value for key in _GOVERNANCE_ORM_KEYS):
            return value
        translated: dict[str, Any] = {}
        for wire_key, orm_key in _GOVERNANCE_ORM_KEYS.items():
            if orm_key in value:
                translated[wire_key] = value[orm_key]
        # Preserve any wire-level keys the caller supplied explicitly so the
        # combined input keeps its semantics.
        for key, item_value in value.items():
            translated.setdefault(key, item_value)
        return translated

    @field_validator(
        "owner_subject",
        "reviewer_subject",
        "verified_by_subject",
    )
    @classmethod
    def bounded_subject(cls, value: str | None, info) -> str | None:
        return _bounded_subject(value, info.field_name)

    @field_validator("supersession_reason")
    @classmethod
    def bounded_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > 1000:
            raise ValueError("supersession_reason must be 1 to 1000 characters")
        return normalized


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
    governance: ItemGovernance = Field(default_factory=ItemGovernance)

    model_config = {"from_attributes": True, "populate_by_name": True}

    @classmethod
    def from_orm_row(cls, row: Any) -> "ItemResponse":
        """Build an ``ItemResponse`` from a SQLAlchemy row, projecting the
        ``governance_*`` attributes into the wire-level ``governance`` block.

        ``model_validate(row)`` cannot do this on its own: Pydantic's
        attribute lookup only honors a field's ``validation_alias`` exactly,
        and the ``governance_`` prefix would otherwise have to be repeated on
        every nested model.
        """
        governance_payload = {
            wire_key: getattr(row, orm_key, None)
            for wire_key, orm_key in _GOVERNANCE_ORM_KEYS.items()
        }
        payload = {
            "id": getattr(row, "id"),
            "source_type": getattr(row, "source_type"),
            "source_url": getattr(row, "source_url", None),
            "title": getattr(row, "title"),
            "summary": getattr(row, "summary", None),
            "raw_content": getattr(row, "raw_content", None),
            "content_chunks": getattr(row, "content_chunks", None),
            "metadata": getattr(row, "metadata_", {}) or {},
            "tags": list(getattr(row, "tags", []) or []),
            "categories": list(getattr(row, "categories", []) or []),
            "status": getattr(row, "status"),
            "deleted_at": getattr(row, "deleted_at", None),
            "created_at": getattr(row, "created_at"),
            "updated_at": getattr(row, "updated_at"),
            "effective_date": getattr(row, "effective_date", None),
            "effective_date_source": getattr(row, "effective_date_source", None),
            "effective_date_quality": getattr(row, "effective_date_quality", None),
            "governance": governance_payload,
        }
        return cls.model_validate(payload)


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
    governance: ItemGovernance | None = None

    @field_validator("tags", "categories")
    @classmethod
    def bounded_labels(cls, values: list[str] | None, info):
        return _bounded_labels(values, info.field_name)


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

    @field_validator("tags")
    @classmethod
    def bounded_tags(cls, values: list[str]):
        return _bounded_labels(values, "tags") or []


class ItemCreateResponse(BaseModel):
    item_id: uuid.UUID
    status: str
    embedding_queued: bool


class BatchActionRequest(BaseModel):
    action: Literal["delete", "tag", "untag"]
    ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def bounded_tags(cls, values: list[str] | None):
        return _bounded_labels(values, "tags")


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
