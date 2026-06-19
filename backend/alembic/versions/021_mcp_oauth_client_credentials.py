"""Add MCP OAuth client credentials

Revision ID: 021
Revises: 020
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_clients", sa.Column("oauth_client_secret_hash", sa.Text(), nullable=True))
    op.add_column("mcp_clients", sa.Column("oauth_revoked_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "mcp_clients",
        sa.Column("oauth_token_ttl_seconds", sa.Integer(), nullable=False, server_default="3600"),
    )

    op.create_table(
        "mcp_oauth_access_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "client_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mcp_clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_mcp_oauth_access_tokens_token_hash", "mcp_oauth_access_tokens", ["token_hash"])
    op.create_index("ix_mcp_oauth_access_tokens_client_id", "mcp_oauth_access_tokens", ["client_id"])
    op.create_index("ix_mcp_oauth_access_tokens_tenant_expires", "mcp_oauth_access_tokens", ["tenant_id", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_mcp_oauth_access_tokens_tenant_expires", table_name="mcp_oauth_access_tokens")
    op.drop_index("ix_mcp_oauth_access_tokens_client_id", table_name="mcp_oauth_access_tokens")
    op.drop_constraint("uq_mcp_oauth_access_tokens_token_hash", "mcp_oauth_access_tokens", type_="unique")
    op.drop_table("mcp_oauth_access_tokens")
    op.drop_column("mcp_clients", "oauth_token_ttl_seconds")
    op.drop_column("mcp_clients", "oauth_revoked_at")
    op.drop_column("mcp_clients", "oauth_client_secret_hash")
