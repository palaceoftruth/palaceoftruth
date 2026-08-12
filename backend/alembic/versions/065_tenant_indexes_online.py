"""Build tenant isolation indexes without blocking application writes.

Revision ID: 065_tenant_indexes_online
Revises: 064_tenant_not_null_validation
"""

from alembic import op
import sqlalchemy as sa


revision = "065_tenant_indexes_online"
down_revision = "064_tenant_not_null_validation"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_items_tenant_id_id_unique", "items", ("tenant_id", "id"), True),
    ("ix_embeddings_tenant_item_chunk", "embeddings", ("tenant_id", "item_id", "chunk_index"), False),
    ("ix_embedding_profile_vectors_tenant_item", "embedding_profile_vectors", ("tenant_id", "item_id", "profile_name"), False),
    ("ix_item_relationships_tenant_source", "item_relationships", ("tenant_id", "source_item_id"), False),
    ("ix_item_relationships_tenant_target", "item_relationships", ("tenant_id", "target_item_id"), False),
)


def _index_state(name: str):
    return op.get_bind().execute(sa.text("""
        SELECT indisvalid, indisready FROM pg_index
        JOIN pg_class ON pg_class.oid = pg_index.indexrelid
        JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
        WHERE pg_namespace.nspname = current_schema() AND pg_class.relname = :name
    """), {"name": name}).first()


def upgrade() -> None:
    for name, table, columns, unique in INDEXES:
        existing = _index_state(name)
        with op.get_context().autocommit_block():
            if existing is not None and not (existing[0] and existing[1]):
                op.execute(f'DROP INDEX CONCURRENTLY "{name}"')
                existing = None
            if existing is None:
                op.create_index(
                    name, table, list(columns), unique=unique,
                    postgresql_concurrently=True,
                )


def downgrade() -> None:
    for name, _table, _columns, _unique in reversed(INDEXES):
        with op.get_context().autocommit_block():
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
