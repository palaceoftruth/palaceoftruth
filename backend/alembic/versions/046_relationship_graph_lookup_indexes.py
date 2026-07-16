"""Add bounded relationship graph lookup indexes.

Revision ID: 046
Revises: 045_embeddings_item_chunk_index
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op
from typing import Sequence, Union


revision: str = "046"
down_revision: Union[str, None] = "045_embeddings_item_chunk_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEXES = (
    ("ix_item_relationships_source_confidence_target", "source_item_id", "target_item_id"),
    ("ix_item_relationships_target_confidence_source", "target_item_id", "source_item_id"),
)


def _index_state(index_name: str) -> tuple[bool, bool] | None:
    return op.get_bind().execute(
        sa.text(
            "select index_state.indisvalid, index_state.indisready "
            "from pg_index index_state "
            "join pg_class index_class on index_class.oid = index_state.indexrelid "
            "join pg_namespace index_namespace on index_namespace.oid = index_class.relnamespace "
            "where index_namespace.nspname = current_schema() "
            "and index_class.relname = :index_name"
        ),
        {"index_name": index_name},
    ).first()


def upgrade() -> None:
    # Build on the populated relationship table without blocking retrieval writes.
    # An interrupted concurrent build is invalid and must be removed explicitly.
    for index_name, seed_column, related_column in INDEXES:
        existing = _index_state(index_name)
        with op.get_context().autocommit_block():
            if existing is not None and not (existing[0] and existing[1]):
                op.execute(f"DROP INDEX CONCURRENTLY {index_name}")
                existing = None
            if existing is None:
                op.execute(
                    f"CREATE INDEX CONCURRENTLY {index_name} "
                    f"ON item_relationships ({seed_column}, confidence DESC, {related_column}, relationship)"
                )


def downgrade() -> None:
    for index_name, _seed_column, _related_column in reversed(INDEXES):
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")
