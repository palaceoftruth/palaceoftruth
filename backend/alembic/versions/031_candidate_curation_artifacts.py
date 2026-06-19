"""Add candidate curation artifact storage.

Revision ID: 031_candidate_curation_artifacts
Revises: 030_source_subscriptions
Create Date: 2026-05-21
"""

from alembic import op


revision = "031_candidate_curation_artifacts"
down_revision = "030_source_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE candidate_curation_artifacts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id text NOT NULL DEFAULT 'default',
            artifact_kind varchar(80) NOT NULL,
            target_runtime varchar(80) NOT NULL,
            target_surface text NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'draft',
            source_item_ids jsonb NOT NULL DEFAULT '[]',
            source_digests jsonb NOT NULL DEFAULT '{}',
            candidate_body text NOT NULL,
            privacy_review jsonb NOT NULL DEFAULT '{}',
            eval_summary jsonb NOT NULL DEFAULT '{}',
            approval jsonb NOT NULL DEFAULT '{}',
            metadata jsonb NOT NULL DEFAULT '{}',
            supersedes_artifact_id uuid REFERENCES candidate_curation_artifacts(id) ON DELETE SET NULL,
            superseded_by_artifact_id uuid REFERENCES candidate_curation_artifacts(id) ON DELETE SET NULL,
            deprecated_reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            approved_at timestamptz,
            deprecated_at timestamptz,
            CONSTRAINT ck_candidate_curation_artifact_kind
                CHECK (artifact_kind IN ('candidate_skill', 'candidate_routing_manifest', 'candidate_prompt_guardrail')),
            CONSTRAINT ck_candidate_curation_artifact_status
                CHECK (status IN ('draft', 'proposed', 'approved', 'rejected', 'deprecated', 'superseded')),
            CONSTRAINT ck_candidate_curation_superseded_lineage
                CHECK (status != 'superseded' OR superseded_by_artifact_id IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE candidate_curation_artifact_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id text NOT NULL DEFAULT 'default',
            artifact_id uuid NOT NULL REFERENCES candidate_curation_artifacts(id) ON DELETE CASCADE,
            event_type varchar(40) NOT NULL,
            previous_status varchar(20),
            next_status varchar(20) NOT NULL,
            previous_snapshot jsonb,
            next_snapshot jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_candidate_curation_artifacts_tenant_status
        ON candidate_curation_artifacts (tenant_id, status)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_candidate_curation_artifacts_tenant_target
        ON candidate_curation_artifacts (tenant_id, target_runtime, target_surface)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_candidate_curation_artifacts_lineage
        ON candidate_curation_artifacts (tenant_id, supersedes_artifact_id, superseded_by_artifact_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_candidate_curation_artifact_events_artifact_created
        ON candidate_curation_artifact_events (tenant_id, artifact_id, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candidate_curation_artifact_events")
    op.execute("DROP TABLE IF EXISTS candidate_curation_artifacts")
