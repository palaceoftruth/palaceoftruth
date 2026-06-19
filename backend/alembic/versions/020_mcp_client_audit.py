"""Add MCP client registry and request audit log

Revision ID: 020
Revises: 019
Create Date: 2026-05-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("client_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("allowed_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "client_key", name="uq_mcp_clients_tenant_client_key"),
    )
    op.create_index("ix_mcp_clients_tenant_last_seen", "mcp_clients", ["tenant_id", "last_seen_at"])

    op.create_table(
        "mcp_request_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_key", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("operation", sa.String(length=120), nullable=False),
        sa.Column("required_scope", sa.String(length=40), nullable=True),
        sa.Column("params_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(length=120), nullable=True),
        sa.Column("app_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_mcp_request_audit_events_tenant_created_at",
        "mcp_request_audit_events",
        ["tenant_id", "created_at"],
    )
    op.create_index("ix_mcp_request_audit_events_client_id", "mcp_request_audit_events", ["client_id"])
    op.create_index("ix_mcp_request_audit_events_operation", "mcp_request_audit_events", ["operation"])


def downgrade() -> None:
    op.drop_index("ix_mcp_request_audit_events_operation", table_name="mcp_request_audit_events")
    op.drop_index("ix_mcp_request_audit_events_client_id", table_name="mcp_request_audit_events")
    op.drop_index("ix_mcp_request_audit_events_tenant_created_at", table_name="mcp_request_audit_events")
    op.drop_table("mcp_request_audit_events")
    op.drop_index("ix_mcp_clients_tenant_last_seen", table_name="mcp_clients")
    op.drop_table("mcp_clients")
