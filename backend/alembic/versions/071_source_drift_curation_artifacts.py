"""Add source drift provenance to candidate curation artifacts.

Revision ID: 071_source_drift_artifacts
Revises: 070_item_governance
Create Date: 2026-08-30
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "071_source_drift_artifacts"
down_revision = "070_item_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        "artifact_kind IN ('candidate_skill', 'candidate_routing_manifest', "
        "'candidate_prompt_guardrail', 'candidate_memory_reflection', 'candidate_source_drift')",
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("source_resource_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("previous_source_record_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("current_source_record_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("affected_item_ids", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("affected_claim_ids", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("evidence_diff", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "candidate_curation_artifacts",
        sa.Column("dedupe_key", sa.String(length=180), nullable=True),
    )
    op.create_foreign_key(
        "fk_candidate_curation_source_resource_tenant",
        "candidate_curation_artifacts",
        "source_resources",
        ["tenant_id", "source_resource_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_candidate_curation_previous_source_record_tenant",
        "candidate_curation_artifacts",
        "source_records",
        ["tenant_id", "previous_source_record_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_candidate_curation_current_source_record_tenant",
        "candidate_curation_artifacts",
        "source_records",
        ["tenant_id", "current_source_record_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_candidate_curation_tenant_dedupe",
        "candidate_curation_artifacts",
        ["tenant_id", "dedupe_key"],
    )
    op.create_index(
        "ix_candidate_curation_source_drift",
        "candidate_curation_artifacts",
        ["tenant_id", "source_resource_id", "created_at"],
    )


def downgrade() -> None:
    # Keep a downgrade recoverable after operators have reviewed drift rows.
    # The prior schema cannot represent the dedicated kind or columns, so copy
    # their values into metadata before restoring its existing generic kind.
    op.execute(
        """
        UPDATE candidate_curation_artifacts
        SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                'legacy_source_drift',
                jsonb_build_object(
                    'source_resource_id', source_resource_id,
                    'previous_source_record_id', previous_source_record_id,
                    'current_source_record_id', current_source_record_id,
                    'affected_item_ids', affected_item_ids,
                    'affected_claim_ids', affected_claim_ids,
                    'evidence_diff', evidence_diff,
                    'dedupe_key', dedupe_key
                )
            ),
            artifact_kind = 'candidate_memory_reflection'
        WHERE artifact_kind = 'candidate_source_drift'
        """
    )
    op.drop_index("ix_candidate_curation_source_drift", table_name="candidate_curation_artifacts")
    op.drop_constraint("uq_candidate_curation_tenant_dedupe", "candidate_curation_artifacts", type_="unique")
    for constraint_name in (
        "fk_candidate_curation_current_source_record_tenant",
        "fk_candidate_curation_previous_source_record_tenant",
        "fk_candidate_curation_source_resource_tenant",
        # Development databases may have applied the pre-review form of this
        # migration. IF EXISTS keeps its rollback recoverable as well.
        "fk_candidate_curation_current_source_record",
        "fk_candidate_curation_previous_source_record",
        "fk_candidate_curation_source_resource",
    ):
        op.execute(
            f"ALTER TABLE candidate_curation_artifacts DROP CONSTRAINT IF EXISTS {constraint_name}"
        )
    op.drop_column("candidate_curation_artifacts", "dedupe_key")
    op.drop_column("candidate_curation_artifacts", "evidence_diff")
    op.drop_column("candidate_curation_artifacts", "affected_claim_ids")
    op.drop_column("candidate_curation_artifacts", "affected_item_ids")
    op.drop_column("candidate_curation_artifacts", "current_source_record_id")
    op.drop_column("candidate_curation_artifacts", "previous_source_record_id")
    op.drop_column("candidate_curation_artifacts", "source_resource_id")
    op.drop_constraint("ck_candidate_curation_artifact_kind", "candidate_curation_artifacts", type_="check")
    op.create_check_constraint(
        "ck_candidate_curation_artifact_kind",
        "candidate_curation_artifacts",
        "artifact_kind IN ('candidate_skill', 'candidate_routing_manifest', "
        "'candidate_prompt_guardrail', 'candidate_memory_reflection')",
    )
