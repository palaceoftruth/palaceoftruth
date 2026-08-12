import uuid
from datetime import datetime

from sqlalchemy import String, Float, ForeignKey, ForeignKeyConstraint, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ItemRelationship(Base):
    __tablename__ = "item_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    source_item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    target_item_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    relationship: Mapped[str] = mapped_column(String(100), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, server_default="0.0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("source_item_id", "target_item_id", "relationship"),
        Index("ix_item_relationships_tenant_source", "tenant_id", "source_item_id"),
        Index("ix_item_relationships_tenant_target", "tenant_id", "target_item_id"),
        ForeignKeyConstraint(
            ["tenant_id", "source_item_id"],
            ["items.tenant_id", "items.id"],
            name="fk_item_relationships_tenant_source",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_item_id"],
            ["items.tenant_id", "items.id"],
            name="fk_item_relationships_tenant_target",
            ondelete="CASCADE",
        ),
    )
