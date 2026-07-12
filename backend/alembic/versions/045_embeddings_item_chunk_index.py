"""Add the default embedding item/chunk lookup index.

Revision ID: 045_embeddings_item_chunk_index
Revises: 044_embedding_profile_1536_hnsw
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "045_embeddings_item_chunk_index"
down_revision: str | None = "044_embedding_profile_1536_hnsw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text(
            "select index_state.indisvalid, index_state.indisready "
            "from pg_index index_state "
            "join pg_class index_class on index_class.oid = index_state.indexrelid "
            "join pg_namespace index_namespace on index_namespace.oid = index_class.relnamespace "
            "where index_namespace.nspname = current_schema() "
            "and index_class.relname = 'ix_embeddings_item_chunk'"
        )
    ).first()
    # Avoid blocking retrieval writes while PostgreSQL builds the index on the
    # populated embedding table. Recover an interrupted invalid concurrent build
    # instead of letting IF NOT EXISTS silently stamp an unusable index.
    with op.get_context().autocommit_block():
        if existing is not None and not (existing.indisvalid and existing.indisready):
            op.execute("DROP INDEX CONCURRENTLY ix_embeddings_item_chunk")
            existing = None
        if existing is None:
            op.execute(
                "CREATE INDEX CONCURRENTLY ix_embeddings_item_chunk "
                "ON embeddings (item_id, chunk_index)"
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_embeddings_item_chunk")
