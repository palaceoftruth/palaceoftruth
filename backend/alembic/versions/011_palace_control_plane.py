"""Add Palace control plane tables

Revision ID: 011
Revises: 010
Create Date: 2026-04-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sync_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False, server_default="folder"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("scan_interval_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "root_path", name="uq_sync_sources_tenant_root"),
    )

    op.create_table(
        "sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("sync_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("triggered_by", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("files_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_changed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_sync_runs_tenant_started_at", "sync_runs", ["tenant_id", "started_at"])

    op.create_table(
        "sync_source_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("sync_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("modified_ns", sa.BigInteger(), nullable=True),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("last_seen_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "sync_source_id", "relative_path", name="uq_sync_source_files_relative_path"),
    )
    op.create_index("ix_sync_source_files_item_id", "sync_source_files", ["item_id"])

    op.create_table(
        "wings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="derived"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_wings_tenant_slug"),
    )

    op.create_table(
        "rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("wing_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("wings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("stable_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("redirect_room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lineage_parent_room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True),
        sa.Column("membership_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tunnel_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "stable_key", name="uq_rooms_tenant_stable_key"),
    )
    op.create_index("ix_rooms_tenant_wing_id", "rooms", ["tenant_id", "wing_id"])

    op.create_table(
        "room_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("representative_item_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("room_id", "generation", name="uq_room_snapshots_room_generation"),
    )
    op.create_index("ix_room_snapshots_tenant_generation", "room_snapshots", ["tenant_id", "generation"])

    op.create_table(
        "room_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("membership_kind", sa.String(length=20), nullable=False, server_default="primary"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="auto"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "room_id", "item_id", "source", name="uq_room_memberships_room_item_source"),
    )
    op.create_index("ix_room_memberships_tenant_item_id", "room_memberships", ["tenant_id", "item_id"])

    op.create_table(
        "room_tunnels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("source_room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tunnel_type", sa.String(length=30), nullable=False, server_default="shared-tag"),
        sa.Column("strength", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "source_room_id", "target_room_id", "tunnel_type", name="uq_room_tunnels_edge"),
    )
    op.create_index("ix_room_tunnels_tenant_source_room", "room_tunnels", ["tenant_id", "source_room_id"])

    op.create_table(
        "palace_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("triggered_by", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("scope", sa.String(length=20), nullable=False, server_default="tenant"),
        sa.Column("requested_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applied_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_sync_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_palace_runs_tenant_started_at", "palace_runs", ["tenant_id", "started_at"])

    op.create_table(
        "palace_tenant_state",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("dirty_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_generation", sa.Integer(), nullable=True),
        sa.Column("active_palace_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("palace_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "palace_dirty_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=30), nullable=False, server_default="ingest"),
        sa.Column("sync_source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sync_sources.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "item_id", name="uq_palace_dirty_items_tenant_item"),
    )
    op.create_index("ix_palace_dirty_items_tenant_generation", "palace_dirty_items", ["tenant_id", "generation"])

    op.create_table(
        "palace_room_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_palace_room_events_tenant_created_at", "palace_room_events", ["tenant_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_palace_room_events_tenant_created_at", table_name="palace_room_events")
    op.drop_table("palace_room_events")

    op.drop_index("ix_palace_dirty_items_tenant_generation", table_name="palace_dirty_items")
    op.drop_table("palace_dirty_items")

    op.drop_table("palace_tenant_state")

    op.drop_index("ix_palace_runs_tenant_started_at", table_name="palace_runs")
    op.drop_table("palace_runs")

    op.drop_index("ix_room_tunnels_tenant_source_room", table_name="room_tunnels")
    op.drop_table("room_tunnels")

    op.drop_index("ix_room_memberships_tenant_item_id", table_name="room_memberships")
    op.drop_table("room_memberships")

    op.drop_index("ix_room_snapshots_tenant_generation", table_name="room_snapshots")
    op.drop_table("room_snapshots")

    op.drop_index("ix_rooms_tenant_wing_id", table_name="rooms")
    op.drop_table("rooms")

    op.drop_table("wings")

    op.drop_index("ix_sync_source_files_item_id", table_name="sync_source_files")
    op.drop_table("sync_source_files")

    op.drop_index("ix_sync_runs_tenant_started_at", table_name="sync_runs")
    op.drop_table("sync_runs")

    op.drop_table("sync_sources")
