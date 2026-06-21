import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PalaceTenantState(Base):
    __tablename__ = "palace_tenant_state"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    dirty_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    indexed_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    active_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_palace_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("palace_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class TemporalFact(Base):
    __tablename__ = "temporal_facts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fact_key", name="uq_temporal_facts_tenant_fact_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    fact_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, server_default="1.0")
    valid_from: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    metadata_json: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    extracted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    superseded_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class SyncSource(Base):
    __tablename__ = "sync_sources"
    __table_args__ = (UniqueConstraint("tenant_id", "root_path", name="uq_sync_sources_tenant_root"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), server_default="folder")
    credential_type: Mapped[str] = mapped_column(String(30), server_default="none")
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    prefix: Mapped[str | None] = mapped_column(Text, nullable=True)
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    force_path_style: Mapped[bool] = mapped_column(Boolean, server_default="false")
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    disabled_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_interval_seconds: Mapped[int] = mapped_column(Integer, server_default="900")
    allowed_extensions: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    sync_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    triggered_by: Mapped[str] = mapped_column(String(20), server_default="manual")
    files_seen: Mapped[int] = mapped_column(Integer, server_default="0")
    files_changed: Mapped[int] = mapped_column(Integer, server_default="0")
    files_skipped: Mapped[int] = mapped_column(Integer, server_default="0")
    items_created: Mapped[int] = mapped_column(Integer, server_default="0")
    items_updated: Mapped[int] = mapped_column(Integer, server_default="0")
    items_failed: Mapped[int] = mapped_column(Integer, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class SyncSourceFile(Base):
    __tablename__ = "sync_source_files"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "sync_source_id",
            "relative_path",
            name="uq_sync_source_files_relative_path",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    sync_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), server_default="active")
    last_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "item_id", "source_version", name="uq_source_records_tenant_item_version"),
        CheckConstraint(
            "status IN ('active', 'stale', 'failed', 'deleted', 'superseded')",
            name="ck_source_records_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_version: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SourceChunk(Base):
    __tablename__ = "source_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_record_id", "chunk_index", name="uq_source_chunks_tenant_record_index"),
        UniqueConstraint("tenant_id", "source_record_id", "chunk_digest", name="uq_source_chunks_tenant_record_digest"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_digest: Mapped[str] = mapped_column(Text, nullable=False)
    span: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        UniqueConstraint("tenant_id", "claim_key", name="uq_claims_tenant_claim_key"),
        CheckConstraint(
            "claim_type IN ('fact', 'preference', 'decision', 'task_state', 'summary', 'classification', 'relationship')",
            name="ck_claims_claim_type",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'stale', 'conflicted', 'rejected', 'superseded')",
            name="ck_claims_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_key: Mapped[str] = mapped_column(Text, nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default="fact")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="active")
    superseded_by_claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="SET NULL"),
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ClaimSource(Base):
    __tablename__ = "claim_sources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "claim_id",
            "source_record_id",
            "source_digest",
            "support_role",
            name="uq_claim_sources_support",
        ),
        CheckConstraint(
            "support_role IN ('supports', 'contradicts', 'context', 'derived_from')",
            name="ck_claim_sources_support_role",
        ),
        CheckConstraint(
            "status IN ('current', 'stale')",
            name="ck_claim_sources_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_record_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_chunks.id", ondelete="SET NULL"),
        nullable=True,
    )
    support_role: Mapped[str] = mapped_column(String(20), nullable=False, server_default="supports")
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="current")
    source_digest: Mapped[str] = mapped_column(Text, nullable=False)
    source_span: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )


class Wing(Base):
    __tablename__ = "wings"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_wings_tenant_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(20), server_default="derived")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class Room(Base):
    __tablename__ = "rooms"
    __table_args__ = (UniqueConstraint("tenant_id", "stable_key", name="uq_rooms_tenant_stable_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    wing_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("wings.id", ondelete="CASCADE"),
        nullable=False,
    )
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    stable_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(20), server_default="active")
    redirect_room_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    lineage_parent_room_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    membership_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    closet_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    snapshot_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    tunnel_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    retrieval_hint_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class RoomSnapshot(Base):
    __tablename__ = "room_snapshots"
    __table_args__ = (UniqueConstraint("room_id", "generation", name="uq_room_snapshots_room_generation"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, server_default="0")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    representative_item_ids: Mapped[list[str]] = mapped_column(JSONB, server_default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )


class RoomClosetArtifact(Base):
    __tablename__ = "room_closet_artifacts"
    __table_args__ = (UniqueConstraint("room_id", "generation", name="uq_room_closet_artifacts_room_generation"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    room_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, server_default="0")
    drawer_refs: Mapped[list[dict[str, object]]] = mapped_column(JSONB, server_default="[]")
    tag_profile: Mapped[dict[str, int]] = mapped_column(JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )


class RetrievalHintArtifact(Base):
    __tablename__ = "retrieval_hint_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "room_id",
            "source_item_id",
            "source_chunk_index",
            "generation",
            name="uq_retrieval_hint_artifacts_source_generation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    room_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    hint_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class RoomMembership(Base):
    __tablename__ = "room_memberships"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "room_id",
            "item_id",
            "source",
            name="uq_room_memberships_room_item_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    room_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    membership_kind: Mapped[str] = mapped_column(String(20), server_default="primary")
    source: Mapped[str] = mapped_column(String(20), server_default="auto")
    confidence: Mapped[float] = mapped_column(Float, server_default="1.0")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class RoomTunnel(Base):
    __tablename__ = "room_tunnels"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_room_id",
            "target_room_id",
            "tunnel_type",
            name="uq_room_tunnels_edge",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    source_room_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_room_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    tunnel_type: Mapped[str] = mapped_column(String(30), server_default="shared-tag")
    strength: Mapped[float] = mapped_column(Float, server_default="0.0")
    activation_count: Mapped[int] = mapped_column(Integer, server_default="0")
    stability: Mapped[float] = mapped_column(Float, server_default="1.0")
    last_activated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class PalaceRun(Base):
    __tablename__ = "palace_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    status: Mapped[str] = mapped_column(String(20), server_default="queued")
    triggered_by: Mapped[str] = mapped_column(String(20), server_default="manual")
    scope: Mapped[str] = mapped_column(String(20), server_default="tenant")
    requested_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    applied_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    source_sync_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class PalaceDirtyItem(Base):
    __tablename__ = "palace_dirty_items"
    __table_args__ = (UniqueConstraint("tenant_id", "item_id", name="uq_palace_dirty_items_tenant_item"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(30), server_default="ingest")
    sync_source_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )


class PalaceRoomEvent(Base):
    __tablename__ = "palace_room_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    room_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("rooms.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )


class CandidateCurationArtifact(Base):
    __tablename__ = "candidate_curation_artifacts"
    __table_args__ = (
        CheckConstraint(
            "artifact_kind IN ('candidate_skill', 'candidate_routing_manifest', 'candidate_prompt_guardrail')",
            name="ck_candidate_curation_artifact_kind",
        ),
        CheckConstraint(
            "status IN ('draft', 'needs_source', 'reviewable', 'promoted', 'proposed', 'approved', 'rejected', 'stale', 'deprecated', 'superseded')",
            name="ck_candidate_curation_artifact_status",
        ),
        CheckConstraint(
            "status != 'superseded' OR superseded_by_artifact_id IS NOT NULL",
            name="ck_candidate_curation_superseded_lineage",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    artifact_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    target_runtime: Mapped[str] = mapped_column(String(80), nullable=False)
    target_surface: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="draft")
    source_item_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    source_digests: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, server_default="{}")
    candidate_body: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_review: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    eval_summary: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    approval: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    supersedes_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("candidate_curation_artifacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded_by_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("candidate_curation_artifacts.id", ondelete="SET NULL"),
        nullable=True,
    )
    deprecated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    deprecated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class CandidateCurationArtifactEvent(Base):
    __tablename__ = "candidate_curation_artifact_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("candidate_curation_artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    next_status: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    next_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
    )
