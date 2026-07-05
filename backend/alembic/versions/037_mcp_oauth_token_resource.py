"""Persist MCP OAuth token resource

Revision ID: 037_mcp_oauth_token_resource
Revises: 036_claims_claim_sources
Create Date: 2026-07-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "037_mcp_oauth_token_resource"
down_revision: Union[str, None] = "036_claims_claim_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("mcp_oauth_access_tokens", sa.Column("resource", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_oauth_access_tokens", "resource")
