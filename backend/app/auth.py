import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, Header, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.mcp_scopes import VALID_MCP_OPERATION_SCOPES
from app.services.mcp_containment import CONTAINMENT_STANDARD, normalize_containment_mode

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    tenant_id: str
    auth_mode: str
    subject_id: str | None = None
    client_id: Any | None = None
    client_key: str | None = None
    client_name: str | None = None
    agent_scope_key: str | None = None
    # Server-owned; never inferred from client_key. See services/mcp_containment.py.
    containment_mode: str = CONTAINMENT_STANDARD
    allow_all_agent_scope_reads: bool = False
    allow_tenant_shared_reads: bool = False
    delegated_agent_scope_keys: tuple[str, ...] = ()
    delegated_workspace_scope_keys: tuple[str, ...] = ()
    delegated_grant_id: Any | None = None
    scopes: tuple[str, ...] = ()
    capabilities: frozenset[str] = field(default_factory=frozenset)
    resource: str | None = None
    audience: str | None = None
    token_hash_reference: str | None = None
    audit_metadata: Mapping[str, Any] = field(default_factory=dict)

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities or "admin" in self.capabilities


# Stored credential verifiers carry their own format tag so peppered and
# legacy rows can coexist during the rollout. Untagged values are the legacy
# unsalted SHA-256 digests written before the pepper existed.
PEPPERED_HASH_PREFIX = "hmac-sha256$"


def _legacy_hash_secret(raw: str) -> str:
    """Unsalted SHA-256 digest — read-only compatibility for pre-pepper rows."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _peppered_hash_secret(raw: str, pepper: str) -> str:
    digest = hmac.new(pepper.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{PEPPERED_HASH_PREFIX}{digest}"


def _credential_pepper() -> str:
    return (getattr(settings, "credential_pepper", "") or "").strip()


def hash_secret(raw: str) -> str:
    """Return the verifier to persist for ``raw``.

    Peppered HMAC-SHA256 when a pepper is configured, otherwise the legacy
    unsalted digest so deployments without a pepper keep working unchanged.
    """
    pepper = _credential_pepper()
    if pepper:
        return _peppered_hash_secret(raw, pepper)
    return _legacy_hash_secret(raw)


def secret_hash_candidates(raw: str) -> tuple[str, str]:
    """Return (preferred, legacy) verifiers to match a stored row against.

    Lookups must accept both while rows written before the pepper was
    configured are still in the table. Both entries are equal when no pepper is
    set, which keeps every ``IN (:hash, :legacy_hash)`` lookup index-friendly.
    """
    return hash_secret(raw), _legacy_hash_secret(raw)


def is_legacy_secret_hash(stored: str | None) -> bool:
    """True when ``stored`` still uses the pre-pepper format and can be upgraded."""
    if not isinstance(stored, str) or not stored:
        return False
    return bool(_credential_pepper()) and not stored.startswith(PEPPERED_HASH_PREFIX)


def _hash_key(raw: str) -> str:
    return hash_secret(raw)


def _parse_json_list(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(status_code=403, detail="MCP client scopes are invalid")
    scopes = [item for item in value if item.strip()]
    invalid = sorted(set(scopes) - VALID_MCP_OPERATION_SCOPES)
    if invalid:
        raise HTTPException(status_code=403, detail=f"MCP client scopes include unsupported scope: {', '.join(invalid)}")
    return scopes


def _parse_stored_api_key_scopes(value: object) -> tuple[str, ...]:
    """Normalize the ``api_keys.scopes`` grant.

    Fails closed: a NULL, malformed, or unrecognized entry contributes nothing,
    so a key with no usable stored grant has no capabilities at all.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            logger.warning("api_keys.scopes held non-JSON text; treating the grant as empty")
            return ()
    if not isinstance(value, list):
        return ()
    scopes = [
        item
        for item in value
        if isinstance(item, str) and item.strip() in VALID_MCP_OPERATION_SCOPES
    ]
    return tuple(dict.fromkeys(scope.strip() for scope in scopes))


def _parse_scope_header(*values: str | None) -> list[str]:
    scopes: list[str] = []
    for value in values:
        if value is None:
            continue
        for part in value.replace(",", " ").split():
            scope = part.strip()
            if scope:
                scopes.append(scope)
    invalid = sorted(set(scopes) - VALID_MCP_OPERATION_SCOPES)
    if invalid:
        raise HTTPException(status_code=403, detail=f"Unsupported MCP scope header: {', '.join(invalid)}")
    return list(dict.fromkeys(scopes))


def _canonical_mcp_resource(request: Request) -> str:
    try:
        url = str(request.url_for("mcp_oauth_protected_resource_metadata"))
        resource_url = url.removesuffix("/.well-known/oauth-protected-resource") + "/mcp"
    except Exception:
        base_url = str(request.base_url).rstrip("/")
        resource_url = f"{base_url}/mcp"
    parsed = urlsplit(resource_url)
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def _canonical_api_resource(request: Request) -> str:
    parsed = urlsplit(str(request.base_url))
    return urlunsplit(("https", parsed.netloc, "/api/v1", "", ""))


def paired_service_resources(request: Request, path: str) -> set[str]:
    """Return resources for Palace's bounded ``api.``/``mcp.`` host pair.

    Both production hosts route to the same control plane, and older clients
    can retain the API-host MCP audience while connecting through the dedicated
    MCP host. Only those two prefixes on the exact same dotted parent domain
    are aliases; local, ambiguous, and unrelated hosts remain host-bound.
    """
    parsed = urlsplit(str(request.base_url))
    host = (parsed.hostname or "").lower()
    service_prefix = next((prefix for prefix in ("api.", "mcp.") if host.startswith(prefix)), None)
    if service_prefix is None:
        return {urlunsplit(("https", parsed.netloc, path, "", ""))}
    parent_domain = host.removeprefix(service_prefix)
    if "." not in parent_domain:
        return {urlunsplit(("https", parsed.netloc, path, "", ""))}
    port_suffix = f":{parsed.port}" if parsed.port is not None else ""
    return {
        urlunsplit(("https", f"api.{parent_domain}{port_suffix}", path, "", "")),
        urlunsplit(("https", f"mcp.{parent_domain}{port_suffix}", path, "", "")),
    }


def _resource_metadata_url(request: Request) -> str:
    parsed_base = urlsplit(str(request.base_url))
    path = "/.well-known/oauth-protected-resource"
    if request.url.path.startswith("/api/v1"):
        path = f"{path}/api/v1"
    elif request.url.path.startswith("/mcp"):
        path = f"{path}/mcp"
    return urlunsplit(("https", parsed_base.netloc, path, "", ""))


def _bearer_auth_headers(request: Request, *, error: str | None = None) -> dict[str, str]:
    params = [f'resource_metadata="{_resource_metadata_url(request)}"']
    if error:
        params.insert(0, f'error="{error}"')
    return {"WWW-Authenticate": "Bearer " + ", ".join(params)}


def _auth_exception(request: Request, status_code: int, detail: str, *, error: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_bearer_auth_headers(request, error=error),
    )


def _is_mcp_resource_validation_request(request: Request) -> bool:
    return request.url.path == "/api/v1/memory/whoami" or request.url.path.startswith("/mcp")


def _expected_token_resources(request: Request, expected_resource: str | None = None) -> set[str]:
    if expected_resource == "mcp" and _is_mcp_resource_validation_request(request):
        return paired_service_resources(request, "/mcp")
    if request.url.path.startswith("/api/v1"):
        return paired_service_resources(request, "/api/v1")
    return paired_service_resources(request, "/mcp")


def _resource_matches_token(*, token_resource: object, expected_resources: set[str] | None) -> bool:
    if expected_resources is None:
        return True
    if token_resource is None:
        # Legacy tokens minted before SAR-984 did not persist an audience. Keep
        # them valid only for the MCP resource while clients rotate tokens.
        return any(resource.endswith("/mcp") for resource in expected_resources)
    return isinstance(token_resource, str) and token_resource in expected_resources


def _request_route(request: Request) -> str:
    return request.url.path


async def _record_token_validation_audit_event(
    db,
    *,
    request: Request,
    token_row,
    operation: str,
    required_scope: str | None,
    status: str,
    error_class: str | None = None,
    params_summary: dict[str, Any] | None = None,
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
            "tenant_id": token_row["tenant_id"],
            "client_id": token_row["client_id"],
            "client_key": token_row["client_key"],
            "client_name": token_row.get("display_name") or token_row["client_key"],
            "operation": operation,
            "required_scope": required_scope,
            "params_summary": json.dumps(
                {
                    "route": _request_route(request),
                    "resource": token_row.get("token_resource"),
                    "scopes": token_row.get("token_scopes") if isinstance(token_row.get("token_scopes"), list) else None,
                    **(params_summary or {}),
                }
            ),
            "status": status,
            "error_class": error_class,
        },
    )


def _context_from_scopes(
    *,
    tenant_id: str,
    auth_mode: str,
    token_hash_reference: str | None,
    subject_id: str | None = None,
    client_id: object | None = None,
    client_key: str | None = None,
    client_name: str | None = None,
    agent_scope_key: str | None = None,
    containment_mode: object | None = None,
    allow_all_agent_scope_reads: bool = False,
    allow_tenant_shared_reads: bool = False,
    delegated_agent_scope_keys: tuple[str, ...] = (),
    delegated_workspace_scope_keys: tuple[str, ...] = (),
    delegated_grant_id: object | None = None,
    scopes: list[str] | tuple[str, ...] | None = None,
    resource: object | None = None,
    audit_metadata: Mapping[str, Any] | None = None,
) -> AuthContext:
    normalized_scopes = tuple(dict.fromkeys(scopes or ()))
    resource_value = resource if isinstance(resource, str) else None
    return AuthContext(
        tenant_id=tenant_id,
        auth_mode=auth_mode,
        subject_id=subject_id,
        client_id=client_id,
        client_key=client_key,
        client_name=client_name,
        agent_scope_key=agent_scope_key,
        containment_mode=normalize_containment_mode(containment_mode),
        allow_all_agent_scope_reads=allow_all_agent_scope_reads,
        allow_tenant_shared_reads=allow_tenant_shared_reads,
        delegated_agent_scope_keys=delegated_agent_scope_keys,
        delegated_workspace_scope_keys=delegated_workspace_scope_keys,
        delegated_grant_id=delegated_grant_id,
        scopes=normalized_scopes,
        capabilities=frozenset(normalized_scopes),
        resource=resource_value,
        audience=resource_value,
        token_hash_reference=token_hash_reference,
        audit_metadata=MappingProxyType(dict(audit_metadata or {})),
    )


def _attach_auth_context(request: Request, context: AuthContext) -> AuthContext:
    request.state.auth_context = context
    request.state.tenant_id = context.tenant_id
    request.state.key_hash = context.token_hash_reference
    request.state.auth_mode = context.auth_mode
    request.state.mcp_client_id = context.client_id
    request.state.mcp_client_key = context.client_key
    request.state.mcp_client_name = context.client_name
    request.state.mcp_agent_scope_key = context.agent_scope_key
    request.state.mcp_containment_mode = context.containment_mode
    request.state.mcp_allow_all_agent_scope_reads = context.allow_all_agent_scope_reads
    request.state.mcp_allow_tenant_shared_reads = context.allow_tenant_shared_reads
    request.state.mcp_allowed_scopes = list(context.scopes) if context.scopes else None
    request.state.mcp_token_resource = context.resource
    return context


def get_auth_context(request: Request) -> AuthContext:
    context = getattr(request.state, "auth_context", None)
    if isinstance(context, AuthContext):
        return context

    tenant_id = getattr(request.state, "tenant_id", None)
    auth_mode = getattr(request.state, "auth_mode", None)
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(status_code=403, detail="Authenticated tenant is missing")
    if not isinstance(auth_mode, str) or not auth_mode:
        raise HTTPException(status_code=403, detail="Authenticated principal is missing")

    raw_scopes = getattr(request.state, "mcp_allowed_scopes", None)
    scopes = tuple(scope for scope in raw_scopes if isinstance(scope, str)) if isinstance(raw_scopes, list) else ()
    token_resource = getattr(request.state, "mcp_token_resource", None)
    return _context_from_scopes(
        tenant_id=tenant_id,
        auth_mode=auth_mode,
        token_hash_reference=getattr(request.state, "key_hash", None),
        client_id=getattr(request.state, "mcp_client_id", None),
        client_key=getattr(request.state, "mcp_client_key", None),
        client_name=getattr(request.state, "mcp_client_name", None),
        agent_scope_key=getattr(request.state, "mcp_agent_scope_key", None),
        containment_mode=getattr(request.state, "mcp_containment_mode", None),
        allow_all_agent_scope_reads=bool(
            getattr(request.state, "mcp_allow_all_agent_scope_reads", False)
        ),
        allow_tenant_shared_reads=bool(
            getattr(request.state, "mcp_allow_tenant_shared_reads", False)
        ),
        scopes=scopes,
        resource=token_resource,
    )


async def verify_api_key(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> str:
    """Validate X-API-Key against api_keys table.

    Sets request.state.tenant_id on success. Raises HTTP 403 on failure.
    """
    if not api_key:
        raise _auth_exception(request, 403, "Missing API key", error="invalid_token")

    key_hash, legacy_key_hash = secret_hash_candidates(api_key)

    async with async_session() as db:
        row = await db.execute(
            text(
                "SELECT id, tenant_id, scopes, key_hash FROM api_keys "
                "WHERE key_hash IN (:hash, :legacy_hash) AND revoked_at IS NULL "
                "LIMIT 1"
            ),
            {"hash": key_hash, "legacy_hash": legacy_key_hash},
        )
        result = row.mappings().one_or_none()
        if result is not None:
            await db.execute(
                text("UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = :id"),
                {"id": result["id"]},
            )
            if is_legacy_secret_hash(result.get("key_hash")):
                # Upgrade-on-use: the raw key is only available here, so this is
                # the one place a pre-pepper row can be re-hashed.
                await db.execute(
                    text("UPDATE api_keys SET key_hash = :hash WHERE id = :id"),
                    {"hash": key_hash, "id": result["id"]},
                )
            await db.commit()

    if result is None:
        raise _auth_exception(request, 403, "Invalid or revoked API key", error="invalid_token")

    stored_scopes = _parse_stored_api_key_scopes(result.get("scopes"))
    if not stored_scopes:
        # A key with no usable stored grant can do nothing, and some routes
        # depend on verify_api_key alone. Deny at authentication instead of
        # letting an ungated route through.
        logger.warning("API key %s has no usable stored scope grant", result["id"])
        raise _auth_exception(
            request,
            403,
            "API key has no stored scope grant",
            error="insufficient_scope",
        )
    _attach_auth_context(
        request,
        _context_from_scopes(
            tenant_id=result["tenant_id"],
            auth_mode="api_key",
            subject_id=str(result["id"]),
            token_hash_reference=key_hash,
            scopes=stored_scopes,
            audit_metadata={
                "api_key_id": str(result["id"]),
                "api_key_granted_scopes": list(stored_scopes),
            },
        ),
    )
    return api_key


async def verify_memory_auth(
    request: Request,
    api_key: str | None = Security(api_key_header),
    authorization: str | None = Header(None, alias="Authorization"),
    expected_resource: str | None = Header(None, alias="X-Palace-Expected-Resource"),
) -> str:
    if api_key:
        return await verify_api_key(request, api_key)

    if authorization is None:
        raise _auth_exception(request, 403, "Missing API key or bearer token", error="invalid_token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _auth_exception(request, 403, "Invalid Authorization header", error="invalid_request")

    token_hash, legacy_token_hash = secret_hash_candidates(token.strip())
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT
                    t.id AS token_id,
                    t.token_hash AS stored_token_hash,
                    t.tenant_id,
                    t.scopes AS token_scopes,
                    t.resource AS token_resource,
                    t.expires_at,
                    t.revoked_at AS token_revoked_at,
                    t.delegated_grant_id,
                    c.id AS client_id,
                    c.client_key,
                    c.display_name,
                    c.allowed_scopes,
                    c.agent_scope_key,
                    c.containment_mode,
                    c.allow_all_agent_scope_reads,
                    c.allow_tenant_shared_reads,
                    c.oauth_revoked_at AS client_revoked_at,
                    g.revoked_at AS grant_revoked_at,
                    g.agent_scope_keys AS delegated_agent_scope_keys,
                    g.workspace_scope_keys AS delegated_workspace_scope_keys
                FROM mcp_oauth_access_tokens t
                JOIN mcp_clients c ON c.id = t.client_id AND c.tenant_id = t.tenant_id
                LEFT JOIN mcp_oauth_delegated_grants g ON g.id = t.delegated_grant_id AND g.tenant_id = t.tenant_id
                WHERE t.token_hash IN (:token_hash, :legacy_token_hash)
                LIMIT 1
                """
            ),
            {"token_hash": token_hash, "legacy_token_hash": legacy_token_hash},
        )
        result = row.mappings().one_or_none()
        if result is not None:
            expires_at = result["expires_at"]
            if not isinstance(expires_at, datetime):
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=None,
                    status="denied",
                    error_class="invalid_expiry",
                )
                await db.commit()
                raise _auth_exception(request, 403, "MCP bearer token expiry is invalid", error="invalid_token")
            if expires_at.tzinfo is None:
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=None,
                    status="denied",
                    error_class="invalid_expiry",
                )
                await db.commit()
                raise _auth_exception(request, 403, "MCP bearer token expiry is invalid", error="invalid_token")
            if expires_at <= datetime.now(timezone.utc):
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=None,
                    status="denied",
                    error_class="expired_token",
                )
                await db.commit()
                raise _auth_exception(request, 403, "MCP bearer token expired", error="invalid_token")
            if result["token_revoked_at"] is not None or result["client_revoked_at"] is not None or (result.get("delegated_grant_id") is not None and result.get("grant_revoked_at") is not None):
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=None,
                    status="denied",
                    error_class="revoked_token",
                )
                await db.commit()
                raise _auth_exception(request, 403, "MCP bearer token revoked", error="invalid_token")
            allowed_scopes = _parse_json_list(result["allowed_scopes"])
            token_scopes = _parse_json_list(result["token_scopes"])
            if any(scope not in allowed_scopes for scope in token_scopes):
                raise HTTPException(status_code=403, detail="MCP bearer token scopes are invalid")
            token_resource = result.get("token_resource")
            expected_resources = _expected_token_resources(request, expected_resource=expected_resource)
            if not _resource_matches_token(token_resource=token_resource, expected_resources=expected_resources):
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=None,
                    status="denied",
                    error_class="invalid_resource",
                    params_summary={"expected_resources": sorted(expected_resources)},
                )
                await db.commit()
                raise _auth_exception(request, 403, "MCP bearer token resource is invalid", error="invalid_token")
            await db.execute(
                text(
                    "UPDATE mcp_oauth_access_tokens "
                    "SET last_used_at = CURRENT_TIMESTAMP "
                    "WHERE id = :token_id"
                ),
                {"token_id": result["token_id"]},
            )
            if is_legacy_secret_hash(result.get("stored_token_hash")):
                await db.execute(
                    text("UPDATE mcp_oauth_access_tokens SET token_hash = :hash WHERE id = :token_id"),
                    {"hash": token_hash, "token_id": result["token_id"]},
                )
            await db.execute(
                text("UPDATE mcp_clients SET last_seen_at = CURRENT_TIMESTAMP WHERE id = :client_id"),
                {"client_id": result["client_id"]},
            )
            await _record_token_validation_audit_event(
                db,
                request=request,
                token_row=result,
                operation="oauth.token_use",
                required_scope=None,
                status="success",
                params_summary={"expected_resources": sorted(expected_resources)},
            )
            await db.commit()

    if result is None:
        raise _auth_exception(request, 403, "Invalid MCP bearer token", error="invalid_token")

    _attach_auth_context(
        request,
        _context_from_scopes(
            tenant_id=result["tenant_id"],
            auth_mode="mcp_oauth",
            token_hash_reference=token_hash,
            subject_id=str(result["client_id"]),
            client_id=result["client_id"],
            client_key=result["client_key"],
            client_name=result.get("display_name") or result["client_key"],
            agent_scope_key=result.get("agent_scope_key"),
            containment_mode=result.get("containment_mode"),
            allow_all_agent_scope_reads=bool(result.get("allow_all_agent_scope_reads")),
            allow_tenant_shared_reads=bool(result.get("allow_tenant_shared_reads")),
            delegated_agent_scope_keys=tuple(result.get("delegated_agent_scope_keys") or ()),
            delegated_workspace_scope_keys=tuple(result.get("delegated_workspace_scope_keys") or ()),
            delegated_grant_id=result.get("delegated_grant_id"),
            scopes=token_scopes,
            resource=result.get("token_resource"),
            audit_metadata={"token_id": str(result["token_id"])},
        ),
    )
    return token.strip()


async def _verify_scoped_bearer_token(
    request: Request,
    *,
    authorization: str | None,
    required_scope: str,
    auth_mode: str,
    detail_prefix: str,
) -> str:
    if authorization is None:
        raise _auth_exception(request, 403, f"Missing API key or {detail_prefix} bearer token", error="invalid_token")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _auth_exception(request, 403, "Invalid Authorization header", error="invalid_request")

    token_hash, legacy_token_hash = secret_hash_candidates(token.strip())
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT
                    t.id AS token_id,
                    t.token_hash AS stored_token_hash,
                    t.tenant_id,
                    t.scopes AS token_scopes,
                    t.resource AS token_resource,
                    t.expires_at,
                    t.revoked_at AS token_revoked_at,
                    t.delegated_grant_id,
                    c.id AS client_id,
                    c.client_key,
                    c.display_name,
                    c.allowed_scopes,
                    c.agent_scope_key,
                    c.containment_mode,
                    c.allow_all_agent_scope_reads,
                    c.allow_tenant_shared_reads,
                    c.oauth_revoked_at AS client_revoked_at,
                    g.revoked_at AS grant_revoked_at,
                    g.agent_scope_keys AS delegated_agent_scope_keys,
                    g.workspace_scope_keys AS delegated_workspace_scope_keys
                FROM mcp_oauth_access_tokens t
                JOIN mcp_clients c ON c.id = t.client_id AND c.tenant_id = t.tenant_id
                LEFT JOIN mcp_oauth_delegated_grants g ON g.id = t.delegated_grant_id AND g.tenant_id = t.tenant_id
                WHERE t.token_hash IN (:token_hash, :legacy_token_hash)
                LIMIT 1
                """
            ),
            {"token_hash": token_hash, "legacy_token_hash": legacy_token_hash},
        )
        result = row.mappings().one_or_none()
        if result is not None:
            expires_at = result["expires_at"]
            if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=required_scope,
                    status="denied",
                    error_class="invalid_expiry",
                )
                await db.commit()
                raise _auth_exception(request, 403, f"{detail_prefix} bearer token expiry is invalid", error="invalid_token")
            if expires_at <= datetime.now(timezone.utc):
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=required_scope,
                    status="denied",
                    error_class="expired_token",
                )
                await db.commit()
                raise _auth_exception(request, 403, f"{detail_prefix} bearer token expired", error="invalid_token")
            if result["token_revoked_at"] is not None or result["client_revoked_at"] is not None or (result.get("delegated_grant_id") is not None and result.get("grant_revoked_at") is not None):
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=required_scope,
                    status="denied",
                    error_class="revoked_token",
                )
                await db.commit()
                raise _auth_exception(request, 403, f"{detail_prefix} bearer token revoked", error="invalid_token")
            allowed_scopes = _parse_json_list(result["allowed_scopes"])
            token_scopes = _parse_json_list(result["token_scopes"])
            if any(scope not in allowed_scopes for scope in token_scopes):
                raise HTTPException(status_code=403, detail=f"{detail_prefix} bearer token scopes are invalid")
            if required_scope not in token_scopes:
                await _record_token_validation_audit_event(
                    db,
                    request=request,
                    token_row=result,
                    operation="oauth.token_use",
                    required_scope=required_scope,
                    status="denied",
                    error_class="insufficient_scope",
                )
                await db.commit()
                raise HTTPException(status_code=403, detail=f"{detail_prefix} bearer token missing {required_scope} scope")
            await db.execute(
                text(
                    "UPDATE mcp_oauth_access_tokens "
                    "SET last_used_at = CURRENT_TIMESTAMP "
                    "WHERE id = :token_id"
                ),
                {"token_id": result["token_id"]},
            )
            if is_legacy_secret_hash(result.get("stored_token_hash")):
                await db.execute(
                    text("UPDATE mcp_oauth_access_tokens SET token_hash = :hash WHERE id = :token_id"),
                    {"hash": token_hash, "token_id": result["token_id"]},
                )
            await db.execute(
                text("UPDATE mcp_clients SET last_seen_at = CURRENT_TIMESTAMP WHERE id = :client_id"),
                {"client_id": result["client_id"]},
            )
            await _record_token_validation_audit_event(
                db,
                request=request,
                token_row=result,
                operation="oauth.token_use",
                required_scope=required_scope,
                status="success",
            )
            await db.commit()

    if result is None:
        raise _auth_exception(request, 403, f"Invalid {detail_prefix} bearer token", error="invalid_token")

    _attach_auth_context(
        request,
        _context_from_scopes(
            tenant_id=result["tenant_id"],
            auth_mode=auth_mode,
            token_hash_reference=token_hash,
            subject_id=str(result["client_id"]),
            client_id=result["client_id"],
            client_key=result["client_key"],
            client_name=result.get("display_name") or result["client_key"],
            agent_scope_key=result.get("agent_scope_key"),
            containment_mode=result.get("containment_mode"),
            allow_all_agent_scope_reads=bool(result.get("allow_all_agent_scope_reads")),
            allow_tenant_shared_reads=bool(result.get("allow_tenant_shared_reads")),
            delegated_agent_scope_keys=tuple(result.get("delegated_agent_scope_keys") or ()),
            delegated_workspace_scope_keys=tuple(result.get("delegated_workspace_scope_keys") or ()),
            delegated_grant_id=result.get("delegated_grant_id"),
            scopes=token_scopes,
            resource=result.get("token_resource"),
            audit_metadata={"token_id": str(result["token_id"])},
        ),
    )
    return token.strip()


async def verify_capture_write_auth(
    request: Request,
    api_key: str | None = Security(api_key_header),
    authorization: str | None = Header(None, alias="Authorization"),
) -> str:
    if api_key:
        return await verify_api_key(request, api_key)
    return await _verify_scoped_bearer_token(
        request,
        authorization=authorization,
        required_scope="capture:write",
        auth_mode="browser_extension",
        detail_prefix="extension",
    )


async def verify_capture_job_read_auth(
    request: Request,
    api_key: str | None = Security(api_key_header),
    authorization: str | None = Header(None, alias="Authorization"),
) -> str:
    if api_key:
        return await verify_api_key(request, api_key)
    return await _verify_scoped_bearer_token(
        request,
        authorization=authorization,
        required_scope="capture:job:read",
        auth_mode="browser_extension",
        detail_prefix="extension",
    )


async def record_oauth_client_audit_event(
    request: Request,
    *,
    operation: str,
    required_scope: str | None,
    status: str,
    params_summary: dict | None = None,
    error_class: str | None = None,
    app_version: str | None = None,
) -> None:
    if getattr(request.state, "auth_mode", None) not in {"mcp_oauth", "browser_extension"}:
        return
    client_id = getattr(request.state, "mcp_client_id", None)
    if client_id is None:
        return
    async with async_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO mcp_request_audit_events
                    (tenant_id, client_id, client_key, client_name, operation, required_scope,
                     params_summary, status, error_class, app_version)
                VALUES
                    (:tenant_id, :client_id, :client_key, :client_name, :operation, :required_scope,
                     CAST(:params_summary AS jsonb), :status, :error_class, :app_version)
                """
            ),
            {
                "tenant_id": request.state.tenant_id,
                "client_id": client_id,
                "client_key": getattr(request.state, "mcp_client_key", "unknown"),
                "client_name": getattr(request.state, "mcp_client_name", None) or "Unknown client",
                "operation": operation,
                "required_scope": required_scope,
                "params_summary": json.dumps(params_summary or {}),
                "status": status,
                "error_class": error_class,
                "app_version": app_version,
            },
        )
        await db.commit()


KNOWN_AUTH_MODES = frozenset({"api_key", "mcp_oauth", "browser_extension"})


def _require_known_auth_mode(request: Request, auth_mode: object) -> str:
    """Deny any principal whose auth mode is missing or unrecognized.

    Both branches used to fall through to success, which made every capability
    gate fail open for an unset or future auth mode.
    """
    if not isinstance(auth_mode, str) or auth_mode not in KNOWN_AUTH_MODES:
        logger.warning(
            "Refusing capability check for unknown auth mode %r on %s",
            auth_mode,
            _request_route(request),
        )
        raise _auth_exception(
            request,
            403,
            "Authenticated principal is missing a recognized auth mode",
            error="invalid_token",
        )
    return auth_mode


def _insufficient_scope_detail(auth_mode: str, required_capability: str) -> str:
    if auth_mode == "api_key":
        return f"API key missing {required_capability} scope"
    return f"MCP bearer token missing {required_capability} scope"


def require_mcp_scope(required_scope: str):
    return require_capability(required_scope)


def require_capability(required_capability: str):
    async def dependency(
        request: Request,
        _: str = Depends(verify_memory_auth),
        mcp_scope: str | None = Header(None, alias="X-MCP-Scope"),
        mcp_scopes: str | None = Header(None, alias="X-MCP-Scopes"),
    ) -> None:
        auth_mode = getattr(request.state, "auth_mode", None)
        if auth_mode == "api_key":
            _require_api_key_scope_header(request, required_capability, mcp_scope, mcp_scopes)
            return
        _require_known_auth_mode(request, auth_mode)
        context = get_auth_context(request)
        if not context.scopes:
            raise _auth_exception(request, 403, "MCP bearer token scopes are invalid", error="insufficient_scope")
        if not context.has_capability(required_capability):
            await record_oauth_client_audit_event(
                request,
                operation="oauth.route_capability",
                required_scope=required_capability,
                status="denied",
                params_summary={
                    "route": _request_route(request),
                    "resource": context.resource,
                    "scopes": list(context.scopes),
                },
                error_class="insufficient_scope",
            )
            raise _auth_exception(
                request,
                403,
                f"MCP bearer token missing {required_capability} scope",
                error="insufficient_scope",
            )

    return dependency


def require_api_capability(required_capability: str):
    async def dependency(
        request: Request,
        _: str = Depends(verify_memory_auth),
    ) -> None:
        context = get_auth_context(request)
        # Tenant API keys are checked against the scope grant persisted on the
        # api_keys row. There is no request header in this path, so nothing the
        # caller sends can influence the decision.
        auth_mode = _require_known_auth_mode(request, context.auth_mode)
        if not context.scopes:
            raise _auth_exception(
                request,
                403,
                "Authenticated principal has no granted scopes",
                error="insufficient_scope",
            )
        if not context.has_capability(required_capability):
            await record_oauth_client_audit_event(
                request,
                operation="oauth.route_capability",
                required_scope=required_capability,
                status="denied",
                params_summary={
                    "route": _request_route(request),
                    "resource": context.resource,
                    "scopes": list(context.scopes),
                },
                error_class="insufficient_scope",
            )
            raise _auth_exception(
                request,
                403,
                _insufficient_scope_detail(auth_mode, required_capability),
                error="insufficient_scope",
            )

    return dependency


def _require_api_key_scope_header(
    request: Request,
    required_scope: str,
    mcp_scope: str | None,
    mcp_scopes: str | None,
) -> None:
    # Validate the caller's header before anything else so a malformed scope
    # keeps its own error rather than surfacing as a missing-context error.
    requested_scopes = _parse_scope_header(mcp_scope, mcp_scopes)
    header_present = mcp_scope is not None or mcp_scopes is not None
    if not header_present:
        # Unchanged from before this rewrite: an MCP-scoped route reached with
        # an API key has always had to declare the scope it acts under.
        raise HTTPException(
            status_code=403,
            detail=f"API key missing {required_scope} MCP scope header",
        )
    context = get_auth_context(request)
    # The grant is what migration 055 persisted on the api_keys row. The headers
    # are a request from the caller, never a source of authority: they can only
    # remove scopes from the grant, and an unknown or absent grant denies.
    granted_scopes = list(dict.fromkeys(context.scopes or ()))
    requested_set = set(requested_scopes)
    effective_scopes = [scope for scope in granted_scopes if scope in requested_set]
    if required_scope not in effective_scopes and "admin" not in effective_scopes:
        raise HTTPException(
            status_code=403,
            detail=f"API key missing {required_scope} MCP scope",
        )
    # request.state.mcp_allowed_scopes is refreshed by _attach_auth_context below.
    audit_metadata = dict(context.audit_metadata or {})
    # Keep the stored grant and the caller's narrowing distinguishable in the
    # audit trail; before this they were one undifferentiated scope list.
    audit_metadata["api_key_granted_scopes"] = sorted(granted_scopes)
    audit_metadata["mcp_requested_scopes"] = sorted(requested_scopes)
    audit_metadata["mcp_effective_scopes"] = sorted(effective_scopes)
    _attach_auth_context(
        request,
        _context_from_scopes(
            tenant_id=context.tenant_id,
            auth_mode=context.auth_mode,
            token_hash_reference=context.token_hash_reference,
            subject_id=context.subject_id,
            client_id=context.client_id,
            client_key=context.client_key,
            client_name=context.client_name,
            scopes=effective_scopes,
            resource=context.resource,
            audit_metadata=audit_metadata,
        ),
    )


def require_api_key_scope_header(required_scope: str):
    async def dependency(
        request: Request,
        _: str = Depends(verify_memory_auth),
        mcp_scope: str | None = Header(None, alias="X-MCP-Scope"),
        mcp_scopes: str | None = Header(None, alias="X-MCP-Scopes"),
    ) -> None:
        if getattr(request.state, "auth_mode", None) == "api_key":
            _require_api_key_scope_header(request, required_scope, mcp_scope, mcp_scopes)

    return dependency


def compare_secret(raw: str, secret_hash: str | None) -> bool:
    if secret_hash is None:
        return False
    # Both candidates are always compared so the answer does not depend on
    # which format the row happens to hold.
    matched = False
    for candidate in secret_hash_candidates(raw):
        matched |= secrets.compare_digest(candidate, secret_hash)
    return matched
