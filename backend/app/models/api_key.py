import uuid
from sqlalchemy import ForeignKey, Integer, String, Text, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ApiKeyAuditEvent(Base):
    __tablename__ = "api_key_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), server_default="admin", nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


class McpClient(Base):
    __tablename__ = "mcp_clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    oauth_client_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    oauth_token_ttl_seconds: Mapped[int] = mapped_column(Integer, server_default="3600", nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, server_default="{}", nullable=False)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class McpRequestAuditEvent(Base):
    __tablename__ = "mcp_request_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_clients.id", ondelete="SET NULL"),
        nullable=True,
    )
    client_key: Mapped[str] = mapped_column(Text, nullable=False)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    required_scope: Mapped[str | None] = mapped_column(String(40), nullable=True)
    params_summary: Mapped[dict] = mapped_column(JSONB, server_default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(120), nullable=True)
    app_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


class McpOAuthAccessToken(Base):
    __tablename__ = "mcp_oauth_access_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_clients.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    resource: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
