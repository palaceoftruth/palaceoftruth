"""Add multi-tenancy: api_keys table + tenant_id columns

Revision ID: 007
Revises: 006
Create Date: 2026-03-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TENANT_TABLES = [
    "items",
    "jobs",
    "feeds",
    "conversations",
    "conversation_messages",
]


def upgrade() -> None:
    # 1. api_keys table
    op.create_table(
        "api_keys",
        sa.Column("id", PG_UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text, nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("idx_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("idx_api_keys_key_hash",  "api_keys", ["key_hash"])

    # 2. Add nullable tenant_id to all data tables
    for table in _TENANT_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.Text, nullable=True))

    # 3. Backfill existing rows to tenant "default"
    for table in _TENANT_TABLES:
        op.execute(f"UPDATE {table} SET tenant_id = 'default' WHERE tenant_id IS NULL")

    # 4. Make tenant_id NOT NULL
    for table in _TENANT_TABLES:
        op.alter_column(table, "tenant_id", nullable=False)

    # 5. Indexes
    op.create_index("idx_items_tenant_id",                 "items",                 ["tenant_id"])
    op.create_index("idx_jobs_tenant_id",                  "jobs",                  ["tenant_id"])
    op.create_index("idx_feeds_tenant_id",                 "feeds",                 ["tenant_id"])
    op.create_index("idx_conversations_tenant_id",         "conversations",         ["tenant_id"])
    op.create_index("idx_conversation_messages_tenant_id", "conversation_messages", ["tenant_id"])
    op.create_index("idx_items_tenant_status",  "items", ["tenant_id", "status"])
    op.create_index("idx_jobs_tenant_status",   "jobs",  ["tenant_id", "status"])
    op.create_index("idx_feeds_tenant_enabled", "feeds", ["tenant_id", "enabled"])

    # 6. Drop old feeds.url unique index, replace with (url, tenant_id) unique constraint
    # Note: migration 004 created this as a bare index (idx_feeds_url), not a named constraint.
    op.drop_index("idx_feeds_url", table_name="feeds")
    op.create_unique_constraint("uq_feeds_url_tenant", "feeds", ["url", "tenant_id"])


def downgrade() -> None:
    op.drop_constraint("uq_feeds_url_tenant", "feeds", type_="unique")
    op.execute("CREATE UNIQUE INDEX idx_feeds_url ON feeds (url)")
    op.drop_index("idx_feeds_tenant_enabled",             table_name="feeds")
    op.drop_index("idx_jobs_tenant_status",               table_name="jobs")
    op.drop_index("idx_items_tenant_status",              table_name="items")
    op.drop_index("idx_conversation_messages_tenant_id",  table_name="conversation_messages")
    op.drop_index("idx_conversations_tenant_id",          table_name="conversations")
    op.drop_index("idx_feeds_tenant_id",                  table_name="feeds")
    op.drop_index("idx_jobs_tenant_id",                   table_name="jobs")
    op.drop_index("idx_items_tenant_id",                  table_name="items")
    for table in reversed(_TENANT_TABLES):
        op.drop_column(table, "tenant_id")
    op.drop_index("idx_api_keys_key_hash",  table_name="api_keys")
    op.drop_index("idx_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
