from __future__ import annotations

import json
import secrets
import base64
import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Form, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.auth import compare_secret, hash_secret, verify_api_key
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


def _browser_consent_url(request: Request, interaction_id: str) -> str:
    """Return the same-site SPA route without trusting a caller-supplied URI."""
    parsed = urlsplit(str(request.base_url))
    host = parsed.hostname or ""
    if host.startswith("api."):
        host = host.removeprefix("api.")
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, "/oauth/consent", f"interaction_id={interaction_id}", ""))


def _authorization_response_uri(*, redirect_uri: str, state: str | None, code: str | None, error: str | None) -> str:
    """Build a response only after the callback URI was read from durable state."""
    parsed = urlsplit(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if code is not None:
        query.append(("code", code))
    if error is not None:
        query.append(("error", error))
    if state is not None:
        query.append(("state", state))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


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


def _matches_s256_pkce_verifier(*, verifier: str, challenge: str) -> bool:
    """Compare RFC 7636 S256 values without persisting or logging the verifier."""
    if not 43 <= len(verifier) <= 128:
        return False
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" for character in verifier):
        return False
    derived = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(derived, challenge)


def _valid_s256_challenge(challenge: str | None) -> bool:
    return isinstance(challenge, str) and len(challenge) == 43 and all(
        character in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in challenge
    )


def _list_of_strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


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
                       oauth_revoked_at, oauth_token_ttl_seconds, display_name,
                       client_type, authorization_code_enabled
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


async def _issue_authorization_code_access_token(
    *,
    db,
    client_row,
    code: str | None,
    code_verifier: str | None,
    redirect_uri: str | None,
) -> McpOAuthTokenResponse:
    """Redeem one tenant-bound code without exposing which binding failed.

    The row lock and conditional update make successful redemptions one-use
    even when two token requests race.  Invalid exchanges leave the code
    unconsumed so an attacker cannot burn a valid user's authorization code.
    """
    if (
        not client_row.get("authorization_code_enabled")
        or client_row.get("client_type") != "confidential_web"
        or not code
        or not code_verifier
        or not redirect_uri
    ):
        raise HTTPException(status_code=400, detail="invalid_grant")

    result = await db.execute(
        text(
            """
            SELECT c.id, c.grant_id, c.pkce_challenge, c.redirect_uri, g.client_id, g.resource, g.scopes,
                   g.revoked_at, c.used_at, c.expires_at
            FROM mcp_oauth_authorization_codes c
            JOIN mcp_oauth_delegated_grants g ON g.id = c.grant_id AND g.tenant_id = c.tenant_id
            WHERE c.code_hash = :code_hash AND c.tenant_id = :tenant_id
            FOR UPDATE
            """
        ),
        {"code_hash": hash_secret(code), "tenant_id": client_row["tenant_id"]},
    )
    row = result.mappings().one_or_none()
    now = datetime.now(timezone.utc)
    if (
        row is None
        or row["client_id"] != client_row["id"]
        or row["used_at"] is not None
        or row["revoked_at"] is not None
        or not isinstance(row["expires_at"], datetime)
        or row["expires_at"] <= now
        or row["redirect_uri"] != redirect_uri
        or not _matches_s256_pkce_verifier(verifier=code_verifier, challenge=row["pkce_challenge"])
    ):
        raise HTTPException(status_code=400, detail="invalid_grant")

    # Validate every remaining grant and client invariant before consuming the
    # code, so a malformed durable row cannot turn into a one-request DoS.
    token_scopes = _parse_scopes(row["scopes"])
    ttl_seconds = int(client_row["oauth_token_ttl_seconds"] or 3600)
    if ttl_seconds <= 0:
        raise HTTPException(status_code=400, detail="invalid_grant")

    consumed = await db.execute(
        text(
            """
            UPDATE mcp_oauth_authorization_codes
            SET used_at = :used_at
            WHERE id = :code_id AND used_at IS NULL
            RETURNING id
            """
        ),
        {"code_id": row["id"], "used_at": now},
    )
    if consumed.mappings().one_or_none() is None:
        raise HTTPException(status_code=400, detail="invalid_grant")

    access_token = secrets.token_urlsafe(48)
    expires_at = now + timedelta(seconds=ttl_seconds)
    await db.execute(
        text(
            """
            INSERT INTO mcp_oauth_access_tokens
                (tenant_id, client_id, token_hash, scopes, resource, delegated_grant_id, expires_at)
            VALUES
                (:tenant_id, :client_id, :token_hash, CAST(:scopes AS jsonb), :resource,
                 :delegated_grant_id, :expires_at)
            """
        ),
        {
            "tenant_id": client_row["tenant_id"],
            "client_id": client_row["id"],
            "token_hash": hash_secret(access_token),
            "scopes": json.dumps(token_scopes),
            "resource": row["resource"],
            "delegated_grant_id": row["grant_id"] if "grant_id" in row else None,
            "expires_at": expires_at,
        },
    )
    await _record_oauth_endpoint_audit_event(
        db,
        client_row=client_row,
        operation="oauth.authorization_code_exchange",
        status="success",
        params_summary={"grant_type": "authorization_code", "resource": row["resource"], "scopes": token_scopes},
    )
    await db.commit()
    return McpOAuthTokenResponse(
        access_token=access_token,
        expires_in=ttl_seconds,
        scope=" ".join(token_scopes),
        resource=row["resource"],
    )


@router.get("/authorize")
async def begin_mcp_authorization(
    request: Request,
    response_type: str = Query(),
    client_id: str = Query(),
    redirect_uri: str = Query(),
    resource: str = Query(),
    code_challenge: str = Query(),
    code_challenge_method: str = Query(),
    scope: str | None = Query(None),
    state: str | None = Query(None),
) -> RedirectResponse:
    """Create a tenant-bound consent interaction for a confidential PKCE client.

    The external client never chooses the consent page or browser authority. The
    actual tenant identity is proved later by the existing browser API-key path.
    """
    tenant_hint, client_key = _split_tenant_qualified_client_id(client_id)
    if (
        response_type != "code"
        or tenant_hint is None
        or not redirect_uri
        or code_challenge_method != "S256"
        or not _valid_s256_challenge(code_challenge)
    ):
        raise HTTPException(status_code=400, detail="invalid_request")
    try:
        requested_resource = _normalize_resource(resource)
    except HTTPException as exc:
        raise HTTPException(status_code=400, detail="invalid_request") from exc
    if requested_resource is None or requested_resource not in _supported_resources(request):
        raise HTTPException(status_code=400, detail="invalid_request")

    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT id, tenant_id, client_key, display_name, allowed_scopes, redirect_uris,
                       allowed_resources, client_type, authorization_code_enabled, oauth_revoked_at
                FROM mcp_clients
                WHERE tenant_id = :tenant_id AND client_key = :client_key
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_hint, "client_key": client_key},
        )
        client_row = result.mappings().one_or_none()
        if (
            client_row is None
            or client_row["oauth_revoked_at"] is not None
            or client_row["client_type"] != "confidential_web"
            or not client_row["authorization_code_enabled"]
            or redirect_uri not in _list_of_strings(client_row["redirect_uris"])
            or requested_resource not in _list_of_strings(client_row["allowed_resources"])
        ):
            raise HTTPException(status_code=400, detail="invalid_request")
        try:
            requested_scopes = _requested_scopes(scope, _parse_scopes(client_row["allowed_scopes"]))
        except HTTPException as exc:
            raise HTTPException(status_code=400, detail="invalid_scope") from exc

        interaction_id = secrets.token_hex(32)
        browser_session = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.execute(
            text(
                """
                INSERT INTO mcp_oauth_authorization_interactions
                    (id, tenant_id, client_id, resource, scopes, agent_scope_keys, workspace_scope_keys,
                     redirect_uri, state, pkce_challenge, browser_session_hash, csrf_token_hash, expires_at)
                VALUES
                    (CAST(:id AS uuid), :tenant_id, :client_id, :resource, CAST(:scopes AS jsonb),
                     CAST('[]' AS jsonb), CAST('[]' AS jsonb), :redirect_uri, :state, :pkce_challenge,
                     :browser_session_hash, :csrf_token_hash, :expires_at)
                """
            ),
            {
                "id": interaction_id,
                "tenant_id": client_row["tenant_id"],
                "client_id": client_row["id"],
                "resource": requested_resource,
                "scopes": json.dumps(requested_scopes),
                "redirect_uri": redirect_uri,
                "state": state,
                "pkce_challenge": code_challenge,
                "browser_session_hash": hash_secret(browser_session),
                "csrf_token_hash": hash_secret(csrf_token),
                "expires_at": expires_at,
            },
        )
        await db.commit()

    redirect = RedirectResponse(_browser_consent_url(request, interaction_id), status_code=303)
    # The API-key remains only in the browser's existing local-storage path;
    # these short-lived cookies are merely possession and CSRF bindings.
    redirect.set_cookie("palace_oauth_consent_session", browser_session, max_age=600, secure=True, httponly=True, samesite="lax", path="/api/v1/memory/mcp/oauth")
    redirect.set_cookie("palace_oauth_consent_csrf", csrf_token, max_age=600, secure=True, httponly=False, samesite="lax", path="/")
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


@router.get("/authorize/{interaction_id}")
async def get_mcp_authorization_interaction(
    request: Request,
    interaction_id: str,
    browser_session: Annotated[str | None, Cookie(alias="palace_oauth_consent_session")] = None,
    _: str = Depends(verify_api_key),
) -> dict[str, object]:
    """Return a non-secret summary for the tenant-bound consent browser session."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not isinstance(tenant_id, str) or not tenant_id or not browser_session:
        raise HTTPException(status_code=400, detail="invalid_request")

    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT i.tenant_id, i.resource, i.scopes, i.agent_scope_keys, i.workspace_scope_keys,
                       i.browser_session_hash, i.decision, i.consumed_at, i.expires_at,
                       c.client_key, c.display_name
                FROM mcp_oauth_authorization_interactions i
                JOIN mcp_clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id
                WHERE i.id = CAST(:id AS uuid)
                LIMIT 1
                """
            ),
            {"id": interaction_id},
        )
        interaction = result.mappings().one_or_none()

    now = datetime.now(timezone.utc)
    if (
        interaction is None
        or interaction["tenant_id"] != tenant_id
        or interaction["decision"] is not None
        or interaction["consumed_at"] is not None
        or not isinstance(interaction["expires_at"], datetime)
        or interaction["expires_at"] <= now
        or not compare_secret(browser_session, interaction["browser_session_hash"])
    ):
        raise HTTPException(status_code=400, detail="invalid_request")

    # Never disclose the callback URI, client state, PKCE challenge, or bindings.
    return {
        "client_name": interaction["display_name"] or interaction["client_key"],
        "tenant_id": tenant_id,
        "resource": interaction["resource"],
        "scopes": interaction["scopes"],
        "agent_scope_keys": interaction["agent_scope_keys"],
        "workspace_scope_keys": interaction["workspace_scope_keys"],
        "expires_at": interaction["expires_at"].isoformat(),
    }


@router.post("/authorize/{interaction_id}/decision")
async def decide_mcp_authorization(
    request: Request,
    interaction_id: str,
    decision: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()],
    browser_session: Annotated[str | None, Cookie(alias="palace_oauth_consent_session")] = None,
    _: str = Depends(verify_api_key),
) -> dict[str, str]:
    """Approve or deny a single interaction using the tenant's browser API key.

    The API key authenticates the tenant but is never inserted into the grant,
    code, interaction, audit payload, or redirect. Cookie possession and a
    double-submit CSRF token bind the browser decision to its GET request.
    """
    if decision not in {"approved", "denied"} or not browser_session or not csrf_token:
        raise HTTPException(status_code=400, detail="invalid_request")
    tenant_id = getattr(request.state, "tenant_id", None)
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(status_code=403, detail="invalid_request")

    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT i.id, i.tenant_id, i.client_id, i.resource, i.scopes, i.agent_scope_keys,
                       i.workspace_scope_keys, i.redirect_uri, i.state, i.pkce_challenge,
                       i.browser_session_hash, i.csrf_token_hash, i.decision, i.consumed_at, i.expires_at,
                       c.client_key, c.display_name
                FROM mcp_oauth_authorization_interactions i
                JOIN mcp_clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id
                WHERE i.id = CAST(:id AS uuid)
                FOR UPDATE
                """
            ),
            {"id": interaction_id},
        )
        interaction = result.mappings().one_or_none()
        now = datetime.now(timezone.utc)
        if (
            interaction is None
            or interaction["tenant_id"] != tenant_id
            or interaction["decision"] is not None
            or interaction["consumed_at"] is not None
            or not isinstance(interaction["expires_at"], datetime)
            or interaction["expires_at"] <= now
            or not compare_secret(browser_session, interaction["browser_session_hash"])
            or not compare_secret(csrf_token, interaction["csrf_token_hash"])
        ):
            raise HTTPException(status_code=400, detail="invalid_request")

        code: str | None = None
        if decision == "approved":
            grant = await db.execute(
                text(
                    """
                    INSERT INTO mcp_oauth_delegated_grants
                        (tenant_id, client_id, resource, scopes, agent_scope_keys, workspace_scope_keys, authorized_by)
                    VALUES
                        (:tenant_id, :client_id, :resource, CAST(:scopes AS jsonb), CAST(:agent_scope_keys AS jsonb),
                         CAST(:workspace_scope_keys AS jsonb), :authorized_by)
                    RETURNING id
                    """
                ),
                {
                    "tenant_id": tenant_id, "client_id": interaction["client_id"], "resource": interaction["resource"],
                    "scopes": json.dumps(interaction["scopes"]), "agent_scope_keys": json.dumps(interaction["agent_scope_keys"]),
                    "workspace_scope_keys": json.dumps(interaction["workspace_scope_keys"]), "authorized_by": "tenant-admin-browser",
                },
            )
            grant_id = grant.mappings().one_or_none()["id"]
            code = secrets.token_urlsafe(48)
            await db.execute(
                text(
                    """
                    INSERT INTO mcp_oauth_authorization_codes
                        (tenant_id, grant_id, code_hash, redirect_uri, pkce_challenge, expires_at)
                    VALUES (:tenant_id, :grant_id, :code_hash, :redirect_uri, :pkce_challenge, :expires_at)
                    """
                ),
                {
                    "tenant_id": tenant_id, "grant_id": grant_id, "code_hash": hash_secret(code),
                    "redirect_uri": interaction["redirect_uri"], "pkce_challenge": interaction["pkce_challenge"],
                    "expires_at": now + timedelta(minutes=5),
                },
            )
        await db.execute(
            text(
                """
                UPDATE mcp_oauth_authorization_interactions
                SET decision = :decision, authorized_by = :authorized_by, decided_at = :decided_at, consumed_at = :consumed_at
                WHERE id = :id AND decision IS NULL AND consumed_at IS NULL
                """
            ),
            {"id": interaction["id"], "decision": decision, "authorized_by": "tenant-admin-browser", "decided_at": now, "consumed_at": now},
        )
        await db.commit()
    return {"redirect_uri": _authorization_response_uri(
        redirect_uri=interaction["redirect_uri"], state=interaction["state"], code=code,
        error="access_denied" if decision == "denied" else None,
    )}


@router.post("/token", response_model=McpOAuthTokenResponse)
async def issue_mcp_access_token(
    request: Request,
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    scope: Annotated[str | None, Form()] = None,
    resource: Annotated[str | None, Form()] = None,
    code: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    authorization: str | None = Header(None, alias="Authorization"),
) -> McpOAuthTokenResponse:
    row = await _authenticate_oauth_client(
        form_client_id=client_id,
        form_client_secret=client_secret,
        authorization=authorization,
    )
    if grant_type == "authorization_code":
        async with async_session() as db:
            return await _issue_authorization_code_access_token(
                db=db,
                client_row=row,
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
            )
    if grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")
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
