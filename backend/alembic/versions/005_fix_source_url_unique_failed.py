"""Fix source_url unique index to allow re-ingestion of failed items

Revision ID: 005
Revises: 004
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Replace the broad unique index with one that excludes failed items.
    # This lets users re-ingest a URL that previously failed without having
    # to manually delete the failed record first.
    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique ON items (source_url)
        WHERE source_url IS NOT NULL AND status != 'failed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique ON items (source_url)
        WHERE source_url IS NOT NULL
        """
    )
