"""Add embedding profile metadata for side-by-side vectors.

Revision ID: 033_embedding_profile_metadata
Revises: 032_adaptive_room_tunnel_signals
Create Date: 2026-05-27
"""

from alembic import op


revision = "033_embedding_profile_metadata"
down_revision = "032_adaptive_room_tunnel_signals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE embedding_profile_vectors
            ADD COLUMN profile_kind text NOT NULL DEFAULT 'text',
            ADD COLUMN input_modality text NOT NULL DEFAULT 'text',
            ADD COLUMN profile_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE embedding_profile_vectors
            ADD CONSTRAINT ck_embedding_profile_vectors_profile_kind
                CHECK (profile_kind IN ('text', 'native_image', 'multilingual_text')),
            ADD CONSTRAINT ck_embedding_profile_vectors_input_modality
                CHECK (input_modality IN ('text', 'image', 'multilingual_text'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE embedding_profile_vectors
            DROP CONSTRAINT IF EXISTS ck_embedding_profile_vectors_input_modality,
            DROP CONSTRAINT IF EXISTS ck_embedding_profile_vectors_profile_kind,
            DROP COLUMN IF EXISTS profile_metadata,
            DROP COLUMN IF EXISTS input_modality,
            DROP COLUMN IF EXISTS profile_kind
        """
    )
