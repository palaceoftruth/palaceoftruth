"""Bind Hermes OAuth clients to canonical agent scopes.

Revision ID: 047_mcp_agent_bindings
Revises: 046_relationship_graph_lookup_indexes
Create Date: 2026-07-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "047_mcp_agent_bindings"
down_revision: Union[str, None] = "046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_clients", sa.Column("agent_scope_key", sa.Text(), nullable=True))
    op.add_column(
        "mcp_clients",
        sa.Column("allow_all_agent_scope_reads", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        "ck_mcp_clients_agent_scope_read_binding",
        "mcp_clients",
        "allow_all_agent_scope_reads = false OR agent_scope_key IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_mcp_clients_agent_scope_read_binding", "mcp_clients", type_="check")
    op.drop_column("mcp_clients", "allow_all_agent_scope_reads")
    op.drop_column("mcp_clients", "agent_scope_key")
