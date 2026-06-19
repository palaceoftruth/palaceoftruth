"""Add repo sync source credential fields

Revision ID: 014
Revises: 013
Create Date: 2026-04-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sync_sources",
        sa.Column("credential_type", sa.String(length=30), nullable=False, server_default="none"),
    )
    op.add_column("sync_sources", sa.Column("credential_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_sources", "credential_ciphertext")
    op.drop_column("sync_sources", "credential_type")
