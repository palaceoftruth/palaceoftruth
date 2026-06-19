"""Add side-by-side embedding profile vector storage.

Revision ID: 028_embedding_profile_vectors
Revises: 027
Create Date: 2026-05-12
"""

from alembic import op


revision = "028_embedding_profile_vectors"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE embeddings
            ADD COLUMN profile_name text NOT NULL DEFAULT 'openai-text-embedding-3-small-1536',
            ADD COLUMN provider text NOT NULL DEFAULT 'openai',
            ADD COLUMN model text NOT NULL DEFAULT 'text-embedding-3-small',
            ADD COLUMN dimensions integer NOT NULL DEFAULT 1536
        """
    )
    op.execute(
        """
        CREATE TABLE embedding_profile_vectors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            item_id uuid NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            chunk_text text NOT NULL,
            profile_name text NOT NULL,
            provider text NOT NULL,
            model text NOT NULL,
            dimensions integer NOT NULL,
            embedding_384 vector(384),
            embedding_half_384 halfvec(384),
            embedding_768 vector(768),
            embedding_half_768 halfvec(768),
            embedding_1024 vector(1024),
            embedding_half_1024 halfvec(1024),
            embedding_1536 vector(1536),
            embedding_half_1536 halfvec(1536),
            created_at timestamptz DEFAULT now(),
            CONSTRAINT uq_embedding_profile_vectors_item_chunk_profile
                UNIQUE (item_id, chunk_index, profile_name),
            CONSTRAINT ck_embedding_profile_vectors_dimension_column CHECK (
                (dimensions = 384 AND embedding_384 IS NOT NULL AND embedding_half_384 IS NOT NULL
                    AND embedding_768 IS NULL AND embedding_half_768 IS NULL
                    AND embedding_1024 IS NULL AND embedding_half_1024 IS NULL
                    AND embedding_1536 IS NULL AND embedding_half_1536 IS NULL)
                OR (dimensions = 768 AND embedding_768 IS NOT NULL AND embedding_half_768 IS NOT NULL
                    AND embedding_384 IS NULL AND embedding_half_384 IS NULL
                    AND embedding_1024 IS NULL AND embedding_half_1024 IS NULL
                    AND embedding_1536 IS NULL AND embedding_half_1536 IS NULL)
                OR (dimensions = 1024 AND embedding_1024 IS NOT NULL AND embedding_half_1024 IS NOT NULL
                    AND embedding_384 IS NULL AND embedding_half_384 IS NULL
                    AND embedding_768 IS NULL AND embedding_half_768 IS NULL
                    AND embedding_1536 IS NULL AND embedding_half_1536 IS NULL)
                OR (dimensions = 1536 AND embedding_1536 IS NOT NULL AND embedding_half_1536 IS NOT NULL
                    AND embedding_384 IS NULL AND embedding_half_384 IS NULL
                    AND embedding_768 IS NULL AND embedding_half_768 IS NULL
                    AND embedding_1024 IS NULL AND embedding_half_1024 IS NULL)
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_embedding_profile_vectors_item_profile "
        "ON embedding_profile_vectors (item_id, profile_name)"
    )
    op.execute(
        "CREATE INDEX idx_embedding_profile_vectors_halfvec_384_hnsw "
        "ON embedding_profile_vectors USING hnsw (embedding_half_384 halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) WHERE dimensions = 384"
    )
    op.execute(
        "CREATE INDEX idx_embedding_profile_vectors_halfvec_768_hnsw "
        "ON embedding_profile_vectors USING hnsw (embedding_half_768 halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) WHERE dimensions = 768"
    )
    op.execute(
        "CREATE INDEX idx_embedding_profile_vectors_halfvec_1024_hnsw "
        "ON embedding_profile_vectors USING hnsw (embedding_half_1024 halfvec_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) WHERE dimensions = 1024"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS embedding_profile_vectors")
    op.execute(
        """
        ALTER TABLE embeddings
            DROP COLUMN IF EXISTS dimensions,
            DROP COLUMN IF EXISTS model,
            DROP COLUMN IF EXISTS provider,
            DROP COLUMN IF EXISTS profile_name
        """
    )
