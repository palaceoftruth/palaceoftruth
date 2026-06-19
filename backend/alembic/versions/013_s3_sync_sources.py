"""Add S3 sync source fields and source fingerprints

Revision ID: 013
Revises: 012
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sync_sources", sa.Column("bucket", sa.Text(), nullable=True))
    op.add_column("sync_sources", sa.Column("prefix", sa.Text(), nullable=True))
    op.add_column("sync_sources", sa.Column("endpoint_url", sa.Text(), nullable=True))
    op.add_column("sync_sources", sa.Column("region", sa.String(length=120), nullable=True))
    op.add_column(
        "sync_sources",
        sa.Column("force_path_style", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("sync_source_files", sa.Column("source_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("sync_source_files", "source_fingerprint")
    op.drop_column("sync_sources", "force_path_style")
    op.drop_column("sync_sources", "region")
    op.drop_column("sync_sources", "endpoint_url")
    op.drop_column("sync_sources", "prefix")
    op.drop_column("sync_sources", "bucket")
