import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector, HALFVEC
from sqlalchemy import CheckConstraint, Integer, Text, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.embedding_profile import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROFILE_NAME,
    DEFAULT_EMBEDDING_PROVIDER,
    EMBEDDING_DIMENSIONS,
)


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Full-precision embedding retained for relationship and rebuild flows.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    # Half-precision embedding with HNSW index for ANN search.
    embedding_half: Mapped[list[float]] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS), nullable=False)
    profile_name: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_EMBEDDING_PROFILE_NAME)
    provider: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_EMBEDDING_PROVIDER)
    model: Mapped[str] = mapped_column(Text, nullable=False, default=DEFAULT_EMBEDDING_MODEL)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, default=EMBEDDING_DIMENSIONS)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )


class EmbeddingProfileVector(Base):
    __tablename__ = "embedding_profile_vectors"
    __table_args__ = (
        UniqueConstraint("item_id", "chunk_index", "profile_name", name="uq_embedding_profile_vectors_item_chunk_profile"),
        CheckConstraint(
            """
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
            """,
            name="ck_embedding_profile_vectors_dimension_column",
        ),
        CheckConstraint(
            "profile_kind IN ('text', 'native_image', 'multilingual_text')",
            name="ck_embedding_profile_vectors_profile_kind",
        ),
        CheckConstraint(
            "input_modality IN ('text', 'image', 'multilingual_text')",
            name="ck_embedding_profile_vectors_input_modality",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    profile_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_kind: Mapped[str] = mapped_column(Text, nullable=False, default="text")
    input_modality: Mapped[str] = mapped_column(Text, nullable=False, default="text")
    profile_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    embedding_384: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    embedding_half_384: Mapped[list[float] | None] = mapped_column(HALFVEC(384), nullable=True)
    embedding_768: Mapped[list[float] | None] = mapped_column(Vector(768), nullable=True)
    embedding_half_768: Mapped[list[float] | None] = mapped_column(HALFVEC(768), nullable=True)
    embedding_1024: Mapped[list[float] | None] = mapped_column(Vector(1024), nullable=True)
    embedding_half_1024: Mapped[list[float] | None] = mapped_column(HALFVEC(1024), nullable=True)
    embedding_1536: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    embedding_half_1536: Mapped[list[float] | None] = mapped_column(HALFVEC(1536), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
