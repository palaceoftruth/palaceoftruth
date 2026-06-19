from __future__ import annotations

import json
import secrets
import base64
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Form, Header, HTTPException, Request
from sqlalchemy import text

from app.auth import compare_secret, hash_secret
from app.database import async_session
from app.schemas.memory import (
    McpOAuthProtectedResourceMetadata,
    McpOAuthRevokeResponse,
    McpOAuthTokenResponse,
)

router = APIRouter(prefix="/memory/mcp/oauth", tags=["mcp-oauth"])
metadata_router = APIRouter(tags=["mcp-oauth"])

SUPPORTED_SCOPES = (
    "read",
    "write",
    "admin",
    "local_only",
    "destructive_prohibited",
    "capture:write",
    "capture:job:read",
)


def _metadata_url(request: Request, path: str) -> str:
    return str(request.url_for(path))


def _parse_scopes(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(status_code=403, detail="MCP client scopes are invalid")
    scopes = [item for item in value if item in SUPPORTED_SCOPES]
    if not scopes:
        raise HTTPException(status_code=403, detail="MCP client has no usable scopes")
    return scopes


def _requested_scopes(raw_scope: str | None, allowed_scopes: list[str]) -> list[str]:
    if raw_scope is None or not raw_scope.strip():
        return allowed_scopes
    requested = [part.strip() for part in raw_scope.split(" ") if part.strip()]
    invalid = sorted(set(requested) - set(allowed_scopes))
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unsupported requested scope: {', '.join(invalid)}")
    return requested


def _client_credentials_from_request(
    *,
    form_client_id: str | None,
    form_client_secret: str | None,
    authorization: str | None,
) -> tuple[str, str]:
    if form_client_id and form_client_secret:
        return form_client_id, form_client_secret
    if not authorization:
        raise HTTPException(status_code=401, detail="invalid_client")
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        raise HTTPException(status_code=401, detail="invalid_client")
    try:
        decoded = base64.b64decode(encoded.strip()).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid_client") from exc
    client_id, separator, client_secret = decoded.partition(":")
    if not separator or not client_id or not client_secret:
        raise HTTPException(status_code=401, detail="invalid_client")
    return client_id, client_secret


@router.post("/token", response_model=McpOAuthTokenResponse)
async def issue_mcp_access_token(
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
    authorization: str | None = Header(None, alias="Authorization"),
) -> McpOAuthTokenResponse:
    if grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    resolved_client_id, resolved_client_secret = _client_credentials_from_request(
        form_client_id=client_id,
        form_client_secret=client_secret,
        authorization=authorization,
    )

    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT id, tenant_id, client_key, allowed_scopes, oauth_client_secret_hash,
                       oauth_revoked_at, oauth_token_ttl_seconds
                FROM mcp_clients
                WHERE client_key = :client_key
                LIMIT 1
                """
            ),
            {"client_key": resolved_client_id},
        )
        row = result.mappings().one_or_none()
        if row is None or row["oauth_revoked_at"] is not None:
            raise HTTPException(status_code=401, detail="invalid_client")
        if not compare_secret(resolved_client_secret, row["oauth_client_secret_hash"]):
            raise HTTPException(status_code=401, detail="invalid_client")

        allowed_scopes = _parse_scopes(row["allowed_scopes"])
        token_scopes = _requested_scopes(scope, allowed_scopes)
        ttl_seconds = int(row["oauth_token_ttl_seconds"] or 3600)
        if ttl_seconds <= 0:
            raise HTTPException(status_code=403, detail="MCP client token TTL is invalid")

        access_token = secrets.token_urlsafe(48)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        await db.execute(
            text(
                """
                INSERT INTO mcp_oauth_access_tokens
                    (tenant_id, client_id, token_hash, scopes, expires_at)
                VALUES
                    (:tenant_id, :client_id, :token_hash, CAST(:scopes AS jsonb), :expires_at)
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "client_id": row["id"],
                "token_hash": hash_secret(access_token),
                "scopes": json.dumps(token_scopes),
                "expires_at": expires_at,
            },
        )
        await db.commit()

    return McpOAuthTokenResponse(access_token=access_token, expires_in=ttl_seconds, scope=" ".join(token_scopes))


@router.post("/revoke", response_model=McpOAuthRevokeResponse)
async def revoke_mcp_access_token(token: Annotated[str, Form()]) -> McpOAuthRevokeResponse:
    async with async_session() as db:
        await db.execute(
            text(
                "UPDATE mcp_oauth_access_tokens "
                "SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
                "WHERE token_hash = :token_hash"
            ),
            {"token_hash": hash_secret(token)},
        )
        await db.commit()
    return McpOAuthRevokeResponse()


@metadata_router.get("/.well-known/oauth-protected-resource", response_model=McpOAuthProtectedResourceMetadata)
async def mcp_oauth_protected_resource_metadata(request: Request) -> McpOAuthProtectedResourceMetadata:
    resource = str(request.url_for("mcp_oauth_protected_resource_metadata"))
    token_url = _metadata_url(request, "issue_mcp_access_token")
    return McpOAuthProtectedResourceMetadata(
        resource=resource.removesuffix("/.well-known/oauth-protected-resource") + "/mcp",
        authorization_servers=[token_url.rsplit("/token", 1)[0]],
        scopes_supported=list(SUPPORTED_SCOPES),  # type: ignore[arg-type]
    )
