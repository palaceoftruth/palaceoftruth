"""Add item effective dates for temporal retrieval ranking

Revision ID: 027
Revises: 026
Create Date: 2026-05-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("items", sa.Column("effective_date", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("items", sa.Column("effective_date_source", sa.String(length=80), nullable=True))
    op.add_column("items", sa.Column("effective_date_quality", sa.String(length=20), nullable=True))
    op.create_index("idx_items_effective_date", "items", ["tenant_id", "effective_date"])


def downgrade() -> None:
    op.drop_index("idx_items_effective_date", table_name="items")
    op.drop_column("items", "effective_date_quality")
    op.drop_column("items", "effective_date_source")
    op.drop_column("items", "effective_date")
