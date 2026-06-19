"""Add room closet artifact storage.

Revision ID: 017_room_closet_artifacts
Revises: 016_temporal_fact_registry
Create Date: 2026-04-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "017_room_closet_artifacts"
down_revision = "016_temporal_fact_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("closet_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "room_closet_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("drawer_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("tag_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "generation", name="uq_room_closet_artifacts_room_generation"),
    )
    op.create_index(
        "ix_room_closet_artifacts_tenant_generation",
        "room_closet_artifacts",
        ["tenant_id", "generation"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_room_closet_artifacts_tenant_generation", table_name="room_closet_artifacts")
    op.drop_table("room_closet_artifacts")
    op.drop_column("rooms", "closet_generation")
