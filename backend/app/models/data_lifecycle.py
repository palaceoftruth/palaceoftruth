import uuid
from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DataLifecycleAuditEvent(Base):
    """Control-plane audit record retained after tenant data is erased."""

    __tablename__ = "data_lifecycle_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    subject_tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    subject_item_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
