"""Canon page, revision, and digest-cache models.

A canon page is an ``items`` row carrying ``source_type = 'canon'``; this table
holds only the editorial metadata that items does not model (slug, section,
room binding, digest priority, confirmation generation). The literals below are
the single source of truth for the section and status vocabularies and are
mirrored by migration ``072_tenant_canon_pages``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


CANON_SECTIONS = ("identity", "rules", "glossary", "runbook", "charter", "faq", "reference")
CANON_STATUSES = ("draft", "published", "archived")
CANON_SOURCE_TYPE = "canon"

# Literal SQL fragments so the constraint text cannot drift from the tuples.
_SECTION_CHECK = "section IN ('identity', 'rules', 'glossary', 'runbook', 'charter', 'faq', 'reference')"
_STATUS_CHECK = "status IN ('draft', 'published', 'archived')"


class CanonPage(Base):
    """Editorial metadata for one canon page, joined to its ``items`` row."""

    __tablename__ = "canon_pages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_canon_pages_tenant_slug"),
        # One published charter per room; drafts and archives stay free.
        Index(
            "uq_canon_pages_tenant_room_charter",
            "tenant_id",
            "room_id",
            unique=True,
            postgresql_where=text(
                "section = 'charter' AND status = 'published' AND room_id IS NOT NULL"
            ),
        ),
        Index(
            "ix_canon_pages_tenant_room_published",
            "tenant_id",
            "room_id",
            postgresql_where=text("status = 'published'"),
        ),
        Index("ix_canon_pages_tenant_status_priority", "tenant_id", "status", "priority"),
        CheckConstraint(_SECTION_CHECK, name="ck_canon_pages_section"),
        CheckConstraint(_STATUS_CHECK, name="ck_canon_pages_status"),
        CheckConstraint(
            "char_count >= 0 AND char_count <= 20000",
            name="ck_canon_pages_char_count",
        ),
        CheckConstraint(
            "parent_slug IS NULL OR parent_slug <> slug",
            name="ck_canon_pages_parent_not_self",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    parent_slug: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    pinned_in_digest: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="published")
    last_confirmed_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    last_confirmed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CanonPageRevision(Base):
    """Immutable body history; one row is appended on every publish."""

    __tablename__ = "canon_page_revisions"
    __table_args__ = (
        UniqueConstraint(
            "page_id",
            "revision",
            name="uq_canon_page_revisions_page_revision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    page_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canon_pages.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class CanonDigestCache(Base):
    """One cached digest per tenant, keyed by a fingerprint of page state."""

    __tablename__ = "canon_digest_cache"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    digest_text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    built_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
