"""Add tenant API key audit and usage metadata

Revision ID: 018
Revises: 017_room_closet_artifacts
Create Date: 2026-04-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "018"
down_revision: Union[str, None] = "017_room_closet_artifacts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "api_key_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("actor_type", sa.String(length=40), nullable=False, server_default="admin"),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_api_key_audit_events_tenant_created_at", "api_key_audit_events", ["tenant_id", "created_at"])
    op.create_index("ix_api_key_audit_events_api_key_id", "api_key_audit_events", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_api_key_audit_events_api_key_id", table_name="api_key_audit_events")
    op.drop_index("ix_api_key_audit_events_tenant_created_at", table_name="api_key_audit_events")
    op.drop_table("api_key_audit_events")
    op.drop_column("api_keys", "last_used_at")
