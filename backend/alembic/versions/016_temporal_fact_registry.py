"""Add temporal fact registry storage.

Revision ID: 016_temporal_fact_registry
Revises: 015_switch_to_small_embeddings
Create Date: 2026-04-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "016_temporal_fact_registry"
down_revision = "015_switch_to_small_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "temporal_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("source_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_key", sa.String(length=64), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("object_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("valid_from", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("valid_to", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("extracted_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("superseded_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_item_id"], ["items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "fact_key", name="uq_temporal_facts_tenant_fact_key"),
    )
    op.create_index("ix_temporal_facts_tenant_status", "temporal_facts", ["tenant_id", "status"], unique=False)
    op.create_index("ix_temporal_facts_tenant_source_item", "temporal_facts", ["tenant_id", "source_item_id"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("Downgrade is intentionally unsupported for temporal fact registry storage.")
