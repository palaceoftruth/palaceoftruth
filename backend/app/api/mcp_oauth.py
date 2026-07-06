from __future__ import annotations

import json
import secrets
import base64
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Form, Header, HTTPException, Request
from sqlalchemy import text

from app.auth import compare_secret, hash_secret
from app.database import async_session
from app.mcp_scopes import ALL_MCP_OPERATION_SCOPES, serialize_mcp_scope_catalog
from app.schemas.memory import (
    McpOAuthAuthorizationServerMetadata,
    McpOAuthIntrospectionResponse,
    McpOAuthProtectedResourceMetadata,
    McpOAuthRevokeResponse,
    McpOAuthTokenResponse,
)

router = APIRouter(prefix="/memory/mcp/oauth", tags=["mcp-oauth"])
metadata_router = APIRouter(tags=["mcp-oauth"])

def _metadata_url(request: Request, path: str) -> str:
    parsed = urlsplit(str(request.url_for(path)))
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _canonical_mcp_resource(request: Request) -> str:
    metadata_url = str(request.url_for("mcp_oauth_protected_resource_metadata"))
    resource_url = metadata_url.removesuffix("/.well-known/oauth-protected-resource") + "/mcp"
    parsed = urlsplit(resource_url)
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _canonical_api_resource(request: Request) -> str:
    token_url = _metadata_url(request, "issue_mcp_access_token")
    parsed = urlsplit(token_url)
    return urlunsplit(("https", parsed.netloc, "/api/v1", "", ""))


def _authorization_server_issuer(request: Request) -> str:
    return _metadata_url(request, "issue_mcp_access_token").rsplit("/token", 1)[0]


def _supported_resources(request: Request) -> set[str]:
    return {_canonical_mcp_resource(request), _canonical_api_resource(request)}


def _normalize_resource(value: str | None) -> str | None:
    if value is None:
        return None
    resource = value.strip()
    if not resource:
        return None
    parsed = urlsplit(resource)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise HTTPException(status_code=400, detail="invalid_resource")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", ""))


def _split_tenant_qualified_client_id(client_id: str) -> tuple[str | None, str]:
    tenant_id, separator, client_key = client_id.partition(":")
    if separator and tenant_id and client_key:
        return tenant_id, client_key
    return None, client_id


def _parse_scopes(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(status_code=403, detail="MCP client scopes are invalid")
    scopes = [item for item in value if item.strip()]
    invalid = sorted(set(scopes) - set(ALL_MCP_OPERATION_SCOPES))
    if invalid:
        raise HTTPException(status_code=403, detail=f"MCP client scopes include unsupported scope: {', '.join(invalid)}")
    if not scopes:
        raise HTTPException(status_code=403, detail="MCP client has no usable scopes")
    return scopes


def _unix_timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


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


async def _authenticate_oauth_client(
    *,
    form_client_id: str | None,
    form_client_secret: str | None,
    authorization: str | None,
):
    resolved_client_id, resolved_client_secret = _client_credentials_from_request(
        form_client_id=form_client_id,
        form_client_secret=form_client_secret,
        authorization=authorization,
    )
    tenant_id, client_key = _split_tenant_qualified_client_id(resolved_client_id)
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT id, tenant_id, client_key, allowed_scopes, oauth_client_secret_hash,
                       oauth_revoked_at, oauth_token_ttl_seconds, display_name
                FROM mcp_clients
                WHERE client_key = :client_key
                  AND (CAST(:tenant_id AS text) IS NULL OR tenant_id = CAST(:tenant_id AS text))
                ORDER BY created_at ASC
                LIMIT 2
                """
            ),
            {"client_key": client_key, "tenant_id": tenant_id},
        )
        rows = list(result.mappings().all())
    if tenant_id is None and len(rows) > 1:
        raise HTTPException(status_code=401, detail="invalid_client")
    row = rows[0] if rows else None
    if row is None or row["oauth_revoked_at"] is not None:
        raise HTTPException(status_code=401, detail="invalid_client")
    if not compare_secret(resolved_client_secret, row["oauth_client_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid_client")
    return row


def _resource_kind(resource: str | None) -> str | None:
    if resource is None:
        return None
    return "api" if urlsplit(resource).path.rstrip("/") == "/api/v1" else "mcp"


async def _record_oauth_endpoint_audit_event(
    db,
    *,
    client_row,
    operation: str,
    status: str,
    params_summary: dict,
    required_scope: str | None = None,
    error_class: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO mcp_request_audit_events
                (tenant_id, client_id, client_key, client_name, operation, required_scope,
                 params_summary, status, error_class)
            VALUES
                (:tenant_id, :client_id, :client_key, :client_name, :operation, :required_scope,
                 CAST(:params_summary AS jsonb), :status, :error_class)
            """
        ),
        {
            "tenant_id": client_row["tenant_id"],
            "client_id": client_row["id"],
            "client_key": client_row["client_key"],
            "client_name": client_row.get("display_name") or client_row["client_key"],
            "operation": operation,
            "required_scope": required_scope,
            "params_summary": json.dumps(params_summary),
            "status": status,
            "error_class": error_class,
        },
    )


async def _deny_token_issue(
    db,
    *,
    client_row,
    status_code: int,
    detail: str,
    params_summary: dict,
    error_class: str,
) -> None:
    await _record_oauth_endpoint_audit_event(
        db,
        client_row=client_row,
        operation="oauth.token_issue",
        status="denied",
        params_summary=params_summary,
        error_class=error_class,
    )
    await db.commit()
    raise HTTPException(status_code=status_code, detail=detail)


@router.post("/token", response_model=McpOAuthTokenResponse)
async def issue_mcp_access_token(
    request: Request,
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
    resource: Annotated[str | None, Form()] = None,
    authorization: str | None = Header(None, alias="Authorization"),
) -> McpOAuthTokenResponse:
    if grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
    row = await _authenticate_oauth_client(
        form_client_id=client_id,
        form_client_secret=client_secret,
        authorization=authorization,
    )
    try:
        requested_resource = _normalize_resource(resource)
    except HTTPException:
        async with async_session() as db:
            await _deny_token_issue(
                db,
                client_row=row,
                status_code=400,
                detail="invalid_resource",
                params_summary={
                    "grant_type": "client_credentials",
                    "requested_resource": resource,
                    "resource_kind": None,
                    "denial_reason": "invalid_resource",
                },
                error_class="invalid_resource",
            )
    if requested_resource is None:
        async with async_session() as db:
            await _deny_token_issue(
                db,
                client_row=row,
                status_code=400,
                detail="invalid_resource",
                params_summary={
                    "grant_type": "client_credentials",
                    "requested_resource": None,
                    "resource_kind": None,
                    "denial_reason": "missing_resource",
                },
                error_class="invalid_resource",
            )
    if requested_resource not in _supported_resources(request):
        async with async_session() as db:
            await _deny_token_issue(
                db,
                client_row=row,
                status_code=400,
                detail="invalid_resource",
                params_summary={
                    "grant_type": "client_credentials",
                    "requested_resource": requested_resource,
                    "resource_kind": _resource_kind(requested_resource),
                    "denial_reason": "unsupported_resource",
                },
                error_class="invalid_resource",
            )

    async with async_session() as db:
        try:
            allowed_scopes = _parse_scopes(row["allowed_scopes"])
            token_scopes = _requested_scopes(scope, allowed_scopes)
        except HTTPException as exc:
            await _deny_token_issue(
                db,
                client_row=row,
                status_code=exc.status_code,
                detail=str(exc.detail),
                params_summary={
                    "grant_type": "client_credentials",
                    "requested_scopes": scope,
                    "requested_resource": requested_resource,
                    "resource_kind": _resource_kind(requested_resource),
                    "denial_reason": "invalid_scope",
                },
                error_class="invalid_scope",
            )
        ttl_raw = row["oauth_token_ttl_seconds"]
        ttl_seconds = int(ttl_raw if ttl_raw is not None else 3600)
        if ttl_seconds <= 0:
            await _deny_token_issue(
                db,
                client_row=row,
                status_code=403,
                detail="MCP client token TTL is invalid",
                params_summary={
                    "grant_type": "client_credentials",
                    "requested_scopes": token_scopes,
                    "requested_resource": requested_resource,
                    "resource_kind": _resource_kind(requested_resource),
                    "ttl_seconds": ttl_seconds,
                    "denial_reason": "invalid_ttl",
                },
                error_class="invalid_ttl",
            )

        access_token = secrets.token_urlsafe(48)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        await db.execute(
            text(
                """
                INSERT INTO mcp_oauth_access_tokens
                    (tenant_id, client_id, token_hash, scopes, resource, expires_at)
                VALUES
                    (:tenant_id, :client_id, :token_hash, CAST(:scopes AS jsonb), :resource, :expires_at)
                """
            ),
            {
                "tenant_id": row["tenant_id"],
                "client_id": row["id"],
                "token_hash": hash_secret(access_token),
                "scopes": json.dumps(token_scopes),
                "resource": requested_resource,
                "expires_at": expires_at,
            },
        )
        await _record_oauth_endpoint_audit_event(
            db,
            client_row=row,
            operation="oauth.token_issue",
            status="success",
            params_summary={
                "grant_type": "client_credentials",
                "requested_scopes": token_scopes,
                "requested_resource": requested_resource,
                "resource_kind": _resource_kind(requested_resource),
                "ttl_seconds": ttl_seconds,
            },
        )
        await db.commit()

    return McpOAuthTokenResponse(
        access_token=access_token,
        expires_in=ttl_seconds,
        scope=" ".join(token_scopes),
        resource=requested_resource,
    )


@router.post("/revoke", response_model=McpOAuthRevokeResponse)
async def revoke_mcp_access_token(
    token: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    authorization: str | None = Header(None, alias="Authorization"),
) -> McpOAuthRevokeResponse:
    caller = await _authenticate_oauth_client(
        form_client_id=client_id,
        form_client_secret=client_secret,
        authorization=authorization,
    )
    async with async_session() as db:
        await db.execute(
            text(
                "UPDATE mcp_oauth_access_tokens "
                "SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP) "
                "WHERE token_hash = :token_hash "
                "  AND tenant_id = :tenant_id "
                "  AND client_id = :client_id"
            ),
            {
                "token_hash": hash_secret(token),
                "tenant_id": caller["tenant_id"],
                "client_id": caller["id"],
            },
        )
        await _record_oauth_endpoint_audit_event(
            db,
            client_row=caller,
            operation="oauth.token_revoke",
            status="success",
            params_summary={"token": {"redacted": True, "present": True}},
        )
        await db.commit()
    return McpOAuthRevokeResponse()


@router.post("/introspect", response_model=McpOAuthIntrospectionResponse)
async def introspect_mcp_access_token(
    request: Request,
    token: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    authorization: str | None = Header(None, alias="Authorization"),
) -> McpOAuthIntrospectionResponse:
    caller = await _authenticate_oauth_client(
        form_client_id=client_id,
        form_client_secret=client_secret,
        authorization=authorization,
    )
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT
                    t.scopes AS token_scopes,
                    t.resource AS token_resource,
                    t.issued_at,
                    t.expires_at,
                    t.revoked_at AS token_revoked_at,
                    c.client_key,
                    c.oauth_revoked_at AS client_revoked_at
                FROM mcp_oauth_access_tokens t
                JOIN mcp_clients c ON c.id = t.client_id AND c.tenant_id = t.tenant_id
                WHERE t.token_hash = :token_hash
                  AND t.tenant_id = :tenant_id
                LIMIT 1
                """
            ),
            {"token_hash": hash_secret(token), "tenant_id": caller["tenant_id"]},
        )
        row = result.mappings().one_or_none()
        if row is None:
            await _record_oauth_endpoint_audit_event(
                db,
                client_row=caller,
                operation="oauth.token_introspect",
                status="denied",
                params_summary={
                    "token": {"redacted": True, "present": True},
                    "active": False,
                    "denial_reason": "not_found_for_client",
                },
                error_class="inactive_token",
            )
            await db.commit()

    if row is None:
        return McpOAuthIntrospectionResponse(active=False)
    expires_at = row["expires_at"]
    issued_at = row["issued_at"]
    active = (
        isinstance(expires_at, datetime)
        and expires_at.tzinfo is not None
        and expires_at > datetime.now(timezone.utc)
        and row["token_revoked_at"] is None
        and row["client_revoked_at"] is None
    )
    if not active:
        async with async_session() as db:
            await _record_oauth_endpoint_audit_event(
                db,
                client_row=caller,
                operation="oauth.token_introspect",
                status="denied",
                params_summary={
                    "token": {"redacted": True, "present": True},
                    "active": False,
                    "denial_reason": "inactive",
                },
                error_class="inactive_token",
            )
            await db.commit()
        return McpOAuthIntrospectionResponse(active=False)
    token_scopes = _parse_scopes(row["token_scopes"])
    async with async_session() as db:
        await _record_oauth_endpoint_audit_event(
            db,
            client_row=caller,
            operation="oauth.token_introspect",
            status="success",
            params_summary={
                "token": {"redacted": True, "present": True},
                "active": True,
                "resource": row["token_resource"],
                "resource_kind": _resource_kind(row["token_resource"]),
                "scopes": token_scopes,
            },
        )
        await db.commit()
    return McpOAuthIntrospectionResponse(
        active=True,
        client_id=row["client_key"],
        scope=" ".join(token_scopes),
        token_type="Bearer",
        exp=_unix_timestamp(expires_at),
        iat=_unix_timestamp(issued_at) if isinstance(issued_at, datetime) else None,
        aud=row["token_resource"],
        iss=_authorization_server_issuer(request),
    )


def _mcp_oauth_protected_resource_metadata_response(request: Request) -> McpOAuthProtectedResourceMetadata:
    return McpOAuthProtectedResourceMetadata(
        resource=_canonical_mcp_resource(request),
        authorization_servers=[_authorization_server_issuer(request)],
        scopes_supported=list(ALL_MCP_OPERATION_SCOPES),
        resource_name="Palace MCP",
        scope_catalog=serialize_mcp_scope_catalog(),
    )


@metadata_router.get("/.well-known/oauth-protected-resource", response_model=McpOAuthProtectedResourceMetadata)
async def mcp_oauth_protected_resource_metadata(request: Request) -> McpOAuthProtectedResourceMetadata:
    return _mcp_oauth_protected_resource_metadata_response(request)


@metadata_router.get("/.well-known/oauth-protected-resource/mcp", response_model=McpOAuthProtectedResourceMetadata)
async def mcp_oauth_protected_resource_metadata_for_mcp_path(request: Request) -> McpOAuthProtectedResourceMetadata:
    return _mcp_oauth_protected_resource_metadata_response(request)


@metadata_router.get("/.well-known/oauth-protected-resource/api/v1", response_model=McpOAuthProtectedResourceMetadata)
async def palace_api_oauth_protected_resource_metadata(request: Request) -> McpOAuthProtectedResourceMetadata:
    return McpOAuthProtectedResourceMetadata(
        resource=_canonical_api_resource(request),
        authorization_servers=[_authorization_server_issuer(request)],
        scopes_supported=list(ALL_MCP_OPERATION_SCOPES),
        resource_name="Palace API",
        scope_catalog=serialize_mcp_scope_catalog(),
    )


def _mcp_oauth_authorization_server_metadata_response(request: Request) -> McpOAuthAuthorizationServerMetadata:
    token_url = _metadata_url(request, "issue_mcp_access_token")
    return McpOAuthAuthorizationServerMetadata(
        issuer=_authorization_server_issuer(request),
        token_endpoint=token_url,
        revocation_endpoint=_metadata_url(request, "revoke_mcp_access_token"),
        introspection_endpoint=_metadata_url(request, "introspect_mcp_access_token"),
        grant_types_supported=["client_credentials"],
        scopes_supported=list(ALL_MCP_OPERATION_SCOPES),
        token_endpoint_auth_methods_supported=["client_secret_basic", "client_secret_post"],
        revocation_endpoint_auth_methods_supported=["client_secret_basic", "client_secret_post"],
        introspection_endpoint_auth_methods_supported=["client_secret_basic", "client_secret_post"],
    )


@metadata_router.get("/.well-known/oauth-authorization-server", response_model=McpOAuthAuthorizationServerMetadata)
async def mcp_oauth_authorization_server_metadata(request: Request) -> McpOAuthAuthorizationServerMetadata:
    return _mcp_oauth_authorization_server_metadata_response(request)


@metadata_router.get(
    "/.well-known/oauth-authorization-server/api/v1/memory/mcp/oauth",
    response_model=McpOAuthAuthorizationServerMetadata,
)
async def mcp_oauth_authorization_server_metadata_for_issuer_path(
    request: Request,
) -> McpOAuthAuthorizationServerMetadata:
    return _mcp_oauth_authorization_server_metadata_response(request)
