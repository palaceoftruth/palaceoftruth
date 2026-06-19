"""Add explicit browser web saves.

Revision ID: 029_web_saves
Revises: 028_embedding_profile_vectors
Create Date: 2026-05-12
"""

from alembic import op


revision = "029_web_saves"
down_revision = "028_embedding_profile_vectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE web_saves (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id text NOT NULL,
            item_id uuid NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            original_url text NOT NULL,
            normalized_url text NOT NULL,
            source_title text,
            source_domain text,
            capture_kind varchar(40) NOT NULL,
            user_tags text[] NOT NULL DEFAULT '{}',
            saved_at timestamptz NOT NULL DEFAULT now(),
            archived_at timestamptz,
            extension_version text,
            metadata jsonb NOT NULL DEFAULT '{}'
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_web_saves_active_tenant_normalized_url
        ON web_saves (tenant_id, normalized_url)
        WHERE archived_at IS NULL
        """
    )
    op.execute("CREATE INDEX ix_web_saves_tenant_saved_at ON web_saves (tenant_id, saved_at)")
    op.execute("CREATE INDEX ix_web_saves_tenant_domain ON web_saves (tenant_id, source_domain)")
    op.execute("CREATE INDEX ix_web_saves_user_tags ON web_saves USING GIN (user_tags)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS web_saves")
