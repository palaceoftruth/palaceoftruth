"""Add tenant-scoped delegated OAuth persistence foundations.

Authorization-code and refresh endpoints are intentionally not enabled by this
migration; it only provides the durable, tenant-qualified contracts they need.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "048_tenant_oauth_foundation"
down_revision: Union[str, None] = "047_mcp_agent_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_clients", sa.Column("client_type", sa.Text(), nullable=False, server_default="service"))
    op.add_column("mcp_clients", sa.Column("redirect_uris", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("mcp_clients", sa.Column("allowed_resources", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("mcp_clients", sa.Column("authorization_code_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint("ck_mcp_clients_client_type", "mcp_clients", "client_type IN ('service', 'confidential_web')")

    op.create_table(
        "mcp_oauth_authorization_interactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("agent_scope_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("workspace_scope_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("pkce_challenge", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_mcp_oauth_interactions_tenant_expires", "mcp_oauth_authorization_interactions", ["tenant_id", "expires_at"])
    op.create_table(
        "mcp_oauth_delegated_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("agent_scope_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("workspace_scope_keys", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("authorized_by", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_mcp_oauth_grants_tenant_client", "mcp_oauth_delegated_grants", ["tenant_id", "client_id"])
    op.create_table(
        "mcp_oauth_authorization_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_oauth_delegated_grants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("pkce_challenge", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("code_hash", name="uq_mcp_oauth_authorization_codes_code_hash"),
    )
    op.create_table(
        "mcp_oauth_refresh_token_families",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mcp_oauth_delegated_grants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("current_token_hash", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("current_token_hash", name="uq_mcp_oauth_refresh_families_token_hash"),
    )
    op.add_column("mcp_oauth_access_tokens", sa.Column("delegated_grant_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_mcp_oauth_access_tokens_delegated_grant", "mcp_oauth_access_tokens", "mcp_oauth_delegated_grants", ["delegated_grant_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_mcp_oauth_access_tokens_delegated_grant", "mcp_oauth_access_tokens", type_="foreignkey")
    op.drop_column("mcp_oauth_access_tokens", "delegated_grant_id")
    op.drop_table("mcp_oauth_refresh_token_families")
    op.drop_table("mcp_oauth_authorization_codes")
    op.drop_index("ix_mcp_oauth_grants_tenant_client", table_name="mcp_oauth_delegated_grants")
    op.drop_table("mcp_oauth_delegated_grants")
    op.drop_index("ix_mcp_oauth_interactions_tenant_expires", table_name="mcp_oauth_authorization_interactions")
    op.drop_table("mcp_oauth_authorization_interactions")
    op.drop_constraint("ck_mcp_clients_client_type", "mcp_clients", type_="check")
    op.drop_column("mcp_clients", "authorization_code_enabled")
    op.drop_column("mcp_clients", "allowed_resources")
    op.drop_column("mcp_clients", "redirect_uris")
    op.drop_column("mcp_clients", "client_type")
