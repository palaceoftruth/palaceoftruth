"""Add generated insight promotion states.

Revision ID: 034_generated_insight_promotion_states
Revises: 033_embedding_profile_metadata
Create Date: 2026-06-20
"""

from alembic import op


revision = "034_generated_insight_promotion_states"
down_revision = "033_embedding_profile_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_curation_artifacts
            DROP CONSTRAINT IF EXISTS ck_candidate_curation_artifact_status,
            ADD CONSTRAINT ck_candidate_curation_artifact_status
                CHECK (status IN (
                    'draft',
                    'needs_source',
                    'reviewable',
                    'promoted',
                    'proposed',
                    'approved',
                    'rejected',
                    'stale',
                    'deprecated',
                    'superseded'
                ))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_curation_artifacts
            DROP CONSTRAINT IF EXISTS ck_candidate_curation_artifact_status,
            ADD CONSTRAINT ck_candidate_curation_artifact_status
                CHECK (status IN (
                    'draft',
                    'proposed',
                    'approved',
                    'rejected',
                    'deprecated',
                    'superseded'
                ))
        """
    )
