"""Phase 3: halfvec HNSW index + tsvector hybrid search

Revision ID: 002
Revises: 001
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- embeddings: halfvec column + HNSW index ---
    # halfvec(3072) stores the same dimensions as vector(3072) at 16-bit precision.
    # pgvector 0.7+ supports HNSW on halfvec up to 4000 dims (vs 2000 for vector).
    op.execute("ALTER TABLE embeddings ADD COLUMN embedding_half halfvec(3072)")
    op.execute("UPDATE embeddings SET embedding_half = embedding::halfvec(3072)")
    op.execute("ALTER TABLE embeddings ALTER COLUMN embedding_half SET NOT NULL")
    op.execute(
        "CREATE INDEX idx_embeddings_halfvec_hnsw "
        "ON embeddings USING hnsw (embedding_half halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # --- items: tsvector generated column + GIN index for hybrid search ---
    op.execute(
        "ALTER TABLE items ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS ("
        "  to_tsvector('english', coalesce(title, '') || ' ' || coalesce(raw_content, ''))"
        ") STORED"
    )
    op.execute(
        "CREATE INDEX idx_items_search_vector ON items USING GIN (search_vector)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_items_search_vector")
    op.execute("ALTER TABLE items DROP COLUMN IF EXISTS search_vector")
    op.execute("DROP INDEX IF EXISTS idx_embeddings_halfvec_hnsw")
    op.execute("ALTER TABLE embeddings DROP COLUMN IF EXISTS embedding_half")
