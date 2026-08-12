"""Validate tenant not-null guards while normal writes continue.

Revision ID: 064_tenant_not_null_validation
Revises: 063_tenant_not_null_guards
"""

from alembic import op


revision = "064_tenant_not_null_validation"
down_revision = "063_tenant_not_null_guards"
branch_labels = None
depends_on = None


NOT_NULL_COLUMNS = {
    "items": ("metadata", "tags", "categories", "status", "created_at", "updated_at"),
    "jobs": ("status", "progress", "created_at"),
    "embeddings": ("created_at", "tenant_id"),
    "embedding_profile_vectors": ("created_at", "tenant_id"),
    "item_relationships": ("confidence", "created_at", "tenant_id"),
}


def upgrade() -> None:
    for table, columns in NOT_NULL_COLUMNS.items():
        for column in columns:
            name = f"ck_{table}_{column}_not_null_063"
            op.execute("SET LOCAL lock_timeout = '5s'")
            op.execute(f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"')


def downgrade() -> None:
    pass
