"""Exclude deleted items from deduplication indexes

Revision ID: 026
Revises: 025
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique
        ON items (tenant_id, source_url)
        WHERE source_url IS NOT NULL
          AND status NOT IN ('failed', 'deleted')
          AND deleted_at IS NULL
        """
    )

    op.execute("DROP INDEX IF EXISTS idx_items_content_hash_tenant_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_content_hash_tenant_unique
        ON items (tenant_id, content_hash)
        WHERE content_hash IS NOT NULL
          AND status NOT IN ('failed', 'deleted')
          AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_items_content_hash_tenant_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_content_hash_tenant_unique
        ON items (tenant_id, content_hash)
        WHERE content_hash IS NOT NULL AND status != 'failed'
        """
    )

    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique
        ON items (tenant_id, source_url)
        WHERE source_url IS NOT NULL AND status != 'failed'
        """
    )
