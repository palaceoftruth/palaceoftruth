"""Add immutable browser-decision and callback bindings for OAuth codes.

The initial delegated-OAuth foundation intentionally stopped before issuing
codes.  A code exchange cannot safely validate the original redirect URI or a
CSRF-protected approval decision without these bindings.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "049_oauth_code_bindings"
down_revision: Union[str, None] = "048_tenant_oauth_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_oauth_authorization_interactions", sa.Column("state", sa.Text(), nullable=True))
    op.add_column("mcp_oauth_authorization_interactions", sa.Column("browser_session_hash", sa.Text(), nullable=True))
    op.add_column("mcp_oauth_authorization_interactions", sa.Column("csrf_token_hash", sa.Text(), nullable=True))
    op.add_column("mcp_oauth_authorization_interactions", sa.Column("decision", sa.Text(), nullable=True))
    op.add_column("mcp_oauth_authorization_interactions", sa.Column("authorized_by", sa.Text(), nullable=True))
    op.add_column("mcp_oauth_authorization_interactions", sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_mcp_oauth_interactions_decision",
        "mcp_oauth_authorization_interactions",
        "decision IS NULL OR decision IN ('approved', 'denied')",
    )
    # Nullable keeps this migration online-safe for any pre-release rows. New
    # authorization-code issuance and exchange fail closed when it is absent.
    op.add_column("mcp_oauth_authorization_codes", sa.Column("redirect_uri", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_oauth_authorization_codes", "redirect_uri")
    op.drop_constraint("ck_mcp_oauth_interactions_decision", "mcp_oauth_authorization_interactions", type_="check")
    op.drop_column("mcp_oauth_authorization_interactions", "decided_at")
    op.drop_column("mcp_oauth_authorization_interactions", "authorized_by")
    op.drop_column("mcp_oauth_authorization_interactions", "decision")
    op.drop_column("mcp_oauth_authorization_interactions", "csrf_token_hash")
    op.drop_column("mcp_oauth_authorization_interactions", "browser_session_hash")
    op.drop_column("mcp_oauth_authorization_interactions", "state")
