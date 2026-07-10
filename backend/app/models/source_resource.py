"""Persistent identity and refresh state for externally addressable sources.

Schema/data flow contract: a tenant-scoped ``SourceResource`` owns one stable
canonical identity, observations are retained as provenance-bearing aliases,
every state transition appends an immutable audit snapshot, and successful
captures point at the existing append-only ``SourceRecord`` versions.  Fetching,
scheduling, backfills, and recrawls deliberately live outside this model.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SourceResource(Base):
    __tablename__ = "source_resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "kind",
            "canonical_identity",
            name="uq_source_resources_tenant_kind_identity",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_source_resources_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "current_source_record_id"],
            ["source_records.tenant_id", "source_records.id"],
            name="fk_source_resources_current_record_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "last_successful_source_record_id"],
            ["source_records.tenant_id", "source_records.id"],
            name="fk_source_resources_last_success_record_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("kind IN ('http')", name="ck_source_resources_kind"),
        CheckConstraint(
            "status IN ('active', 'unreachable', 'gone', 'paused')",
            name="ck_source_resources_status",
        ),
        CheckConstraint(
            "refresh_policy IN ('manual', 'interval', 'adaptive')",
            name="ck_source_resources_refresh_policy",
        ),
        CheckConstraint("refresh_slo_seconds > 0", name="ck_source_resources_refresh_slo"),
        CheckConstraint("consecutive_failures >= 0", name="ck_source_resources_failure_count"),
        Index("ix_source_resources_tenant_next_due", "tenant_id", "next_due_at"),
        Index("ix_source_resources_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, server_default="http")
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_identity: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_policy: Mapped[str] = mapped_column(String(20), nullable=False, server_default="interval")
    refresh_slo_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="86400")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")

    validator_etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    validator_last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    robots_allowed: Mapped[bool | None] = mapped_column(nullable=True)
    robots_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    robots_cached_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    content_changed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    backoff_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    current_source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    last_successful_source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    aliases: Mapped[list["SourceResourceAlias"]] = relationship(
        back_populates="resource", order_by="SourceResourceAlias.observed_at"
    )
    audit_snapshots: Mapped[list["SourceResourceAuditSnapshot"]] = relationship(
        back_populates="resource", order_by="SourceResourceAuditSnapshot.recorded_at"
    )


class SourceResourceAlias(Base):
    __tablename__ = "source_resource_aliases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["source_resources.tenant_id", "source_resources.id"],
            name="fk_source_resource_aliases_resource_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "resource_id", "signal", "normalized_url", name="uq_source_resource_alias_signal_url"
        ),
        CheckConstraint(
            "signal IN ('submitted', 'final', 'canonical')", name="ck_source_resource_alias_signal"
        ),
        CheckConstraint(
            "decision IN ('accepted', 'rejected', 'conflict')", name="ck_source_resource_alias_decision"
        ),
        Index("ix_source_resource_aliases_tenant_url", "tenant_id", "normalized_url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    submitted_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    canonical_signal_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    signal: Mapped[str] = mapped_column(String(20), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    resource: Mapped[SourceResource] = relationship(back_populates="aliases")


class SourceResourceAuditSnapshot(Base):
    __tablename__ = "source_resource_audit_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["source_resources.tenant_id", "source_resources.id"],
            name="fk_source_resource_audit_resource_tenant",
            ondelete="RESTRICT",
        ),
        Index("ix_source_resource_audit_resource_recorded", "resource_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    next_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    resource: Mapped[SourceResource] = relationship(back_populates="audit_snapshots")
