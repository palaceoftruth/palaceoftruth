import uuid
from datetime import datetime

from sqlalchemy import ARRAY, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SourceSubscription(Base):
    __tablename__ = "source_subscriptions"
    __table_args__ = (
        Index("ix_source_subscriptions_tenant_status", "tenant_id", "status"),
        Index("ix_source_subscriptions_tenant_deleted", "tenant_id", "deleted_at"),
        Index(
            "uq_source_subscriptions_active_external",
            "tenant_id",
            "provider_type",
            "external_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    provider_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    auto_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3600")
    cursor: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    provider_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    last_checked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_discovered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    entries: Mapped[list["SourceSubscriptionEntry"]] = relationship(
        back_populates="subscription",
        cascade="all, delete-orphan",
        order_by="SourceSubscriptionEntry.discovered_at.desc()",
    )


class SourceSubscriptionEntry(Base):
    __tablename__ = "source_subscription_entries"
    __table_args__ = (
        Index("ix_source_subscription_entries_subscription_status", "subscription_id", "status"),
        Index("ix_source_subscription_entries_tenant_status", "tenant_id", "status"),
        Index("ix_source_subscription_entries_job_id", "job_id"),
        Index("ix_source_subscription_entries_item_id", "item_id"),
        Index(
            "uq_source_subscription_entries_provider_entry",
            "tenant_id",
            "subscription_id",
            "provider_entry_id",
            unique=True,
            postgresql_where=text("provider_entry_id IS NOT NULL"),
        ),
        Index(
            "uq_source_subscription_entries_source_url",
            "tenant_id",
            "subscription_id",
            "source_url",
            unique=True,
            postgresql_where=text("source_url IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_entry_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="discovered")
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    queued_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    skipped_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    subscription: Mapped[SourceSubscription] = relationship(back_populates="entries")
