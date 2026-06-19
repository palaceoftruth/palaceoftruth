"""Add soft delete fields for items and feeds

Revision ID: 023
Revises: 022
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("feeds", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_index(
        "ix_items_tenant_deleted_status",
        "items",
        ["tenant_id", "deleted_at", "status"],
    )
    op.create_index(
        "ix_feeds_tenant_deleted",
        "feeds",
        ["tenant_id", "deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_feeds_tenant_deleted", table_name="feeds")
    op.drop_index("ix_items_tenant_deleted_status", table_name="items")
    op.drop_column("feeds", "deleted_at")
    op.drop_column("items", "deleted_at")
