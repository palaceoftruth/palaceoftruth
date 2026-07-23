"""Model public OAuth clients as a distinct no-secret credential type."""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "051_public_oauth_clients"
down_revision: Union[str, None] = "050_refresh_rotation_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_clients", sa.Column("oauth_client_id", sa.Text(), nullable=True))
    op.add_column(
        "mcp_clients",
        sa.Column("token_endpoint_auth_method", sa.Text(), nullable=False, server_default="client_secret_basic"),
    )
    op.create_unique_constraint("uq_mcp_clients_oauth_client_id", "mcp_clients", ["oauth_client_id"])
    op.drop_constraint("ck_mcp_clients_client_type", "mcp_clients", type_="check")
    op.create_check_constraint(
        "ck_mcp_clients_client_type",
        "mcp_clients",
        "client_type IN ('service', 'confidential_web', 'public')",
    )
    op.create_check_constraint(
        "ck_mcp_clients_public_oauth_auth",
        "mcp_clients",
        "(client_type <> 'public') OR "
        "(oauth_client_id IS NOT NULL AND oauth_client_secret_hash IS NULL "
        "AND token_endpoint_auth_method = 'none')",
    )


def downgrade() -> None:
    # Downgrade must never silently delete public-client identities or turn
    # them into secret-bearing clients.  Operators must revoke/migrate those
    # clients explicitly before rolling this schema back.
    public_client = op.get_bind().execute(
        sa.text("SELECT 1 FROM mcp_clients WHERE client_type = 'public' LIMIT 1")
    ).first()
    if public_client is not None:
        raise RuntimeError("cannot downgrade while public OAuth clients exist; revoke or migrate them explicitly")
    op.drop_constraint("ck_mcp_clients_public_oauth_auth", "mcp_clients", type_="check")
    op.drop_constraint("ck_mcp_clients_client_type", "mcp_clients", type_="check")
    op.create_check_constraint(
        "ck_mcp_clients_client_type",
        "mcp_clients",
        "client_type IN ('service', 'confidential_web')",
    )
    op.drop_constraint("uq_mcp_clients_oauth_client_id", "mcp_clients", type_="unique")
    op.drop_column("mcp_clients", "token_endpoint_auth_method")
    op.drop_column("mcp_clients", "oauth_client_id")
