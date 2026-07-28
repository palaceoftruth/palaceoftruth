"""Allow explicitly bound MCP clients to read tenant-shared memory.

Revision ID: 053_mcp_tenant_shared_reads
Revises: 052_source_resource_source_class
"""

from alembic import op
import sqlalchemy as sa


revision = "053_mcp_tenant_shared_reads"
down_revision = "052_source_resource_source_class"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_clients",
        sa.Column(
            "allow_tenant_shared_reads",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_mcp_clients_tenant_shared_read_binding",
        "mcp_clients",
        "allow_tenant_shared_reads = false OR agent_scope_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_mcp_clients_tenant_shared_read_binding",
        "mcp_clients",
        type_="check",
    )
    op.drop_column("mcp_clients", "allow_tenant_shared_reads")
