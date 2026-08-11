"""Add short-lived browser sessions so the SPA stops holding a tenant API key.

Revision ID: 058_browser_sessions
Revises: 057_mcp_containment_mode
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "058_browser_sessions"
down_revision = "057_mcp_containment_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        # Revoking or deleting the originating key must take its sessions with it.
        sa.Column("api_key_id", sa.UUID(), nullable=False),
        sa.Column("session_token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token_hash", sa.Text(), nullable=False),
        # The session's own grant. Never widened from the key at request time.
        sa.Column("scopes", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_token_hash", name="uq_browser_sessions_session_token_hash"),
    )
    # Expiry sweeps and per-key revocation are the two access patterns.
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"])
    op.create_index("ix_browser_sessions_api_key_id", "browser_sessions", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_api_key_id", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_expires_at", table_name="browser_sessions")
    op.drop_table("browser_sessions")
