"""Add claims and claim sources.

Revision ID: 036_claims_claim_sources
Revises: 035_source_records_chunks
Create Date: 2026-06-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "036_claims_claim_sources"
down_revision = "035_source_records_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("claim_key", sa.Text(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.String(length=30), nullable=False, server_default="fact"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("superseded_by_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["superseded_by_claim_id"], ["claims.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("tenant_id", "claim_key", name="uq_claims_tenant_claim_key"),
        sa.CheckConstraint(
            "claim_type IN ('fact', 'preference', 'decision', 'task_state', 'summary', 'classification', 'relationship')",
            name="ck_claims_claim_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'stale', 'conflicted', 'rejected', 'superseded')",
            name="ck_claims_status",
        ),
    )
    op.create_index("ix_claims_tenant_status_type", "claims", ["tenant_id", "status", "claim_type"])

    op.create_table(
        "claim_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("support_role", sa.String(length=20), nullable=False, server_default="supports"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="current"),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column("source_span", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_chunk_id"], ["source_chunks.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "tenant_id",
            "claim_id",
            "source_record_id",
            "source_digest",
            "support_role",
            name="uq_claim_sources_support",
        ),
        sa.CheckConstraint(
            "support_role IN ('supports', 'contradicts', 'context', 'derived_from')",
            name="ck_claim_sources_support_role",
        ),
        sa.CheckConstraint(
            "status IN ('current', 'stale')",
            name="ck_claim_sources_status",
        ),
    )
    op.create_index("ix_claim_sources_tenant_claim_role", "claim_sources", ["tenant_id", "claim_id", "support_role", "status"])
    op.create_index("ix_claim_sources_tenant_source_record", "claim_sources", ["tenant_id", "source_record_id"])


def downgrade() -> None:
    op.drop_index("ix_claim_sources_tenant_source_record", table_name="claim_sources")
    op.drop_index("ix_claim_sources_tenant_claim_role", table_name="claim_sources")
    op.drop_table("claim_sources")
    op.drop_index("ix_claims_tenant_status_type", table_name="claims")
    op.drop_table("claims")
