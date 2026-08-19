"""Allow explicitly bound MCP clients to read sibling workspace scopes.

Revision ID: 069_mcp_workspace_scope_reads
Revises: 068_curation_principals
"""

from alembic import op
import sqlalchemy as sa


revision = "069_mcp_workspace_scope_reads"
down_revision = "068_curation_principals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_clients",
        sa.Column(
            "allow_workspace_scope_reads",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    # Mirrors ck_mcp_clients_tenant_shared_read_binding: delegated reach is only
    # meaningful for a client bound to a canonical agent scope, because that
    # binding is what the audit trail attributes the cross-scope read to.
    op.create_check_constraint(
        "ck_mcp_clients_workspace_scope_read_binding",
        "mcp_clients",
        "allow_workspace_scope_reads = false OR agent_scope_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_mcp_clients_workspace_scope_read_binding",
        "mcp_clients",
        type_="check",
    )
    op.drop_column("mcp_clients", "allow_workspace_scope_reads")
