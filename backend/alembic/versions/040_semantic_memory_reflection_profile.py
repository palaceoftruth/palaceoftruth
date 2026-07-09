"""Add semantic memory reflection profile settings.

Revision ID: 040_semantic_memory_reflection
Revises: 039_memory_entries
Create Date: 2026-07-09
"""

from alembic import op


revision = "040_semantic_memory_reflection"
down_revision = "039_memory_entries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE memory_scope_profiles
            ADD COLUMN IF NOT EXISTS reflect_mission text NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS reflection_enabled boolean NOT NULL DEFAULT false
        """
    )
    op.execute(
        """
        ALTER TABLE candidate_curation_artifacts
            DROP CONSTRAINT IF EXISTS ck_candidate_curation_artifact_kind,
            ADD CONSTRAINT ck_candidate_curation_artifact_kind
                CHECK (artifact_kind IN (
                    'candidate_skill',
                    'candidate_routing_manifest',
                    'candidate_prompt_guardrail',
                    'candidate_memory_reflection'
                ))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE candidate_curation_artifacts
            DROP CONSTRAINT IF EXISTS ck_candidate_curation_artifact_kind,
            ADD CONSTRAINT ck_candidate_curation_artifact_kind
                CHECK (artifact_kind IN (
                    'candidate_skill',
                    'candidate_routing_manifest',
                    'candidate_prompt_guardrail'
                ))
        """
    )
    op.execute(
        """
        ALTER TABLE memory_scope_profiles
            DROP COLUMN IF EXISTS reflection_enabled,
            DROP COLUMN IF EXISTS reflect_mission
        """
    )
