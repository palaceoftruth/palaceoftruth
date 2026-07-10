"""Add durable leases for bounded watched-source refresh dispatch.

Revision ID: 042_resource_refresh_leases
Revises: 041_source_resources
Create Date: 2026-07-10
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "042_resource_refresh_leases"
down_revision = "041_source_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_resources", sa.Column("refresh_lease_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "source_resources",
        sa.Column("refresh_lease_expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_source_resources_due_lease",
        "source_resources",
        ["status", "next_due_at", "refresh_lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_resources_due_lease", table_name="source_resources")
    op.drop_column("source_resources", "refresh_lease_expires_at")
    op.drop_column("source_resources", "refresh_lease_token")
