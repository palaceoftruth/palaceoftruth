"""Add content_hash to items and duplicate_of to jobs

Revision ID: 006
Revises: 005
Create Date: 2026-03-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add content_hash column to items (nullable — pre-existing items keep NULL)
    op.add_column("items", sa.Column("content_hash", sa.String(64), nullable=True))

    # Partial unique index matching the source_url pattern from migration 005:
    # excludes NULLs and failed items so re-ingestion after failure is allowed.
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_content_hash_unique ON items (content_hash)
        WHERE content_hash IS NOT NULL AND status != 'failed'
        """
    )

    # Add duplicate_of to jobs — points to the existing item when a worker
    # detects a content-hash collision and marks the job as 'duplicate'.
    op.add_column(
        "jobs",
        sa.Column(
            "duplicate_of",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "duplicate_of")
    op.execute("DROP INDEX IF EXISTS idx_items_content_hash_unique")
    op.drop_column("items", "content_hash")
