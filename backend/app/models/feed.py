import uuid
from datetime import datetime
from sqlalchemy import Boolean, Integer, Text, ARRAY, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class Feed(Base):
    __tablename__ = "feeds"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    url: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    auto_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    poll_interval: Mapped[int] = mapped_column(Integer, server_default="3600")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    paused_reason: Mapped[str | None] = mapped_column(Text)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_fetched_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    feed_metadata: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, server_default="default")
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
