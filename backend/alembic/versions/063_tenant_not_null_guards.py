"""Add unvalidated tenant not-null guards using short metadata locks.

Revision ID: 063_tenant_not_null_guards
Revises: 062_tenant_backfill
"""

from alembic import op


revision = "063_tenant_not_null_guards"
down_revision = "062_tenant_backfill"
branch_labels = None
depends_on = None


NOT_NULL_COLUMNS = {
    "items": ("metadata", "tags", "categories", "status", "created_at", "updated_at"),
    "jobs": ("status", "progress", "created_at"),
    "embeddings": ("created_at", "tenant_id"),
    "embedding_profile_vectors": ("created_at", "tenant_id"),
    "item_relationships": ("confidence", "created_at", "tenant_id"),
}


def guard_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_not_null_063"


def upgrade() -> None:
    for table, columns in NOT_NULL_COLUMNS.items():
        for column in columns:
            name = guard_name(table, column)
            op.execute("SET LOCAL lock_timeout = '5s'")
            op.execute(
                f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
                f'CHECK ("{column}" IS NOT NULL) NOT VALID'
            )


def downgrade() -> None:
    for table, columns in reversed(tuple(NOT_NULL_COLUMNS.items())):
        for column in reversed(columns):
            op.execute("SET LOCAL lock_timeout = '5s'")
            op.execute(
                f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS '
                f'"{guard_name(table, column)}"'
            )
