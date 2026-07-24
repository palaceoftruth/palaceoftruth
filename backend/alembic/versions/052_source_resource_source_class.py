"""Persist watched-source class for bounded discovery policy selection.

Revision ID: 052_source_resource_source_class
Revises: 051_public_oauth_clients
"""

from alembic import op
import sqlalchemy as sa


revision = "052_source_resource_source_class"
down_revision = "051_public_oauth_clients"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_resources",
        sa.Column("source_class", sa.String(length=20), nullable=False, server_default="webpage"),
    )
    op.create_check_constraint(
        "ck_source_resources_source_class",
        "source_resources",
        "source_class IN ('webpage', 'feed', 'sitemap')",
    )
    op.alter_column("source_resources", "source_class", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_source_resources_source_class", "source_resources", type_="check")
    op.drop_column("source_resources", "source_class")
