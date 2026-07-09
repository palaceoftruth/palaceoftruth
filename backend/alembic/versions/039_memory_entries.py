"""Add first-class memory entries.

Revision ID: 039_memory_entries
Revises: 038_memory_scope_profiles
Create Date: 2026-07-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "039_memory_entries"
down_revision: Union[str, None] = "038_memory_scope_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Text(), server_default="default", nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("created_by_role", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("valid_from", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("supersedes_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fact_kind", sa.String(length=20), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('agent', 'workspace', 'session', 'tenant_shared')",
            name="ck_memory_entries_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'tenant_shared' AND scope_key IS NULL) "
            "OR (scope_type != 'tenant_shared' AND scope_key IS NOT NULL)",
            name="ck_memory_entries_scope_shape",
        ),
        sa.CheckConstraint(
            "fact_kind IS NULL OR fact_kind IN ('world', 'experience', 'observation')",
            name="ck_memory_entries_fact_kind",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until >= valid_from",
            name="ck_memory_entries_valid_window",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_entry_id"], ["memory_entries.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_entry_id"], ["memory_entries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_memory_entries_tenant_item", "memory_entries", ["tenant_id", "item_id"], unique=True)
    op.create_index(
        "uq_memory_entries_tenant_idempotency",
        "memory_entries",
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_memory_entries_tenant_scope_created",
        "memory_entries",
        ["tenant_id", "scope_type", "scope_key", "created_at"],
    )
    op.create_index(
        "ix_memory_entries_tenant_valid_window",
        "memory_entries",
        ["tenant_id", "valid_from", "valid_until"],
    )
    op.create_index(
        "ix_memory_entries_tenant_supersession",
        "memory_entries",
        ["tenant_id", "supersedes_entry_id", "superseded_by_entry_id"],
    )

    op.execute(
        """
        INSERT INTO memory_entries (
            tenant_id,
            item_id,
            scope_type,
            scope_key,
            source,
            source_url,
            created_by_role,
            idempotency_key,
            valid_from,
            valid_until,
            fact_kind,
            metadata,
            created_at,
            updated_at
        )
        SELECT
            i.tenant_id,
            i.id,
            COALESCE(i.metadata->'memory_entry'->'scope'->>'type', 'tenant_shared') AS scope_type,
            CASE
                WHEN COALESCE(i.metadata->'memory_entry'->'scope'->>'type', 'tenant_shared') = 'tenant_shared'
                    THEN NULL
                ELSE NULLIF(i.metadata->'memory_entry'->'scope'->>'key', '')
            END AS scope_key,
            NULLIF(i.metadata->'memory_entry'->>'source', '') AS source,
            COALESCE(NULLIF(i.metadata->'memory_entry'->>'source_url', ''), i.source_url) AS source_url,
            NULLIF(i.metadata->'memory_entry'->>'created_by_role', '') AS created_by_role,
            COALESCE(NULLIF(i.metadata->'memory_entry'->>'idempotency_key', ''), i.idempotency_key) AS idempotency_key,
            CASE
                WHEN i.metadata->'memory_entry'->>'valid_from' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                    THEN (i.metadata->'memory_entry'->>'valid_from')::timestamptz
                ELSE NULL
            END AS valid_from,
            CASE
                WHEN i.metadata->'memory_entry'->>'valid_until' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                    THEN (i.metadata->'memory_entry'->>'valid_until')::timestamptz
                ELSE NULL
            END AS valid_until,
            CASE
                WHEN i.metadata->'memory_entry'->>'fact_kind' IN ('world', 'experience', 'observation')
                    THEN i.metadata->'memory_entry'->>'fact_kind'
                ELSE NULL
            END AS fact_kind,
            COALESCE(i.metadata->'memory_entry'->'metadata', '{}'::jsonb) AS metadata,
            COALESCE(i.created_at, now()) AS created_at,
            COALESCE(i.updated_at, i.created_at, now()) AS updated_at
        FROM items i
        WHERE i.metadata->'memory_entry' IS NOT NULL
          AND COALESCE(i.metadata->'memory_entry'->'scope'->>'type', 'tenant_shared')
              IN ('agent', 'workspace', 'session', 'tenant_shared')
          AND (
              COALESCE(i.metadata->'memory_entry'->'scope'->>'type', 'tenant_shared') = 'tenant_shared'
              OR NULLIF(i.metadata->'memory_entry'->'scope'->>'key', '') IS NOT NULL
          )
        ON CONFLICT (tenant_id, item_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_memory_entries_tenant_supersession", table_name="memory_entries")
    op.drop_index("ix_memory_entries_tenant_valid_window", table_name="memory_entries")
    op.drop_index("ix_memory_entries_tenant_scope_created", table_name="memory_entries")
    op.drop_index("uq_memory_entries_tenant_idempotency", table_name="memory_entries")
    op.drop_index("uq_memory_entries_tenant_item", table_name="memory_entries")
    op.drop_table("memory_entries")
