"""Finish stored-grant lifecycle and reject audience-less tokens.

Revision ID: 059_scope_authority_lifecycle
Revises: 058_browser_sessions
"""

from alembic import op
import sqlalchemy as sa


revision = "059_scope_authority_lifecycle"
down_revision = "058_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column("created_by", sa.Text(), nullable=False, server_default="legacy-import"),
    )
    # Repair deployments where the original migration 055 already backfilled
    # every key with admin. Legacy keys enter a bounded retirement window.
    op.execute(
        "UPDATE api_keys "
        "SET scopes = (scopes - 'admin') || '[\"audit:write\"]'::jsonb "
        "WHERE scopes ? 'admin'"
    )
    op.execute(
        "UPDATE api_keys SET expires_at = CURRENT_TIMESTAMP + INTERVAL '90 days' "
        "WHERE expires_at IS NULL"
    )
    op.alter_column(
        "api_keys",
        "expires_at",
        existing_type=sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP + INTERVAL '90 days'"),
    )

    # A missing resource used to match MCP requests. Revoke those tokens and
    # replace NULL with an explicit non-routable marker before enforcing the
    # stored-audience invariant.
    op.execute(
        """
        UPDATE mcp_oauth_access_tokens
        SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP),
            resource = 'urn:palace:revoked-legacy-audience'
        WHERE resource IS NULL
        """
    )
    op.alter_column(
        "mcp_oauth_access_tokens",
        "resource",
        existing_type=sa.Text(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "mcp_oauth_access_tokens",
        "resource",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.drop_column("api_keys", "created_by")
    op.drop_column("api_keys", "expires_at")
