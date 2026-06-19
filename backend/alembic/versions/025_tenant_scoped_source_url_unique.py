"""Scope source URL uniqueness by tenant

Revision ID: 025
Revises: 024
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The application deduplicates source URLs inside a tenant. Keep the database
    # constraint aligned so another tenant or old default-tenant row cannot block
    # re-ingestion for the current tenant.
    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique
        ON items (tenant_id, source_url)
        WHERE source_url IS NOT NULL AND status != 'failed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_items_source_url_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_source_url_unique
        ON items (source_url)
        WHERE source_url IS NOT NULL AND status != 'failed'
        """
    )
