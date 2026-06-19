import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import String, Text, ARRAY, func, Computed
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Item(Base):
    __tablename__ = "items"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_content: Mapped[str | None] = mapped_column(Text)
    content_chunks: Mapped[Any | None] = mapped_column(JSONB)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    status: Mapped[str] = mapped_column(String(20), server_default="processing")
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
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
