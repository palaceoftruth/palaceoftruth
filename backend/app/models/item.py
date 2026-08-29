import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, ARRAY, UniqueConstraint, func, Computed, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# Keep the enum surface narrow and shared between migration, API schema, and
# search ranking so the schema, the wire format, and the ranking adjustment
# cannot drift apart.
ITEM_GOVERNANCE_VERIFICATION_STATES = ("unverified", "verified", "stale", "rejected")
ITEM_GOVERNANCE_RISK_CLASSES = ("low", "moderate", "high", "critical")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_items_tenant_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_content: Mapped[str | None] = mapped_column(Text)
    content_chunks: Mapped[Any | None] = mapped_column(JSONB)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}", nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}", nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="processing", nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    effective_date: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    effective_date_source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    effective_date_quality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Generated tsvector for hybrid search (Phase 3); never written by app
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(raw_content, ''))",
            persisted=True,
        ),
    )

    # Recommendation 1: accountably-owned knowledge. Every column is nullable
    # so the existing corpus stays "unassigned / unverified" until a human
    # touches it; we never infer ownership.
    governance_owner_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    governance_reviewer_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    governance_verification_state: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    governance_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    governance_verified_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    governance_verification_deadline: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    governance_risk_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    governance_supersession_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    governance_superseded_by_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
        nullable=True,
    )
    governance_superseded_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
