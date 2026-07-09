"""Persist memory scope profiles

Revision ID: 038_memory_scope_profiles
Revises: 037_mcp_oauth_token_resource
Create Date: 2026-07-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "038_memory_scope_profiles"
down_revision: Union[str, None] = "037_mcp_oauth_token_resource"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memory_scope_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Text(), server_default="default", nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=True),
        sa.Column("retain_mission", sa.Text(), server_default="", nullable=False),
        sa.Column("quiet_recall", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('agent', 'workspace', 'session', 'tenant_shared')",
            name="ck_memory_scope_profiles_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'tenant_shared' AND scope_key IS NULL) "
            "OR (scope_type != 'tenant_shared' AND scope_key IS NOT NULL)",
            name="ck_memory_scope_profiles_scope_shape",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_memory_scope_profiles_tenant_scope
        ON memory_scope_profiles (tenant_id, scope_type, (coalesce(scope_key, '')))
        """
    )


def downgrade() -> None:
    op.drop_index("uq_memory_scope_profiles_tenant_scope", table_name="memory_scope_profiles")
    op.drop_table("memory_scope_profiles")
