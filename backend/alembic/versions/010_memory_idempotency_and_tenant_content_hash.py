"""Add memory idempotency key and tenant-scoped content hash uniqueness

Revision ID: 010
Revises: 009
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("idempotency_key", sa.String(length=64), nullable=True))

    op.execute("DROP INDEX IF EXISTS idx_items_content_hash_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_content_hash_tenant_unique
        ON items (tenant_id, content_hash)
        WHERE content_hash IS NOT NULL AND status != 'failed'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_idempotency_key_tenant_unique
        ON items (tenant_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_items_idempotency_key_tenant_unique")
    op.execute("DROP INDEX IF EXISTS idx_items_content_hash_tenant_unique")
    op.execute(
        """
        CREATE UNIQUE INDEX idx_items_content_hash_unique
        ON items (content_hash)
        WHERE content_hash IS NOT NULL AND status != 'failed'
        """
    )
    op.drop_column("items", "idempotency_key")
