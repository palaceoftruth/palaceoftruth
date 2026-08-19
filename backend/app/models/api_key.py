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
    # Unsalted SHA-256 hex for rows created before migration 055, or
    # "hmac-sha256$<hex>" once the key re-authenticates with a pepper set.
    key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Authoritative scope grant for this key. The X-MCP-Scope headers can only
    # narrow it; they can never add a scope that is not stored here.
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, server_default="legacy-import", nullable=False)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
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


class BrowserSession(Base):
    """A short-lived SPA session minted from an API key (H-20).

    The API key itself never reaches the browser's persistent storage. The
    session token lives only in an HttpOnly cookie, so no script can read it,
    and its scope grant is narrower than the key's by default.
    """

    __tablename__ = "browser_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
    )
    session_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Double-submit companion. Readable by JS on purpose: the check is that the
    # attacker's origin cannot read it back to echo it in the header.
    csrf_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class BrowserExtensionPairingKey(Base):
    __tablename__ = "browser_extension_pairing_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    credential_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    purpose: Mapped[str] = mapped_column(Text, server_default="browser_extension_token", nullable=False)
    issued_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class McpClient(Base):
    __tablename__ = "mcp_clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    agent_scope_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    allow_all_agent_scope_reads: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    allow_tenant_shared_reads: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    allow_workspace_scope_reads: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    oauth_client_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    oauth_token_ttl_seconds: Mapped[int] = mapped_column(Integer, server_default="3600", nullable=False)
    client_type: Mapped[str] = mapped_column(Text, server_default="service", nullable=False)
    # Server-owned containment decision. Never derived from client_key at read
    # time: the registrant chooses that string. See app/services/mcp_containment.py.
    containment_mode: Mapped[str] = mapped_column(Text, server_default="standard", nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    allowed_resources: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    authorization_code_enabled: Mapped[bool] = mapped_column(server_default="false", nullable=False)
    oauth_client_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    token_endpoint_auth_method: Mapped[str] = mapped_column(Text, server_default="client_secret_basic", nullable=False)
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
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    delegated_grant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_oauth_delegated_grants.id", ondelete="SET NULL"),
        nullable=True,
    )
    refresh_token_family_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_oauth_refresh_token_families.id", ondelete="SET NULL"),
        nullable=True,
    )
    issued_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class McpOAuthAuthorizationInteraction(Base):
    """Tenant-qualified, short-lived authorization state; never stores a verifier."""
    __tablename__ = "mcp_oauth_authorization_interactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    agent_scope_keys: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    workspace_scope_keys: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    # These values bind the browser decision to the original authorization
    # request without ever persisting a PKCE verifier or an API credential.
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    pkce_challenge: Mapped[str | None] = mapped_column(Text, nullable=True)
    browser_session_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    csrf_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    authorized_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    consumed_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


class McpOAuthDelegatedGrant(Base):
    __tablename__ = "mcp_oauth_delegated_grants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mcp_clients.id", ondelete="CASCADE"), nullable=False)
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    agent_scope_keys: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    workspace_scope_keys: Mapped[list[str]] = mapped_column(JSONB, server_default="[]", nullable=False)
    authorized_by: Mapped[str] = mapped_column(Text, nullable=False)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


class McpOAuthAuthorizationCode(Base):
    __tablename__ = "mcp_oauth_authorization_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mcp_oauth_delegated_grants.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # OAuth requires the exchange request to repeat the exact callback URI.
    redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    pkce_challenge: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class McpOAuthRefreshTokenFamily(Base):
    __tablename__ = "mcp_oauth_refresh_token_families"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("mcp_oauth_delegated_grants.id", ondelete="CASCADE"), nullable=False)
    current_token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class McpOAuthRefreshToken(Base):
    """Opaque, one-use refresh-token record retained for replay detection."""
    __tablename__ = "mcp_oauth_refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_oauth_refresh_token_families.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    issued_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    used_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    revoked_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[object] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
