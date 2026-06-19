"""Add webhook_url and signing_key to jobs table

Revision ID: 008
Revises: 007
Create Date: 2026-03-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("webhook_url", sa.Text, nullable=True))
    op.add_column("jobs", sa.Column("signing_key", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "signing_key")
    op.drop_column("jobs", "webhook_url")
