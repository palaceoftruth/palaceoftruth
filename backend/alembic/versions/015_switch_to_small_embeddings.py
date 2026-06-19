"""Switch embeddings to text-embedding-3-small dimensions.

Revision ID: 015_switch_to_small_embeddings
Revises: 014
Create Date: 2026-04-15
"""

from alembic import op


revision = "015_switch_to_small_embeddings"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_embeddings_halfvec_hnsw")
    op.execute(
        "ALTER TABLE embeddings "
        "ALTER COLUMN embedding TYPE vector(1536) "
        "USING subvector(embedding, 1, 1536)::vector(1536)"
    )
    op.execute(
        "ALTER TABLE embeddings "
        "ALTER COLUMN embedding_half TYPE halfvec(1536) "
        "USING subvector(embedding, 1, 1536)::halfvec(1536)"
    )
    op.execute(
        "CREATE INDEX idx_embeddings_halfvec_hnsw "
        "ON embeddings USING hnsw (embedding_half halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is intentionally unsupported; restoring 3072 dimensions requires a full re-embed."
    )
