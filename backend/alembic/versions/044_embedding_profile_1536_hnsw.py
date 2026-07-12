"""Add the missing 1536-dimensional side-profile HNSW index.

Revision ID: 044_embedding_profile_1536_hnsw
Revises: 043_job_attempts
"""

from collections.abc import Sequence

from alembic import op


revision: str = "044_embedding_profile_1536_hnsw"
down_revision: str | None = "043_job_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_embedding_profile_vectors_halfvec_1536_hnsw "
        "ON embedding_profile_vectors USING hnsw (embedding_half_1536 halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) WHERE dimensions = 1536"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_embedding_profile_vectors_halfvec_1536_hnsw")
