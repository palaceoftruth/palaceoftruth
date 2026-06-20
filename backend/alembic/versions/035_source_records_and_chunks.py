"""Add source records and source chunks.

Revision ID: 035_source_records_chunks
Revises: 034_gen_insight_promo_states
Create Date: 2026-06-20
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "035_source_records_chunks"
down_revision = "034_gen_insight_promo_states"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "item_id", "source_version", name="uq_source_records_tenant_item_version"),
        sa.CheckConstraint(
            "status IN ('active', 'stale', 'failed', 'deleted', 'superseded')",
            name="ck_source_records_status",
        ),
    )
    op.create_index("ix_source_records_tenant_status_kind", "source_records", ["tenant_id", "status", "source_kind"])
    op.create_index(
        "ix_source_records_tenant_source_uri",
        "source_records",
        ["tenant_id", "source_uri"],
        postgresql_where=sa.text("source_uri IS NOT NULL"),
    )

    op.create_table(
        "source_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("chunk_digest", sa.Text(), nullable=False),
        sa.Column("span", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "source_record_id", "chunk_index", name="uq_source_chunks_tenant_record_index"),
        sa.UniqueConstraint("tenant_id", "source_record_id", "chunk_digest", name="uq_source_chunks_tenant_record_digest"),
    )
    op.create_index("ix_source_chunks_tenant_item_index", "source_chunks", ["tenant_id", "item_id", "chunk_index"])


def downgrade() -> None:
    op.drop_index("ix_source_chunks_tenant_item_index", table_name="source_chunks")
    op.drop_table("source_chunks")
    op.drop_index("ix_source_records_tenant_source_uri", table_name="source_records")
    op.drop_index("ix_source_records_tenant_status_kind", table_name="source_records")
    op.drop_table("source_records")
