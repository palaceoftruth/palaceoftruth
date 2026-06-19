"""Add sync source disable audit metadata

Revision ID: 019
Revises: 018
Create Date: 2026-04-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_sources", sa.Column("disabled_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column("sync_sources", sa.Column("disabled_by", sa.Text(), nullable=True))
    op.add_column("sync_sources", sa.Column("disabled_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_sources", "disabled_reason")
    op.drop_column("sync_sources", "disabled_by")
    op.drop_column("sync_sources", "disabled_at")
