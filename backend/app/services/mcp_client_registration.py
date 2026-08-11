from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.memory import McpOAuthClientRegisterRequest
from app.services.mcp_containment import derive_containment_mode


@dataclass(frozen=True)
class PublicClientDriftError(ValueError):
    fields: tuple[str, ...]


_CLIENT_COLUMNS = """
id, tenant_id, client_key, display_name, allowed_scopes, metadata,
agent_scope_key, allow_all_agent_scope_reads, allow_tenant_shared_reads, containment_mode,
client_type, redirect_uris, allowed_resources, authorization_code_enabled, oauth_client_id,
token_endpoint_auth_method, oauth_revoked_at, oauth_token_ttl_seconds, created_at, last_seen_at
"""


def _json_value(value: Any, fallback: Any) -> Any:
    return value if isinstance(value, type(fallback)) else fallback


def _drift_fields(row: Any, body: McpOAuthClientRegisterRequest) -> tuple[str, ...]:
    expected = {
        "display_name": body.display_name,
        "allowed_scopes": sorted(body.allowed_scopes),
        "metadata": body.metadata,
        "agent_scope_key": body.agent_scope_key,
        "allow_all_agent_scope_reads": body.allow_all_agent_scope_reads,
        "allow_tenant_shared_reads": body.allow_tenant_shared_reads,
        "containment_mode": derive_containment_mode(
            client_key=body.client_key, requested_mode=body.containment_mode
        ),
        "client_type": "public",
        "redirect_uris": sorted(body.redirect_uris),
        "allowed_resources": sorted(body.allowed_resources),
        "authorization_code_enabled": True,
        "token_endpoint_auth_method": "none",
        "oauth_token_ttl_seconds": body.token_ttl_seconds,
        "oauth_revoked_at": None,
    }
    actual = {
        "display_name": row["display_name"],
        "allowed_scopes": sorted(_json_value(row["allowed_scopes"], [])),
        "metadata": _json_value(row["metadata"], {}),
        "agent_scope_key": row.get("agent_scope_key"),
        "allow_all_agent_scope_reads": bool(row.get("allow_all_agent_scope_reads")),
        "allow_tenant_shared_reads": bool(row.get("allow_tenant_shared_reads")),
        "containment_mode": row.get("containment_mode") or "standard",
        "client_type": row.get("client_type") or "service",
        "redirect_uris": sorted(_json_value(row.get("redirect_uris"), [])),
        "allowed_resources": sorted(_json_value(row.get("allowed_resources"), [])),
        "authorization_code_enabled": bool(row.get("authorization_code_enabled")),
        "token_endpoint_auth_method": row.get("token_endpoint_auth_method") or "client_secret_basic",
        "oauth_token_ttl_seconds": row["oauth_token_ttl_seconds"],
        "oauth_revoked_at": row["oauth_revoked_at"],
    }
    return tuple(field for field, value in expected.items() if actual[field] != value)


async def ensure_public_mcp_client(
    db: AsyncSession,
    *,
    tenant_id: str,
    body: McpOAuthClientRegisterRequest,
) -> tuple[Any, bool]:
    """Create one public client or return the exact stable-key registration."""
    if body.client_type != "public":
        raise ValueError("ensure accepts only public PKCE clients")

    select_statement = text(
        f"SELECT {_CLIENT_COLUMNS} FROM mcp_clients "
        "WHERE tenant_id = :tenant_id AND client_key = :client_key"
    )
    params = {"tenant_id": tenant_id, "client_key": body.client_key}
    row = (await db.execute(select_statement, params)).mappings().one_or_none()
    created = False
    if row is None:
        oauth_client_id = secrets.token_urlsafe(24)
        result = await db.execute(
            text(
                f"""
                INSERT INTO mcp_clients
                    (tenant_id, client_key, display_name, allowed_scopes, metadata, agent_scope_key,
                     allow_all_agent_scope_reads, allow_tenant_shared_reads, containment_mode,
                     client_type, redirect_uris, allowed_resources, authorization_code_enabled,
                     oauth_client_id, token_endpoint_auth_method, oauth_client_secret_hash,
                     oauth_revoked_at, oauth_token_ttl_seconds)
                VALUES
                    (:tenant_id, :client_key, :display_name, CAST(:allowed_scopes AS jsonb),
                     CAST(:metadata AS jsonb), :agent_scope_key, :allow_all_agent_scope_reads,
                     :allow_tenant_shared_reads, :containment_mode, 'public',
                     CAST(:redirect_uris AS jsonb), CAST(:allowed_resources AS jsonb), TRUE,
                     :oauth_client_id, 'none', NULL, NULL, :token_ttl_seconds)
                ON CONFLICT (tenant_id, client_key) DO NOTHING
                RETURNING {_CLIENT_COLUMNS}
                """
            ),
            {
                **params,
                "display_name": body.display_name,
                "allowed_scopes": json.dumps(body.allowed_scopes),
                "metadata": json.dumps(body.metadata),
                "agent_scope_key": body.agent_scope_key,
                "allow_all_agent_scope_reads": body.allow_all_agent_scope_reads,
                "allow_tenant_shared_reads": body.allow_tenant_shared_reads,
                "containment_mode": derive_containment_mode(
                    client_key=body.client_key, requested_mode=body.containment_mode
                ),
                "client_type": "public",
                "redirect_uris": json.dumps(body.redirect_uris),
                "allowed_resources": json.dumps(body.allowed_resources),
                "authorization_code_enabled": True,
                "oauth_client_id": oauth_client_id,
                "token_endpoint_auth_method": "none",
                "secret_hash": None,
                "token_ttl_seconds": body.token_ttl_seconds,
            },
        )
        row = result.mappings().one_or_none()
        created = row is not None
        if row is None:
            row = (await db.execute(select_statement, params)).mappings().one()

    drift = _drift_fields(row, body)
    if drift:
        raise PublicClientDriftError(drift)
    if created:
        await db.commit()
    return row, created
