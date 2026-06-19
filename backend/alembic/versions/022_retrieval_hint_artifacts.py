"""Add retrieval hint artifacts

Revision ID: 022
Revises: 021
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column("retrieval_hint_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "retrieval_hint_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("room_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_item_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("hint_text", sa.Text(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint(
        "uq_retrieval_hint_artifacts_source_generation",
        "retrieval_hint_artifacts",
        ["tenant_id", "room_id", "source_item_id", "source_chunk_index", "generation"],
    )
    op.create_index(
        "ix_retrieval_hint_artifacts_tenant_room_generation",
        "retrieval_hint_artifacts",
        ["tenant_id", "room_id", "generation"],
    )
    op.create_index(
        "ix_retrieval_hint_artifacts_source_item",
        "retrieval_hint_artifacts",
        ["source_item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_hint_artifacts_source_item", table_name="retrieval_hint_artifacts")
    op.drop_index("ix_retrieval_hint_artifacts_tenant_room_generation", table_name="retrieval_hint_artifacts")
    op.drop_constraint(
        "uq_retrieval_hint_artifacts_source_generation",
        "retrieval_hint_artifacts",
        type_="unique",
    )
    op.drop_table("retrieval_hint_artifacts")
    op.drop_column("rooms", "retrieval_hint_generation")
