"""RSS/Atom feed ingestion: feeds table + source_url unique partial index on items

Revision ID: 004
Revises: 003
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Historical upgrades must never discard tenant data automatically. Stop
    # with a repairable error and let an operator resolve duplicates explicitly.
    duplicate_count = op.get_bind().execute(
        sa.text("""
        SELECT COUNT(*)
        FROM (
            SELECT source_url
            FROM items
            WHERE source_url IS NOT NULL
            GROUP BY source_url
            HAVING COUNT(*) > 1
        ) duplicates
        """)
    ).scalar_one()
    if duplicate_count:
        raise RuntimeError(
            "Migration 004 found duplicate item source_url values; resolve them "
            "explicitly before retrying the migration"
        )

    # Step 1b: Partial unique index on items.source_url (non-NULL only)
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique ON items (source_url)
        WHERE source_url IS NOT NULL
        """
    )

    # Step 2: feeds table
    op.execute(
        """
        CREATE TABLE feeds (
            id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            url                  TEXT NOT NULL,
            name                 TEXT,
            auto_tags            TEXT[] NOT NULL DEFAULT '{}',
            poll_interval        INTEGER NOT NULL DEFAULT 3600,
            enabled              BOOLEAN NOT NULL DEFAULT TRUE,
            paused_reason        TEXT,
            etag                 TEXT,
            last_modified        TEXT,
            last_fetched_at      TIMESTAMPTZ,
            last_error           TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            feed_metadata        JSONB NOT NULL DEFAULT '{}',
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX idx_feeds_url ON feeds (url)")
    op.execute("CREATE INDEX idx_feeds_enabled_fetched ON feeds (enabled, last_fetched_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feeds")
    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
