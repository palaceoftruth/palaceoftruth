"""Add nullable governance fields to items and claims.

Revision ID: 070_item_governance
Revises: 069_mcp_workspace_scope_reads
Create Date: 2026-08-29
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision = "070_item_governance"
down_revision = "069_mcp_workspace_scope_reads"
branch_labels = None
depends_on = None


# Values intentionally narrow so the API and search ranking share a single source
# of truth. Do not expand without adding a check constraint entry here so the
# schema and the Pydantic enum cannot drift apart.
_ITEM_RISK_CLASSES = ("low", "moderate", "high", "critical")
_ITEM_VERIFICATION_STATES = ("unverified", "verified", "stale", "rejected")
_CLAIM_RISK_CLASSES = ("low", "moderate", "high", "critical")
_CLAIM_VERIFICATION_STATES = ("unverified", "verified", "stale", "rejected")


def upgrade() -> None:
    # Items: accountable owner, reviewer, verification, expiry, risk class.
    op.add_column(
        "items",
        sa.Column("governance_owner_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("governance_reviewer_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column(
            "governance_verification_state",
            sa.String(length=20),
            nullable=True,
        ),
    )
    op.add_column(
        "items",
        sa.Column("governance_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("governance_verified_by_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column("governance_verification_deadline", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "items",
        sa.Column(
            "governance_risk_class",
            sa.String(length=20),
            nullable=True,
        ),
    )
    op.add_column(
        "items",
        sa.Column(
            "governance_supersession_reason",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "items",
        sa.Column(
            "governance_superseded_by_item_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "items",
        sa.Column(
            "governance_superseded_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
    )

    # Foreign key preserves the supersession chain inside the same table so a
    # tenant can still trace the prior item id after deletion.
    op.create_foreign_key(
        "fk_items_governance_superseded_by",
        "items",
        "items",
        ["governance_superseded_by_item_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_check_constraint(
        "ck_items_governance_verification_state",
        "items",
        "governance_verification_state IS NULL OR governance_verification_state IN ("
        + ", ".join(f"'{value}'" for value in _ITEM_VERIFICATION_STATES)
        + ")",
    )
    op.create_check_constraint(
        "ck_items_governance_risk_class",
        "items",
        "governance_risk_class IS NULL OR governance_risk_class IN ("
        + ", ".join(f"'{value}'" for value in _ITEM_RISK_CLASSES)
        + ")",
    )

    # Tenant-scoped index for expired-deadline scans; only the partial slice
    # that operators filter on benefits from this index, and the partial WHERE
    # keeps it tiny.
    op.create_index(
        "idx_items_governance_tenant_deadline",
        "items",
        ["tenant_id", "governance_verification_deadline"],
        postgresql_where=sa.text("governance_verification_deadline IS NOT NULL"),
    )
    op.create_index(
        "idx_items_governance_tenant_risk",
        "items",
        ["tenant_id", "governance_risk_class"],
        postgresql_where=sa.text("governance_risk_class IS NOT NULL"),
    )

    # Claims: mirror enough surface so a derived claim can carry the same
    # accountability as the raw item it was extracted from.
    op.add_column(
        "claims",
        sa.Column("governance_owner_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("governance_reviewer_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column(
            "governance_verification_state",
            sa.String(length=20),
            nullable=True,
        ),
    )
    op.add_column(
        "claims",
        sa.Column("governance_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("governance_verified_by_subject", sa.Text(), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("governance_verification_deadline", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("governance_risk_class", sa.String(length=20), nullable=True),
    )

    op.create_check_constraint(
        "ck_claims_governance_verification_state",
        "claims",
        "governance_verification_state IS NULL OR governance_verification_state IN ("
        + ", ".join(f"'{value}'" for value in _CLAIM_VERIFICATION_STATES)
        + ")",
    )
    op.create_check_constraint(
        "ck_claims_governance_risk_class",
        "claims",
        "governance_risk_class IS NULL OR governance_risk_class IN ("
        + ", ".join(f"'{value}'" for value in _CLAIM_RISK_CLASSES)
        + ")",
    )
    op.create_index(
        "idx_claims_governance_tenant_deadline",
        "claims",
        ["tenant_id", "governance_verification_deadline"],
        postgresql_where=sa.text("governance_verification_deadline IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_claims_governance_tenant_deadline", table_name="claims")
    op.drop_constraint("ck_claims_governance_risk_class", "claims", type_="check")
    op.drop_constraint("ck_claims_governance_verification_state", "claims", type_="check")
    op.drop_column("claims", "governance_risk_class")
    op.drop_column("claims", "governance_verification_deadline")
    op.drop_column("claims", "governance_verified_by_subject")
    op.drop_column("claims", "governance_verified_at")
    op.drop_column("claims", "governance_verification_state")
    op.drop_column("claims", "governance_reviewer_subject")
    op.drop_column("claims", "governance_owner_subject")

    op.drop_index("idx_items_governance_tenant_risk", table_name="items")
    op.drop_index("idx_items_governance_tenant_deadline", table_name="items")
    op.drop_constraint("ck_items_governance_risk_class", "items", type_="check")
    op.drop_constraint("ck_items_governance_verification_state", "items", type_="check")
    op.drop_constraint("fk_items_governance_superseded_by", "items", type_="foreignkey")
    op.drop_column("items", "governance_superseded_at")
    op.drop_column("items", "governance_superseded_by_item_id")
    op.drop_column("items", "governance_supersession_reason")
    op.drop_column("items", "governance_risk_class")
    op.drop_column("items", "governance_verification_deadline")
    op.drop_column("items", "governance_verified_by_subject")
    op.drop_column("items", "governance_verified_at")
    op.drop_column("items", "governance_verification_state")
    op.drop_column("items", "governance_reviewer_subject")
    op.drop_column("items", "governance_owner_subject")
