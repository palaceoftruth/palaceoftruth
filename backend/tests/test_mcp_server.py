import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.mcp_server import (
    McpHttpAuthMiddleware,
    McpHttpAuthResult,
    McpHttpAuthVerifier,
    PalaceApiError,
    SecondBrainApiClient,
    SecondBrainMcpRuntime,
    SecondBrainMcpSettings,
    _build_scope,
    _normalize_created_at,
    _port_from_env,
    _streamable_http_transport_security,
    backfill_deferred_relationships,
    capture_checkpoint,
    connection_info,
    create_memory_entry,
    get_graph,
    get_item_relationships,
    get_answer_audit,
    get_claim_support,
    get_palace_room,
    get_retrieval_doctor,
    get_wakeup_context,
    list_memory_entries,
    list_memory_scopes,
    list_temporal_facts,
    palace_checkpoint,
    palace_connection_info,
    palace_context,
    palace_remember,
    palace_search,
    mcp,
    retrieve_agent_memory,
    retrieve_memory_trajectory,
)


def _mcp_auth_test_client(
    *,
    transport: httpx.MockTransport,
    settings: SecondBrainMcpSettings | None = None,
) -> TestClient:
    async def endpoint(request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST"])])
    verifier = McpHttpAuthVerifier(
        settings
        or SecondBrainMcpSettings(
            api_base_url="https://api.palaceoftruth.test",
            api_key="adapter-key",
            timeout_seconds=5.0,
        ),
        transport=transport,
    )
    return TestClient(McpHttpAuthMiddleware(app, verifier))


def _whoami_transport(tenants_by_credential: dict[str, str | dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/memory/whoami":
            return httpx.Response(404, json={"detail": "not found"})
        credential = request.headers.get("x-api-key") or request.headers.get("authorization")
        payload = tenants_by_credential.get(credential or "")
        if payload is None:
            return httpx.Response(403, json={"detail": "Invalid or revoked API key"})
        if isinstance(payload, str):
            payload = {
                "status": "ok",
                "tenant_id": payload,
                "auth_mode": "api_key",
                "mcp_client_key": None,
                "allowed_scopes": [],
            }
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_settings_from_env_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("SECONDBRAIN_API_BASE_URL", "https://api.secondbrain.test")

    with pytest.raises(RuntimeError, match="PALACEOFTRUTH_API_KEY, SECONDBRAIN_API_KEY, or API_KEY"):
        SecondBrainMcpSettings.from_env()


@pytest.mark.asyncio
async def test_mcp_surface_exposes_no_destructive_item_or_feed_delete_tools() -> None:
    tool_names = {tool.name for tool in await mcp.list_tools()}

    assert {
        "palace_search",
        "palace_remember",
        "palace_checkpoint",
        "palace_context",
        "get_wakeup_context",
        "get_claim_support",
        "get_answer_audit",
    } <= tool_names
    assert not any("delete" in name or "purge" in name for name in tool_names)
    assert not {"delete_item", "delete_feed", "purge_item"} & tool_names


def test_settings_from_env_falls_back_to_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.setenv("API_KEY", "fallback-secret")
    monkeypatch.setenv("SECONDBRAIN_API_BASE_URL", "https://api.secondbrain.test")

    settings = SecondBrainMcpSettings.from_env()

    assert settings.api_key == "fallback-secret"


def test_settings_from_env_accepts_oauth_client_secret_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_API_BASE_URL", "https://api.palaceoftruth.test")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "codex-remote")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")

    settings = SecondBrainMcpSettings.from_env()

    assert settings.api_key is None
    assert settings.oauth_client_secret == "client-secret"
    assert settings.oauth_token_url == "https://api.palaceoftruth.test/api/v1/memory/mcp/oauth/token"


def test_settings_from_env_accepts_explicit_oauth_resource_and_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_RESOURCE", "https://mcp.palace.sarvent.cloud/mcp")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_AUDIENCE", "https://mcp.palace.sarvent.cloud/mcp")

    settings = SecondBrainMcpSettings.from_env()

    assert settings.oauth_resource == "https://mcp.palace.sarvent.cloud/mcp"
    assert settings.oauth_audience == "https://mcp.palace.sarvent.cloud/mcp"


def test_settings_from_env_prefers_palace_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "palace-secret")
    monkeypatch.setenv("SECONDBRAIN_API_KEY", "secondbrain-secret")
    monkeypatch.setenv("PALACEOFTRUTH_API_BASE_URL", "https://api.palaceoftruth.test")
    monkeypatch.setenv("SECONDBRAIN_API_BASE_URL", "https://api.secondbrain.test")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("SECONDBRAIN_MCP_TIMEOUT_SECONDS", "30")

    settings = SecondBrainMcpSettings.from_env()

    assert settings.api_key == "palace-secret"
    assert settings.api_base_url == "https://api.palaceoftruth.test"
    assert settings.timeout_seconds == 12.5


def test_mcp_http_auth_rejects_missing_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "should not be called"})

    transport = httpx.MockTransport(handler)

    with _mcp_auth_test_client(transport=transport) as client:
        response = client.post("/mcp")

    assert response.status_code == 401
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "Missing API key or bearer token",
    }


def test_mcp_http_auth_accepts_valid_api_key_for_adapter_tenant() -> None:
    transport = _whoami_transport({"adapter-key": "tenant-a"})

    with _mcp_auth_test_client(transport=transport) as client:
        response = client.post("/mcp", headers={"X-API-Key": "adapter-key"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_mcp_http_auth_preserves_api_key_scope_header_for_caller() -> None:
    seen_scope_headers: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/v1/memory/whoami":
            return httpx.Response(404, json={"detail": "not found"})
        credential = request.headers.get("x-api-key")
        if credential == "caller-key":
            seen_scope_headers.append((request.headers.get("x-mcp-scope"), request.headers.get("x-mcp-scopes")))
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "tenant_id": "tenant-a",
                    "auth_mode": "api_key",
                    "mcp_client_key": None,
                    "allowed_scopes": [],
                },
            )
        if credential == "adapter-key":
            return httpx.Response(200, json={"tenant_id": "tenant-a"})
        return httpx.Response(403, json={"detail": "Invalid or revoked API key"})

    async def scenario() -> McpHttpAuthResult:
        verifier = McpHttpAuthVerifier(
            SecondBrainMcpSettings(
                api_base_url="https://api.palaceoftruth.test",
                api_key="adapter-key",
                timeout_seconds=5.0,
            ),
            transport=httpx.MockTransport(handler),
        )
        return await verifier.verify(
            {
                "x-api-key": "caller-key",
                "x-mcp-scope": "read",
            }
        )

    auth_result = asyncio.run(scenario())

    assert seen_scope_headers == [("read", None)]
    assert auth_result.client_id == "api-key"
    assert auth_result.scopes == ("read",)


def test_mcp_http_auth_accepts_valid_bearer_for_adapter_tenant() -> None:
    transport = _whoami_transport(
        {
            "adapter-key": "tenant-a",
            "Bearer caller-token": {
                "status": "ok",
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "mcp_client_key": "codex-remote",
                "allowed_scopes": ["read"],
            },
        }
    )

    with _mcp_auth_test_client(transport=transport) as client:
        response = client.post("/mcp", headers={"Authorization": "Bearer caller-token"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_mcp_http_auth_rejects_valid_credential_for_different_tenant() -> None:
    transport = _whoami_transport(
        {
            "adapter-key": "tenant-a",
            "other-tenant-key": "tenant-b",
        }
    )

    with _mcp_auth_test_client(transport=transport) as client:
        response = client.post("/mcp", headers={"X-API-Key": "other-tenant-key"})

    assert response.status_code == 403
    assert response.json() == {
        "error": "invalid_token",
        "error_description": "MCP credential tenant does not match adapter tenant",
    }


@pytest.mark.asyncio
async def test_api_client_uses_static_mcp_bearer_token() -> None:
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, json={"tenant_id": "tenant-a"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test") as http_client:
        api = SecondBrainApiClient(
            SecondBrainMcpSettings(
                api_base_url="https://api.test",
                api_key="legacy-api-key",
                bearer_token="bearer-token",
            ),
            client=http_client,
        )
        await api.whoami()

    assert seen_headers["authorization"] == "Bearer bearer-token"
    assert "x-api-key" not in seen_headers


@pytest.mark.asyncio
async def test_api_client_mints_oauth_token_with_client_credentials() -> None:
    seen_requests: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((request.method, str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/oauth/token"):
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "client_id=codex-remote" in body
            assert "resource=https%3A%2F%2Fapi.test%2Fapi%2Fv1" in body
            return httpx.Response(200, json={"access_token": "minted-token", "expires_in": 3600})
        return httpx.Response(200, json={"tenant_id": "tenant-a"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test") as http_client:
        api = SecondBrainApiClient(
            SecondBrainMcpSettings(
                api_base_url="https://api.test",
                api_key="legacy-api-key",
                oauth_client_secret="client-secret",
                oauth_token_url="https://api.test/api/v1/memory/mcp/oauth/token",
                client_key="codex-remote",
            ),
            client=http_client,
        )
        await api.whoami()

    assert seen_requests[0][1].endswith("/api/v1/memory/mcp/oauth/token")
    assert seen_requests[1][2] == "Bearer minted-token"


@pytest.mark.asyncio
async def test_api_client_audit_metadata_reports_oauth_auth_mode() -> None:
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            return httpx.Response(200, json={"access_token": "minted-token", "expires_in": 3600})
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test") as http_client:
        api = SecondBrainApiClient(
            SecondBrainMcpSettings(
                api_base_url="https://api.test",
                api_key="legacy-api-key",
                oauth_client_secret="client-secret",
                oauth_token_url="https://api.test/api/v1/memory/mcp/oauth/token",
                client_key="codex-remote",
            ),
            client=http_client,
        )
        await api.record_mcp_request_audit(
            operation="get_wakeup_context",
            required_scope="read",
            params_summary={},
            status="success",
            latency_ms=12,
            error_class=None,
        )

    assert audit_payload["client"]["metadata"]["auth_mode"] == "oauth_client_credentials"


@pytest.mark.asyncio
async def test_api_client_audit_metadata_reports_static_bearer_when_mixed_credentials() -> None:
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            raise AssertionError("static bearer should be used before minting OAuth")
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test") as http_client:
        api = SecondBrainApiClient(
            SecondBrainMcpSettings(
                api_base_url="https://api.test",
                api_key="legacy-api-key",
                bearer_token="bearer-token",
                oauth_client_secret="client-secret",
                oauth_token_url="https://api.test/api/v1/memory/mcp/oauth/token",
                client_key="codex-remote",
            ),
            client=http_client,
        )
        await api.record_mcp_request_audit(
            operation="get_wakeup_context",
            required_scope="read",
            params_summary={},
            status="success",
            latency_ms=12,
            error_class=None,
        )

    assert audit_payload["client"]["metadata"]["auth_mode"] == "static_bearer"


@pytest.mark.asyncio
async def test_api_client_ignores_legacy_mcp_oauth_resource_for_backend_calls() -> None:
    seen_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            body = request.content.decode()
            seen_bodies.append(body)
            return httpx.Response(200, json={"access_token": "minted-token", "expires_in": 3600})
        return httpx.Response(200, json={"tenant_id": "tenant-a"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.test") as http_client:
        api = SecondBrainApiClient(
            SecondBrainMcpSettings(
                api_base_url="https://api.test",
                api_key="legacy-api-key",
                oauth_client_secret="client-secret",
                oauth_token_url="https://api.test/api/v1/memory/mcp/oauth/token",
                oauth_resource="https://mcp.test/mcp",
                client_key="codex-remote",
            ),
            client=http_client,
        )
        await api.whoami()

    assert "resource=https%3A%2F%2Fapi.test%2Fapi%2Fv1" in seen_bodies[0]
    assert "resource=https%3A%2F%2Fmcp.test%2Fmcp" not in seen_bodies[0]


def test_build_scope_validates_scope_shape() -> None:
    assert _build_scope("tenant_shared", None) == {"type": "tenant_shared"}

    with pytest.raises(ValueError, match="scope_key is required"):
        _build_scope("workspace", None)

    with pytest.raises(ValueError, match="must be omitted"):
        _build_scope("tenant_shared", "launch-pad")


def test_settings_from_env_validates_default_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "karen")

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type == "agent"
    assert settings.default_scope_key == "karen"

    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "workspace")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY")
    with pytest.raises(RuntimeError, match="scope_key is required"):
        SecondBrainMcpSettings.from_env()


def test_settings_from_env_loads_default_scope_from_hermes_palace_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "palaceoftruth.json").write_text(
        json.dumps({"scope_type": "agent", "scope_key": "iris", "api_key": "do-not-read"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type == "agent"
    assert settings.default_scope_key == "iris"

    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "andrew")
    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type == "agent"
    assert settings.default_scope_key == "andrew"


def test_settings_from_env_ignores_incomplete_hermes_palace_config_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "palaceoftruth.json").write_text(
        json.dumps({"scope_type": "agent"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type is None
    assert settings.default_scope_key is None


def test_settings_from_env_ignores_invalid_hermes_palace_config_scope_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "palaceoftruth.json").write_text(
        json.dumps({"scope_type": "bogus", "scope_key": "iris"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type is None
    assert settings.default_scope_key is None


def test_settings_from_env_drops_stale_scope_key_for_tenant_shared_hermes_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "palaceoftruth.json").write_text(
        json.dumps({"scope_type": "tenant_shared", "scope_key": "old-agent"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type == "tenant_shared"
    assert settings.default_scope_key is None


def test_settings_from_env_defaults_key_only_hermes_config_to_agent_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "palaceoftruth.json").write_text(
        json.dumps({"scope_key": "iris"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type == "agent"
    assert settings.default_scope_key == "iris"


def test_settings_from_env_scope_type_override_does_not_reuse_hermes_scope_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "palaceoftruth.json").write_text(
        json.dumps({"scope_type": "agent", "scope_key": "iris"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "tenant_shared")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)

    settings = SecondBrainMcpSettings.from_env()

    assert settings.default_scope_type == "tenant_shared"
    assert settings.default_scope_key is None


def test_normalize_created_at_defaults_to_utc_z_suffix() -> None:
    created_at = _normalize_created_at(None)
    assert created_at.endswith("Z")


def test_port_from_env_ignores_kubernetes_service_style_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECONDBRAIN_MCP_PORT", "tcp://10.43.18.192:8765")

    assert _port_from_env("SECONDBRAIN_MCP_PORT", 8765) == 8765


def test_port_from_env_prefers_palace_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_MCP_PORT", "9876")
    monkeypatch.setenv("SECONDBRAIN_MCP_PORT", "8765")

    assert _port_from_env(("PALACEOFTRUTH_MCP_PORT", "SECONDBRAIN_MCP_PORT"), 7000) == 9876


def test_streamable_http_transport_security_disables_host_checks_for_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_MCP_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("SECONDBRAIN_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("SECONDBRAIN_MCP_ALLOWED_ORIGINS", raising=False)

    security = _streamable_http_transport_security("0.0.0.0")

    assert security.enable_dns_rebinding_protection is False


def test_streamable_http_transport_security_respects_explicit_allowlists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_MCP_ALLOWED_HOSTS", "mcp.palaceoftruth.test")
    monkeypatch.setenv("SECONDBRAIN_MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv(
        "PALACEOFTRUTH_MCP_ALLOWED_ORIGINS",
        "https://mcp.palaceoftruth.test",
    )

    security = _streamable_http_transport_security("0.0.0.0")

    assert security.enable_dns_rebinding_protection is True
    assert "mcp.palaceoftruth.test" in security.allowed_hosts
    assert "mcp.example.com" not in security.allowed_hosts
    assert "https://mcp.palaceoftruth.test" in security.allowed_origins


def test_connection_resources_share_same_payload() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
            ),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(
                        settings=api.settings,
                        api=api,
                    )
                )
            )
            palace_payload = json.loads(await palace_connection_info(ctx))
            compatibility_payload = json.loads(await connection_info(ctx))

        assert palace_payload == {
            "api_base_url": "https://api.palaceoftruth.test",
            "tenant_id": "tenant-a",
        }
        assert compatibility_payload == palace_payload

    asyncio.run(scenario())


def test_create_memory_entry_uses_authenticated_tenant() -> None:
    seen_paths: list[str] = []
    seen_scope_headers: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        seen_scope_headers.append((request.headers.get("x-mcp-scope"), request.headers.get("x-mcp-scopes")))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            payload = json.loads(request.content.decode())
            assert payload["tenant_id"] == "tenant-a"
            assert payload["scope"] == {"type": "workspace", "key": "launch-pad"}
            assert payload["relationship_policy"] == "deferred"
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": {"type": "workspace", "key": "launch-pad"},
                    "accepted_as": "canonical",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.create_memory_entry(
                title="Shared brief",
                body="Agents should reuse the same launch brief.",
                source="mcp",
                created_at="2026-04-12T12:00:00Z",
                summary=None,
                tags=["launch"],
                scope_type="workspace",
                scope_key="launch-pad",
                source_url=None,
                created_by_role="agent",
                metadata={"ticket_id": "launch-12"},
                idempotency_key=None,
                webhook_url=None,
                enable_ai_enrichment=False,
                relationship_policy="deferred",
            )
            assert result["status"] == "queued"

    asyncio.run(scenario())
    assert seen_paths == ["/api/v1/memory/whoami", "/api/v1/memory/entries"]
    default_scopes = "read,write,write:agent,write:workspace,write:session,admin,local_only,destructive_prohibited"
    assert seen_scope_headers[0] == ("read", default_scopes)
    assert seen_scope_headers[1] == ("write", default_scopes)


def test_capture_checkpoint_normalizes_payload_and_returns_compact_ack() -> None:
    seen: list[tuple[str, str]] = []
    seen_entry: dict | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_entry
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            seen_entry = json.loads(request.content.decode())
            assert seen_entry["tenant_id"] == "tenant-a"
            assert seen_entry["scope"] == {"type": "session", "key": "run-123"}
            assert seen_entry["summary"] == "Checkpoint summary"
            assert seen_entry["relationship_policy"] == "deferred"
            assert seen_entry["idempotency_key"].startswith("checkpoint:")
            assert len(seen_entry["idempotency_key"]) <= 64
            assert seen_entry["tags"] == ["checkpoint", "codex-checkpoint", "checkpoint-precompact", "sar-312"]
            assert "Evidence snippets:" in seen_entry["body"]
            assert "first evidence" in seen_entry["body"]
            assert seen_entry["metadata"]["checkpoint"]["relationship_backfill_requested"] is True
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": {"type": "session", "key": "run-123"},
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000":
            return httpx.Response(
                200,
                json={"job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "queued"},
            )
        if request.url.path == "/api/v1/memory/relationships/backfill":
            assert json.loads(request.content.decode()) == {"limit": 3, "defer_seconds": 0}
            return httpx.Response(202, json={"status": "queued", "queued_relationship_jobs": 2})
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            assert payload["operation"] == "capture_checkpoint"
            assert payload["status"] == "success"
            assert payload["params_summary"]["summary"] == {"redacted": True, "present": True}
            assert payload["params_summary"]["evidence_snippets"] == {"redacted": True, "present": True}
            assert payload["params_summary"]["metadata"] == {"redacted": True, "present": True}
            assert "first evidence" not in json.dumps(payload)
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await capture_checkpoint(
                title="PreCompact checkpoint",
                summary=" Checkpoint summary ",
                evidence_snippets=[" first evidence \n with spacing ", "", "second evidence"],
                ctx=ctx,
                scope_type="session",
                scope_key="run-123",
                checkpoint_kind="precompact",
                tags=["sar-312"],
                metadata={"task_id": "SAR-312"},
                backfill_limit=3,
                backfill_defer_seconds=0,
            )

    result = asyncio.run(scenario())

    assert result["status"] == "queued"
    assert result["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert result["memory_job"] == {"job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "queued"}
    assert result["relationship_backfill"] == {"status": "queued", "queued_relationship_jobs": 2}
    assert seen == [
        ("GET", "/api/v1/memory/whoami"),
        ("POST", "/api/v1/memory/entries"),
        ("GET", "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000"),
        ("POST", "/api/v1/memory/relationships/backfill"),
        ("POST", "/api/v1/memory/mcp/audit"),
    ]
    assert seen_entry is not None


def test_create_memory_entry_returns_duplicate_replay_metadata() -> None:
    replay_payload = {
        "job_id": "550e8400-e29b-41d4-a716-446655440000",
        "status": "completed",
        "contract_status": "completed",
        "replayed": True,
        "source_item_id": "550e8400-e29b-41d4-a716-446655440099",
        "scope": {"type": "workspace", "key": "palaceoftruth"},
        "accepted_as": "canonical",
        "poll_url": "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000",
        "poll_after_seconds": 5,
        "retryable": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(202, json=replay_payload)
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            assert payload["operation"] == "create_memory_entry"
            assert payload["status"] == "success"
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await create_memory_entry(
                title="Duplicate replay",
                body="Same safe memory body.",
                ctx=ctx,
                scope_type="workspace",
                scope_key="palaceoftruth",
                idempotency_key="same-request",
            )

    assert asyncio.run(scenario()) == replay_payload


def test_create_memory_entry_preserves_structured_duplicate_conflict_error() -> None:
    detail = {
        "status": "duplicate_conflict",
        "contract_status": "rejected",
        "conflict_kind": "payload_mismatch",
        "retryable": False,
        "existing_job_id": "550e8400-e29b-41d4-a716-446655440000",
        "existing_source_item_id": "550e8400-e29b-41d4-a716-446655440099",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(409, json={"detail": detail})
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            assert payload["operation"] == "create_memory_entry"
            assert payload["status"] == "error"
            assert payload["error_class"] == "PalaceApiError"
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await create_memory_entry(
                title="Duplicate conflict",
                body="Changed body.",
                ctx=ctx,
                idempotency_key="same-request",
            )

    with pytest.raises(PalaceApiError) as exc_info:
        asyncio.run(scenario())

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == detail


def test_palace_remember_alias_returns_duplicate_replay_metadata() -> None:
    replay_payload = {
        "job_id": "550e8400-e29b-41d4-a716-446655440000",
        "status": "completed",
        "contract_status": "completed",
        "replayed": True,
        "source_item_id": "550e8400-e29b-41d4-a716-446655440099",
        "scope": {"type": "agent", "key": "codex"},
        "accepted_as": "canonical",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            payload = json.loads(request.content.decode())
            assert payload["source"] == "codex"
            assert payload["scope"] == {"type": "agent", "key": "codex"}
            return httpx.Response(202, json=replay_payload)
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await palace_remember(
                title="Duplicate replay",
                body="Same safe memory body.",
                ctx=ctx,
                scope_type="agent",
                scope_key="codex",
                idempotency_key="same-request",
            )

    assert asyncio.run(scenario()) == replay_payload


def test_capture_checkpoint_duplicate_replay_returns_typed_ack_without_new_backfill() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "completed",
                    "contract_status": "completed",
                    "replayed": True,
                    "source_item_id": "550e8400-e29b-41d4-a716-446655440099",
                    "scope": {"type": "agent", "key": "codex"},
                    "accepted_as": "canonical",
                    "poll_url": "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000",
                    "poll_after_seconds": 5,
                    "retryable": False,
                },
            )
        if request.url.path == "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000":
            return httpx.Response(
                200,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "completed",
                    "item_id": "550e8400-e29b-41d4-a716-446655440099",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await capture_checkpoint(
                title="Replay checkpoint",
                summary="Safe summary",
                evidence_snippets=["focused evidence"],
                ctx=ctx,
                scope_type="agent",
                scope_key="codex",
                idempotency_key="checkpoint-replay",
            )

    result = asyncio.run(scenario())

    assert result["status"] == "completed"
    assert result["contract_status"] == "completed"
    assert result["replayed"] is True
    assert result["job_id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert result["source_item_id"] == "550e8400-e29b-41d4-a716-446655440099"
    assert result["memory_job"] == {
        "job_id": "550e8400-e29b-41d4-a716-446655440000",
        "status": "completed",
        "item_id": "550e8400-e29b-41d4-a716-446655440099",
    }
    assert result["relationship_backfill"] == {"queued": False, "reason": "replayed_duplicate"}
    assert ("POST", "/api/v1/memory/relationships/backfill") not in seen


def test_capture_checkpoint_duplicate_replay_skips_read_without_item_pointer() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "completed",
                    "contract_status": "completed",
                    "replayed": True,
                    "scope": {"type": "agent", "key": "codex"},
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await capture_checkpoint(
                title="Replay checkpoint",
                summary="Safe summary",
                evidence_snippets=["focused evidence"],
                ctx=ctx,
                scope_type="agent",
                scope_key="codex",
                idempotency_key="checkpoint-replay",
            )

    result = asyncio.run(scenario())

    assert result["replayed"] is True
    assert result["memory_job"] is None
    assert ("GET", "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000") not in seen


def test_capture_checkpoint_dry_run_preserves_idempotency_without_writing() -> None:
    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url.path))),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await capture_checkpoint(
                title="Stop checkpoint",
                summary="A safe summary",
                evidence_snippets=["non-sensitive exact output"],
                ctx=ctx,
                scope_type="agent",
                scope_key="codex",
                idempotency_key="checkpoint-explicit",
                dry_run=True,
            )

    result = asyncio.run(scenario())

    assert result == {
        "status": "dry_run",
        "accepted": False,
        "would_write": {
            "title": "Stop checkpoint",
            "scope": {"type": "agent", "key": "codex"},
            "tags": ["checkpoint", "codex-checkpoint", "checkpoint-manual"],
            "idempotency_key": "checkpoint-explicit",
            "relationship_policy": "deferred",
            "evidence_snippet_count": 1,
        },
        "relationship_backfill": {"queued": False, "reason": "dry_run"},
    }


def test_capture_checkpoint_idempotency_is_stable_for_same_payload() -> None:
    async def scenario(summary: str, evidence_snippets: list[str]) -> str:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url.path))),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            result = await capture_checkpoint(
                title="Stable checkpoint",
                summary=summary,
                evidence_snippets=evidence_snippets,
                ctx=ctx,
                scope_type="workspace",
                scope_key="palaceoftruth",
                checkpoint_kind="stop",
                source_url="codex://run/run-123",
                created_at="2026-05-09T02:30:00Z",
                dry_run=True,
            )
            return result["would_write"]["idempotency_key"]  # type: ignore[index,return-value]

    first = asyncio.run(scenario("Summary", ["same evidence"]))
    second = asyncio.run(scenario("Summary", ["same evidence"]))
    changed = asyncio.run(scenario("Summary", ["changed evidence"]))

    assert first == second
    assert first != changed


def test_capture_checkpoint_rejects_possible_raw_secret() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url.path))),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await capture_checkpoint(
                title="Unsafe checkpoint",
                summary="A summary",
                evidence_snippets=["Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"],
                ctx=ctx,
                scope_type="workspace",
                scope_key="palaceoftruth",
                dry_run=True,
            )

    with pytest.raises(ValueError, match="possible raw secret"):
        asyncio.run(scenario())


def test_capture_checkpoint_kill_switch_blocks_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CHECKPOINT_CAPTURE_DISABLED", "true")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url.path))),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await capture_checkpoint(
                title="Disabled checkpoint",
                summary="A safe summary",
                evidence_snippets=["safe evidence"],
                ctx=ctx,
                scope_type="workspace",
                scope_key="palaceoftruth",
            )

    with pytest.raises(RuntimeError, match="checkpoint capture is disabled"):
        asyncio.run(scenario())


def test_palace_checkpoint_alias_reuses_checkpoint_safety_defaults() -> None:
    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url.path))),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await palace_checkpoint(
                title="Handoff checkpoint",
                summary="Safe handoff summary",
                evidence_snippets=["Validated MCP alias tests"],
                ctx=ctx,
                dry_run=True,
            )

    result = asyncio.run(scenario())

    assert result["status"] == "dry_run"
    assert result["would_write"]["scope"] == {"type": "agent", "key": "codex"}  # type: ignore[index]
    assert result["would_write"]["relationship_policy"] == "deferred"  # type: ignore[index]


def test_mcp_tool_records_redacted_audit_after_success() -> None:
    seen: list[tuple[str, dict]] = []
    seen_audit_scope_headers: list[tuple[str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": {"type": "tenant_shared"},
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            seen_audit_scope_headers.append((request.headers.get("x-mcp-scope"), request.headers.get("x-mcp-scopes")))
            seen.append((request.url.path, payload))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    client_key="codex-local",
                    client_name="Codex local MCP",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await create_memory_entry(
                title="Launch note",
                body="raw memory body must not be audited",
                ctx=ctx,
                metadata={"secret": "value"},
            )

    asyncio.run(scenario())

    assert len(seen) == 1
    payload = seen[0][1]
    assert payload["client"]["client_key"] == "codex-local"
    assert payload["client"]["metadata"]["auth_mode"] == "api_key"
    assert payload["operation"] == "create_memory_entry"
    assert payload["required_scope"] == "write"
    assert payload["status"] == "success"
    assert payload["params_summary"]["body"] == {"redacted": True, "present": True}
    assert payload["params_summary"]["metadata"] == {"redacted": True, "present": True}
    assert "result_summary" not in payload["params_summary"]
    assert "raw memory body" not in json.dumps(payload)
    default_scopes = "read,write,write:agent,write:workspace,write:session,admin,local_only,destructive_prohibited"
    assert seen_audit_scope_headers == [("write", default_scopes)]


def test_mcp_tool_denies_missing_write_scope_and_records_audit() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            assert payload["operation"] == "create_memory_entry"
            assert payload["status"] == "denied"
            assert payload["error_class"] == "PermissionError"
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    client_scopes=("read",),
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            with pytest.raises(PermissionError, match="missing write scope"):
                await create_memory_entry(title="Denied", body="secret", ctx=ctx)

    asyncio.run(scenario())
    assert seen_paths == ["/api/v1/memory/mcp/audit"]


def test_mcp_tool_denies_http_caller_missing_write_scope_and_records_audit() -> None:
    seen_paths: list[str] = []
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            audit_payload.update(payload)
            assert payload["operation"] == "create_memory_entry"
            assert payload["status"] == "denied"
            assert payload["params_summary"]["http_client_id"] == "read-only-client"
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    client_scopes=("read", "write"),
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            access_token = AccessToken(
                token="caller-token",
                client_id="read-only-client",
                scopes=["read"],
            )
            context_token = auth_context_var.set(AuthenticatedUser(access_token))
            try:
                with pytest.raises(PermissionError, match="missing write scope"):
                    await create_memory_entry(title="Denied", body="secret", ctx=ctx)
            finally:
                auth_context_var.reset(context_token)

    asyncio.run(scenario())
    assert seen_paths == ["/api/v1/memory/mcp/audit"]
    assert audit_payload["error_class"] == "PermissionError"


def test_palace_remember_alias_uses_configured_default_scope() -> None:
    seen: list[tuple[str, str]] = []
    seen_entry: dict[str, object] = {}
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            seen_entry.update(json.loads(request.content.decode()))
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": {"type": "agent", "key": "codex"},
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    default_scope_type="agent",
                    default_scope_key="iris",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_remember(
                title="Codex operating rule",
                body="Use Palace as the primary memory path.",
                ctx=ctx,
                tags=["codex-memory"],
            )

    asyncio.run(scenario())

    assert seen == [
        ("GET", "/api/v1/memory/whoami"),
        ("POST", "/api/v1/memory/entries"),
        ("POST", "/api/v1/memory/mcp/audit"),
    ]
    assert seen_entry["source"] == "codex"
    assert seen_entry["scope"] == {"type": "agent", "key": "iris"}
    assert seen_entry["created_by_role"] == "agent"
    assert seen_entry["relationship_policy"] == "immediate"
    assert audit_payload["operation"] == "create_memory_entry"
    assert "Use Palace as the primary memory path" not in json.dumps(audit_payload)


def test_palace_remember_alias_without_configured_scope_defaults_to_tenant_shared() -> None:
    seen_entry: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            payload = json.loads(request.content.decode())
            seen_entry.update(payload)
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": payload["scope"],
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_remember(
                title="Shared note",
                body="No adapter default means tenant_shared.",
                ctx=ctx,
            )

    asyncio.run(scenario())

    assert seen_entry["scope"] == {"type": "tenant_shared"}


def test_create_memory_entry_uses_configured_default_scope_when_omitted() -> None:
    seen_entries: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            payload = json.loads(request.content.decode())
            seen_entries.append(payload)
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": payload["scope"],
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    default_scope_type="agent",
                    default_scope_key="karen",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await create_memory_entry(title="Defaulted", body="Uses adapter defaults.", ctx=ctx)
            await create_memory_entry(
                title="Shared",
                body="Explicit tenant_shared remains shared.",
                ctx=ctx,
                scope_type="tenant_shared",
            )

    asyncio.run(scenario())

    assert [entry["scope"] for entry in seen_entries] == [
        {"type": "agent", "key": "karen"},
        {"type": "tenant_shared"},
    ]


def test_create_memory_entry_requires_scope_type_when_only_scope_key_is_provided() -> None:
    async def scenario() -> None:
        api = SecondBrainApiClient(
            SecondBrainMcpSettings(api_base_url="https://api.palaceoftruth.test", api_key="secret"),
            client=httpx.AsyncClient(
                base_url="https://api.palaceoftruth.test",
                transport=httpx.MockTransport(lambda request: httpx.Response(500)),
            ),
        )
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api))
        )
        with pytest.raises(ValueError, match="scope_key must be omitted"):
            await create_memory_entry(title="Invalid", body="missing scope type", ctx=ctx, scope_key="karen")
        await api.aclose()

    asyncio.run(scenario())


def test_palace_remember_preserves_scope_key_only_agent_fallback() -> None:
    seen_entry: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            seen_entry.update(json.loads(request.content.decode()))
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "scope": {"type": "agent", "key": "alice"},
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(201, json={"status": "recorded"})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(api_base_url="https://api.palaceoftruth.test", api_key="secret"),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_remember(title="Agent note", body="Scope key only.", ctx=ctx, scope_key="alice")

    asyncio.run(scenario())

    assert seen_entry["scope"] == {"type": "agent", "key": "alice"}


def test_get_wakeup_brief_calls_memory_facade_with_scope() -> None:
    seen_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memory/wakeup-brief"
        seen_query.update(dict(request.url.params.multi_items()))
        return httpx.Response(
            200,
            json={
                "source_item_id": "550e8400-e29b-41d4-a716-446655440000",
                "title": "Wake-up Brief 2026-04-23 [wing:product-growth]",
                "summary": "Startup context for product growth.",
                "body": "Current body",
                "source_url": "memory://wakeup-brief/wing/product-growth/2026-04-23",
                "day": "2026-04-23",
                "scope_type": "wing",
                "scope_key": "product-growth",
                "generation": 7,
                "indexed_generation": 8,
                "freshness": "stale",
                "stale": True,
                "room_count": 3,
                "diary_count": 2,
                "fact_count": 5,
                "updated_at": "2026-04-23T06:00:00Z",
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.get_wakeup_brief(
                scope_type="wing",
                scope_key="product-growth",
            )
            assert result["freshness"] == "stale"

    asyncio.run(scenario())
    assert seen_query == {"scope_type": "wing", "scope_key": "product-growth"}


def test_list_memory_jobs_calls_memory_facade_with_filters() -> None:
    seen_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memory/jobs"
        seen_query.update(dict(request.url.params.multi_items()))
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "job_id": "550e8400-e29b-41d4-a716-446655440000",
                        "status": "failed",
                        "error_message": "embedding provider timeout",
                        "duplicate_of": None,
                        "created_at": "2026-04-12T15:00:00Z",
                        "completed_at": "2026-04-12T15:00:06Z",
                    }
                ],
                "total": 1,
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.list_memory_jobs(
                status="failed",
                page=2,
                per_page=50,
            )
            assert result["total"] == 1
            assert result["jobs"][0]["status"] == "failed"

    asyncio.run(scenario())
    assert seen_query == {"page": "2", "per_page": "50", "status": "failed"}


def test_list_memory_entries_calls_scoped_memory_listing_with_filters() -> None:
    seen_query: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/memory/entries"
        seen_query.update(dict(request.url.params.multi_items()))
        assert request.url.params.get_list("tags") == ["launch", "agent-memory"]
        return httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "source_item_id": "550e8400-e29b-41d4-a716-446655440000",
                        "title": "Launch brief",
                        "summary": "Cross-host launch context.",
                        "source": "mcp",
                        "source_url": None,
                        "scope": {"type": "agent", "key": "codex"},
                        "tags": ["launch", "agent-memory"],
                        "created_at": "2026-04-12T12:00:00Z",
                        "updated_at": "2026-04-12T12:03:00Z",
                        "readiness_state": "ready",
                        "job_id": None,
                        "job_status": None,
                    }
                ],
                "total": 1,
                "limit": 10,
                "next_cursor": None,
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.list_memory_entries(
                scope_type="agent",
                scope_key="codex",
                tags=["launch", "agent-memory"],
                tags_mode="all",
                limit=10,
                cursor="2026-04-13T12:00:00Z",
            )
            assert result["total"] == 1

    asyncio.run(scenario())
    assert seen_query == {
        "scope_type": "agent",
        "scope_key": "codex",
        "tags": "agent-memory",
        "tags_mode": "all",
        "limit": "10",
        "cursor": "2026-04-13T12:00:00Z",
    }


def test_list_memory_entries_tool_validates_scope_shape_before_calling_rest() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(
                        settings=api.settings,
                        api=api,
                    )
                )
            )
            with pytest.raises(ValueError, match="scope_key is required"):
                await list_memory_entries(ctx, scope_type="workspace", scope_key=None)

    asyncio.run(scenario())


def test_list_memory_scopes_calls_read_only_scope_summary() -> None:
    seen_query: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/memory/scopes"
        seen_query.update(dict(request.url.params.items()))
        return httpx.Response(
            200,
            json={
                "scopes": [
                    {
                        "scope": {"type": "workspace", "key": "exampleos"},
                        "entry_count": 2,
                        "latest_created_at": "2026-05-06T12:00:00Z",
                        "latest_updated_at": "2026-05-06T12:05:00Z",
                        "tags": ["codex-memory"],
                        "sources": ["codex"],
                    }
                ],
                "total": 1,
                "limit": 25,
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.list_memory_scopes(limit=25, sample_limit=4)
            assert result["scopes"][0]["scope"] == {"type": "workspace", "key": "exampleos"}

    asyncio.run(scenario())
    assert seen_query == {"limit": "25", "sample_limit": "4"}


def test_backfill_deferred_relationships_tool_posts_operator_request() -> None:
    seen_payload: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/memory/relationships/backfill"
        seen_payload.update(json.loads(request.content.decode()))
        return httpx.Response(
            202,
            json={
                "status": "queued",
                "tenant_id": "tenant-a",
                "limit": 25,
                "defer_seconds": 3,
                "lease_key": "singleton:backfill-deferred-relationships-tenant-a",
                "lease_holder": "singleton:backfill-deferred-relationships-tenant-a",
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(
                        settings=api.settings,
                        api=api,
                    )
                )
            )
            result = await backfill_deferred_relationships(
                ctx,
                limit=25,
                defer_seconds=3,
            )
            assert result["status"] == "queued"
            assert result["tenant_id"] == "tenant-a"
            assert result["lease_key"] == "singleton:backfill-deferred-relationships-tenant-a"

    asyncio.run(scenario())
    assert seen_payload == {"limit": 25, "defer_seconds": 3}


def test_graph_and_fact_tools_use_bounded_read_only_rest_surfaces() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []
    item_id = "550e8400-e29b-41d4-a716-446655440000"
    room_id = "550e8400-e29b-41d4-a716-446655440010"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params.multi_items())))
        if request.url.path == "/api/v1/graph":
            assert request.url.params["item_id"] == item_id
            assert request.url.params["node_limit"] == "25"
            assert request.url.params["edge_limit"] == "50"
            return httpx.Response(
                200,
                json={
                    "nodes": [
                        {
                            "id": item_id,
                            "title": "Launch context",
                            "source_type": "note",
                            "tags": ["launch"],
                        }
                    ],
                    "edges": [],
                    "meta": {"orphaned_ready_items": 1},
                },
            )
        if request.url.path == f"/api/v1/items/{item_id}/related":
            return httpx.Response(200, json={"relationships": []})
        if request.url.path == "/api/v1/palace/facts":
            assert request.url.params["current_only"] == "true"
            assert request.url.params["limit"] == "10"
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "550e8400-e29b-41d4-a716-446655440001",
                        "source_item_id": item_id,
                        "source_item_title": "Launch context",
                        "subject": "Palace",
                        "predicate": "supports",
                        "object_text": "agent memory",
                        "confidence": 0.95,
                        "status": "active",
                        "valid_from": None,
                        "valid_to": None,
                        "extracted_at": "2026-05-05T12:00:00Z",
                        "superseded_at": None,
                    }
                ],
            )
        if request.url.path == f"/api/v1/palace/rooms/{room_id}":
            return httpx.Response(
                200,
                json={
                    "room": {
                        "id": room_id,
                        "wing_id": "550e8400-e29b-41d4-a716-446655440011",
                        "name": "Launch",
                        "stable_key": "default:launch",
                        "state": "active",
                        "item_count": 1,
                        "summary": "Launch material",
                        "membership_status": {"status": "fresh", "generation": 1, "target_generation": 1, "message": "fresh"},
                        "snapshot_status": {"status": "fresh", "generation": 1, "target_generation": 1, "message": "fresh"},
                        "tunnel_status": {"status": "fresh", "generation": 1, "target_generation": 1, "message": "fresh"},
                        "redirect_room_id": None,
                    },
                    "wing_name": "Default",
                    "banner": None,
                    "representative_items": [],
                    "tunnels": [],
                    "memberships": [],
                    "redirect_target": None,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440099",
                    "client_id": "550e8400-e29b-41d4-a716-446655440098",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            assert (await get_graph(ctx, item_id=item_id, node_limit=25, edge_limit=50))["nodes"][0]["id"] == item_id
            assert await get_item_relationships(item_id=item_id, ctx=ctx) == {"relationships": []}
            assert (await list_temporal_facts(ctx, current_only=True, limit=10))[0]["source_item_id"] == item_id
            assert (await get_palace_room(room_id=room_id, ctx=ctx))["room"]["id"] == room_id

    asyncio.run(scenario())
    assert [entry[1] for entry in seen] == [
        "/api/v1/graph",
        "/api/v1/memory/mcp/audit",
        f"/api/v1/items/{item_id}/related",
        "/api/v1/memory/mcp/audit",
        "/api/v1/palace/facts",
        "/api/v1/memory/mcp/audit",
        f"/api/v1/palace/rooms/{room_id}",
        "/api/v1/memory/mcp/audit",
    ]


def test_answer_audit_tool_calls_read_endpoint_and_records_audit() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []
    claim_id = "550e8400-e29b-41d4-a716-446655440001"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params.multi_items())))
        if request.url.path == "/api/v1/palace/answers/audit":
            assert request.url.params["claim_id"] == claim_id
            assert request.url.params["status"] == "active"
            assert request.url.params["limit"] == "10"
            return httpx.Response(
                200,
                json={
                    "tenant_id": "tenant-a",
                    "audit_scope": "decision_claims",
                    "items": [
                        {
                            "object_type": "decision_claim",
                            "object_id": claim_id,
                            "object_key": "decision:answer-audit",
                            "object_text": "Use compact answer audit state.",
                            "claim_type": "decision",
                            "claim_status": "active",
                            "support_state": "source_backed",
                            "audit_state": "curated",
                            "warning": None,
                            "promotion_status": "promoted",
                            "source_count": 1,
                            "sources": [
                                {
                                    "source_record_id": "550e8400-e29b-41d4-a716-446655440002",
                                    "source_chunk_id": "550e8400-e29b-41d4-a716-446655440003",
                                    "source_item_id": "550e8400-e29b-41d4-a716-446655440004",
                                    "source_record_status": "active",
                                    "support_role": "supports",
                                    "support_status": "current",
                                    "source_digest": "digest-a",
                                    "source_span": {"source_chunk_digest": "digest-a"},
                                }
                            ],
                            "metadata": {"review_action": "promote"},
                        }
                    ],
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            assert payload["operation"] == "get_answer_audit"
            assert "chunk_text" not in json.dumps(payload)
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440099",
                    "client_id": "550e8400-e29b-41d4-a716-446655440098",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            result = await get_answer_audit(ctx, claim_id=claim_id, status="active", limit=10)
            assert result["items"][0]["audit_state"] == "curated"

    asyncio.run(scenario())
    assert [entry[1] for entry in seen] == [
        "/api/v1/palace/answers/audit",
        "/api/v1/memory/mcp/audit",
    ]


def test_graph_and_room_tools_validate_uuid_text_before_rest_calls() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            with pytest.raises(ValueError, match="item_id must be a UUID"):
                await get_item_relationships(item_id="not-a-uuid", ctx=ctx)
            with pytest.raises(ValueError, match="room_id must be a UUID"):
                await get_palace_room(room_id="not-a-uuid", ctx=ctx)

    asyncio.run(scenario())


def test_retrieve_memory_trajectory_tool_forwards_scoped_request() -> None:
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/trajectory":
            payload = json.loads(request.content.decode())
            seen.append((request.method, request.url.path, payload))
            assert payload["query"] == "how did deploy status change?"
            assert payload["trajectory_subject"] == "deploy status"
            assert payload["agent_scope_key"] == "codex"
            assert payload["include_broad_corpus"] is False
            assert payload["tags"] == ["release"]
            return httpx.Response(
                200,
                json={
                    "query": payload["query"],
                    "trajectory_subject": payload["trajectory_subject"],
                    "scopes": [{"type": "agent", "key": "codex"}],
                    "trace": {"searched_scopes": [{"type": "agent", "key": "codex"}]},
                    "entries": [
                        {
                            "item_id": "550e8400-e29b-41d4-a716-446655440001",
                            "title": "Conversation fact: Andrew said",
                            "subject": "Andrew",
                            "predicate": "said",
                            "object_text": "Deploy is ready.",
                            "trajectory_key": "deploy status",
                            "status": "current",
                            "event_time": "2026-05-02T12:00:00Z",
                            "source_item_id": "550e8400-e29b-41d4-a716-446655440000",
                            "source_span": {"line_start": 2, "line_end": 2},
                            "retrieved_scope_label": "agent/codex",
                            "score": 0.91,
                        }
                    ],
                    "current_entries": [],
                    "total": 1,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440099",
                    "client_id": "550e8400-e29b-41d4-a716-446655440098",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            result = await retrieve_memory_trajectory(
                query="how did deploy status change?",
                ctx=ctx,
                trajectory_subject="deploy status",
                agent_scope_key="codex",
                include_broad_corpus=False,
                tags=["release"],
            )
            assert result["entries"][0]["object_text"] == "Deploy is ready."

    asyncio.run(scenario())
    assert seen == [
        (
            "POST",
            "/api/v1/memory/trajectory",
            {
                "query": "how did deploy status change?",
                "trajectory_subject": "deploy status",
                "agent_scope_key": "codex",
                "include_agent_scope_keys": [],
                "include_all_permitted_agent_scopes": False,
                "access_reason": None,
                "workspace_scope_keys": [],
                "session_scope_key": None,
                "include_tenant_shared": True,
                "tenant_shared_policy": "always",
                "include_broad_corpus": False,
                "broad_corpus_policy": "disabled",
                "workspace_strict": False,
                "limit": 10,
                "candidate_limit": None,
                "display_limit": None,
                "context_budget_chars": None,
                "tags": ["release"],
                "tags_mode": "any",
                "min_score": None,
                "date_from": None,
                "date_to": None,
            },
        )
    ]


def test_graph_tool_denies_missing_read_scope_and_records_audit() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/v1/memory/mcp/audit":
            payload = json.loads(request.content.decode())
            assert payload["operation"] == "get_graph"
            assert payload["required_scope"] == "read"
            assert payload["status"] == "denied"
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    client_scopes=("write",),
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            with pytest.raises(PermissionError, match="missing read scope"):
                await get_graph(ctx)

    asyncio.run(scenario())
    assert seen_paths == ["/api/v1/memory/mcp/audit"]


def test_retrieve_agent_memory_posts_policy_request() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "scopes": [
                    {"type": "agent", "key": "orchestrator"},
                    {"type": "workspace", "key": "exampleos"},
                    {"type": "tenant_shared"},
                ],
                "trace": {
                    "searched_scopes": [
                        {"type": "agent", "key": "orchestrator"},
                        {"type": "workspace", "key": "exampleos"},
                        {"type": "tenant_shared"},
                    ],
                    "broad_corpus_searched": True,
                    "excluded_scope_types": ["agent", "workspace", "session"],
                    "fallback_used": False,
                    "completeness_warnings": [],
                },
                "results": [],
                "total": 0,
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.retrieve_agent_memory(
                query="exampleos memory",
                agent_scope_key="orchestrator",
                include_agent_scope_keys=["security-agent"],
                include_agent_scope_patterns=["agent/*"],
                agent_scope_pattern_limit=3,
                include_all_permitted_agent_scopes=True,
                access_reason="assemble delegated agent context",
                workspace_scope_keys=["exampleos"],
                session_scope_key=None,
                include_tenant_shared=True,
                include_broad_corpus=True,
                limit=5,
                candidate_limit=20,
                broad_candidate_limit=30,
                display_limit=8,
                context_budget_chars=4000,
                tags=["codex-memory"],
                tags_mode="all",
                min_score=None,
                date_from=None,
                date_to=None,
                tenant_shared_policy="fallback_only",
                broad_corpus_policy="enabled",
                workspace_strict=True,
            )
            assert result["trace"]["excluded_scope_types"] == ["agent", "workspace", "session"]

    asyncio.run(scenario())
    assert seen_payload["agent_scope_key"] == "orchestrator"
    assert seen_payload["include_agent_scope_keys"] == ["security-agent"]
    assert seen_payload["include_agent_scope_patterns"] == ["agent/*"]
    assert seen_payload["agent_scope_pattern_limit"] == 3
    assert seen_payload["include_all_permitted_agent_scopes"] is True
    assert seen_payload["access_reason"] == "assemble delegated agent context"
    assert seen_payload["workspace_scope_keys"] == ["exampleos"]
    assert seen_payload["tenant_shared_policy"] == "fallback_only"
    assert seen_payload["include_broad_corpus"] is True
    assert seen_payload["broad_corpus_policy"] == "enabled"
    assert seen_payload["workspace_strict"] is True
    assert seen_payload["candidate_limit"] == 20
    assert seen_payload["broad_candidate_limit"] == 30
    assert seen_payload["display_limit"] == 8
    assert seen_payload["context_budget_chars"] == 4000
    assert seen_payload["tags_mode"] == "all"


def test_palace_search_alias_posts_agent_memory_request_with_codex_defaults() -> None:
    seen: list[tuple[str, str]] = []
    seen_payload: dict[str, object] = {}
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/retrieve-agent":
            seen_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "scopes": [{"type": "agent", "key": "codex"}],
                    "trace": {
                        "searched_scopes": [
                            {"type": "agent", "key": "codex"},
                            {"type": "agent", "key": "security-agent"},
                        ],
                        "caller_agent_scope_key": "codex",
                        "requested_agent_scope_keys": ["security-agent"],
                        "authorized_agent_scope_keys": ["security-agent"],
                        "denied_agent_scope_keys": [],
                        "delegated_agent_decision": "allowed",
                        "access_reason_present": True,
                        "result_counts_by_scope": {
                            "agent/codex": 0,
                            "agent/security-agent": 1,
                        },
                        "broad_corpus_searched": False,
                        "context_budget_chars": 4000,
                    },
                    "results": [
                        {
                            "item_id": "00000000-0000-0000-0000-000000000031",
                            "title": "Security specialist memory",
                            "summary": "secret summary must not enter audit",
                            "source_type": "note",
                            "source_url": None,
                            "tags": ["scope-agent", "agent-security-agent"],
                            "system_tags": ["scope-agent", "agent-security-agent"],
                            "semantic_tags": [],
                            "retrieved_scope_type": "agent",
                            "retrieved_scope_key": "security-agent",
                            "retrieved_scope_label": "agent/security-agent",
                            "created_at": "2026-05-23T00:00:00Z",
                            "chunk_text": "raw specialist memory body must not enter audit",
                            "chunk_index": 0,
                            "score": 0.91,
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_search(
                query="Palace memory conventions",
                ctx=ctx,
                include_agent_scope_keys=["security-agent"],
                include_all_permitted_agent_scopes=True,
                access_reason="assemble governed memory briefing",
                workspace_scope_keys=["palaceoftruth"],
                tags=["codex-memory"],
            )

    asyncio.run(scenario())

    assert seen == [
        ("POST", "/api/v1/memory/retrieve-agent"),
        ("POST", "/api/v1/memory/mcp/audit"),
    ]
    assert seen_payload["agent_scope_key"] == "codex"
    assert seen_payload["include_agent_scope_keys"] == ["security-agent"]
    assert seen_payload["include_all_permitted_agent_scopes"] is True
    assert seen_payload["access_reason"] == "assemble governed memory briefing"
    assert seen_payload["workspace_scope_keys"] == ["palaceoftruth"]
    assert seen_payload["include_tenant_shared"] is True
    assert seen_payload["tenant_shared_policy"] == "always"
    assert seen_payload["include_broad_corpus"] is False
    assert seen_payload["broad_corpus_policy"] == "default"
    assert seen_payload["workspace_strict"] is False
    assert seen_payload["tags"] == ["codex-memory"]
    assert audit_payload["operation"] == "retrieve_agent_memory"
    assert audit_payload["params_summary"]["query"] == {"redacted": True, "present": True}
    assert "audit_request_id" in audit_payload["params_summary"]
    result_summary = audit_payload["params_summary"]["result_summary"]
    assert result_summary["total"] == 1
    assert result_summary["returned_result_count"] == 1
    assert result_summary["returned_scope_labels"] == ["agent/security-agent"]
    assert result_summary["trace"]["caller_agent_scope_key"] == "codex"
    assert result_summary["trace"]["requested_agent_scope_keys"] == ["security-agent"]
    assert result_summary["trace"]["authorized_agent_scope_keys"] == ["security-agent"]
    assert result_summary["trace"]["result_counts_by_scope"] == {
        "agent/codex": 0,
        "agent/security-agent": 1,
    }
    assert result_summary["trace"]["context_budget_chars"] == 4000
    assert "raw specialist memory body" not in json.dumps(audit_payload)
    assert "secret summary" not in json.dumps(audit_payload)


def test_palace_search_alias_uses_configured_default_agent_scope() -> None:
    seen_payload: dict[str, object] = {}
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/retrieve-agent":
            seen_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "scopes": [
                        {"type": "agent", "key": "clara"},
                        {"type": "workspace", "key": "hermes"},
                    ],
                    "trace": {
                        "searched_scopes": [
                            {"type": "agent", "key": "clara"},
                            {"type": "workspace", "key": "hermes"},
                        ],
                        "caller_agent_scope_key": "clara",
                        "broad_corpus_searched": False,
                    },
                    "results": [],
                    "total": 0,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    default_scope_type="agent",
                    default_scope_key="clara",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_search(
                query="clara-memory-mcp-canary-20260707T234902Z",
                ctx=ctx,
                workspace_scope_keys=["hermes"],
                include_tenant_shared=True,
            )

    asyncio.run(scenario())

    assert seen_payload["agent_scope_key"] == "clara"
    assert seen_payload["workspace_scope_keys"] == ["hermes"]
    assert seen_payload["include_tenant_shared"] is True
    assert audit_payload["params_summary"]["agent_scope_key"] == "clara"


def test_palace_search_alias_explicit_agent_scope_overrides_configured_default() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/retrieve-agent":
            seen_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "scopes": [{"type": "agent", "key": "karen"}],
                    "trace": {
                        "searched_scopes": [{"type": "agent", "key": "karen"}],
                        "caller_agent_scope_key": "karen",
                    },
                    "results": [],
                    "total": 0,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    default_scope_type="agent",
                    default_scope_key="clara",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_search(
                query="karen-memory-mcp-canary-20260707T234902Z",
                ctx=ctx,
                agent_scope_key="karen",
                workspace_scope_keys=["hermes"],
            )

    asyncio.run(scenario())

    assert seen_payload["agent_scope_key"] == "karen"
    assert seen_payload["workspace_scope_keys"] == ["hermes"]


def test_palace_search_alias_can_request_strict_workspace_memory() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/retrieve-agent":
            seen_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "scopes": [{"type": "workspace", "key": "project-b"}],
                    "trace": {
                        "searched_scopes": [{"type": "workspace", "key": "project-b"}],
                        "workspace_strict": True,
                        "tenant_shared_policy": "fallback_only",
                        "tenant_shared_fallback_used": False,
                        "broad_corpus_policy": "disabled",
                        "broad_corpus_searched": False,
                    },
                    "results": [],
                    "total": 0,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await palace_search(
                query="project status",
                ctx=ctx,
                workspace_scope_keys=["project-b"],
                include_tenant_shared=True,
                tenant_shared_policy="fallback_only",
                include_broad_corpus=False,
                broad_corpus_policy="disabled",
                workspace_strict=True,
            )

    asyncio.run(scenario())

    assert seen_payload["agent_scope_key"] == "codex"
    assert seen_payload["workspace_scope_keys"] == ["project-b"]
    assert seen_payload["workspace_strict"] is True
    assert seen_payload["tenant_shared_policy"] == "fallback_only"
    assert seen_payload["include_broad_corpus"] is False
    assert seen_payload["broad_corpus_policy"] == "disabled"


def test_palace_context_alias_loads_wakeup_and_recent_memory() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params.multi_items())))
        if request.url.path == "/api/v1/memory/wakeup-brief":
            return httpx.Response(200, json={"freshness": "fresh", "stale": False})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(
                200,
                json={
                    "entries": [],
                    "total": 0,
                    "limit": 3,
                    "next_cursor": None,
                },
            )
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await palace_context(ctx=ctx, limit=3, tags=["codex-memory"])

    result = asyncio.run(scenario())

    assert result == {
        "wakeup_brief": {"freshness": "fresh", "stale": False},
        "recent_memory": {"entries": [], "total": 0, "limit": 3, "next_cursor": None},
    }
    assert seen == [
        ("GET", "/api/v1/memory/wakeup-brief", {"scope_type": "tenant"}),
        ("POST", "/api/v1/memory/mcp/audit", {}),
        (
            "GET",
            "/api/v1/memory/entries",
            {"scope_type": "agent", "scope_key": "codex", "tags": "codex-memory", "tags_mode": "any", "limit": "3"},
        ),
        ("POST", "/api/v1/memory/mcp/audit", {}),
    ]


def test_get_wakeup_context_returns_compact_session_start_package() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []
    audit_payload: dict[str, object] = {}
    entry_item_id = "550e8400-e29b-41d4-a716-446655440011"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params.multi_items())))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"tenant_id": "tenant-a", "auth_mode": "mcp_oauth"})
        if request.url.path == "/api/v1/memory/wakeup-brief":
            return httpx.Response(
                200,
                json={
                    "freshness": "fresh",
                    "stale": False,
                    "summary": "Ready",
                    "body": "wakeup body must not leak",
                    "source_trust": {
                        "item_id": "550e8400-e29b-41d4-a716-446655440010",
                        "state": "source_backed",
                        "source_status": "active",
                        "chunk_count": 2,
                        "source_url": "https://example.test/source",
                        "preview": "trust preview must not leak",
                        "body": "trust body must not leak",
                        "chunk_text": "trust chunk text must not leak",
                    },
                },
            )
        if request.url.path == "/api/v1/memory/entries":
            params = dict(request.url.params.multi_items())
            tags = params.get("tags")
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "id": "entry-1",
                            "source_item_id": entry_item_id,
                            "title": "Current local Palace MCP profile",
                            "summary": "Use the repo-owned stdio adapter.",
                            "body": "raw body must not leak",
                            "preview": "preview must not leak",
                            "source_trust": {
                                "state": "source_backed",
                                "body": "entry trust body must not leak",
                                "chunk_text": "entry trust chunk must not leak",
                                "preview": "entry trust preview must not leak",
                            },
                            "scope": {"type": params["scope_type"], "key": params.get("scope_key")},
                            "source": "codex",
                            "source_url": "https://example.test/source",
                            "tags": [tags] if tags else ["codex-memory"],
                        }
                    ],
                    "total": 1,
                    "limit": int(params["limit"]),
                    "next_cursor": None,
                },
            )
        if request.url.path == "/api/v1/memory/source-trust-summaries":
            payload = json.loads(request.content.decode())
            assert payload == {"item_ids": [entry_item_id]}
            return httpx.Response(
                200,
                json={
                    "summaries": [
                        {
                            "item_id": entry_item_id,
                            "state": "generated_unpromoted",
                            "warning": "generated_artifact_without_promoted_source_support",
                            "chunk_count": 0,
                        }
                    ]
                },
            )
        if request.url.path == "/api/v1/memory/jobs":
            return httpx.Response(200, json={"jobs": [{"job_id": "job-1", "status": "complete"}], "total": 1})
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await get_wakeup_context(
                ctx=ctx,
                workspace_scope_keys=["palaceoftruth"],
                session_scope_key="test-session-key",
                memory_limit_per_scope=2,
                checkpoint_limit_per_scope=1,
            )

    result = asyncio.run(scenario())

    assert result["schema_version"] == 1
    assert result["tenant"] == {"tenant_id": "tenant-a", "auth_mode": "mcp_oauth"}
    assert result["readiness"]["status"] == "ready"
    assert result["wakeup_brief"]["source_trust"]["state"] == "source_backed"
    assert "preview" not in json.dumps(result["wakeup_brief"]["source_trust"])
    assert "body" not in json.dumps(result["wakeup_brief"])
    assert result["privacy"]["raw_memory_bodies_included"] is False
    response_json = json.dumps(result)
    assert "body" not in response_json
    assert "preview" not in response_json
    assert "chunk_text" not in response_json
    assert result["scope_summaries"][0]["entries"][0]["source_item_id"] == entry_item_id
    assert result["scope_summaries"][0]["entries"][0]["source_trust"]["state"] == "generated_unpromoted"
    assert result["scope_summaries"][0]["source_trust"] == {
        "status": "ok",
        "entry_count": 1,
        "attached_count": 1,
    }
    assert result["checkpoint_pointers"][0]["tags"] == ["checkpoint"]
    assert result["follow_up_probes"][0]["tool"] == "palace_search"
    assert audit_payload["operation"] == "get_wakeup_context"
    assert audit_payload["required_scope"] == "read"
    assert seen[-1] == ("POST", "/api/v1/memory/mcp/audit", {})


def test_get_wakeup_context_returns_partial_warning_when_source_trust_unavailable() -> None:
    entry_item_id = "550e8400-e29b-41d4-a716-446655440012"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"tenant_id": "tenant-a", "auth_mode": "mcp_oauth"})
        if request.url.path == "/api/v1/memory/wakeup-brief":
            return httpx.Response(
                200,
                json={"freshness": "fresh", "stale": False, "summary": "Ready", "body": "wakeup body must not leak"},
            )
        if request.url.path == "/api/v1/memory/entries":
            params = dict(request.url.params.multi_items())
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "id": "entry-1",
                            "source_item_id": entry_item_id,
                            "title": "Current local Palace MCP profile",
                            "summary": "Use the repo-owned stdio adapter.",
                            "body": "raw body must not leak",
                            "preview": "preview must not leak",
                            "source_trust": {
                                "state": "source_backed",
                                "body": "entry trust body must not leak",
                                "chunk_text": "entry trust chunk must not leak",
                                "preview": "entry trust preview must not leak",
                            },
                            "scope": {"type": params["scope_type"], "key": params.get("scope_key")},
                            "source": "codex",
                            "tags": [],
                        }
                    ],
                    "total": 1,
                    "limit": int(params["limit"]),
                    "next_cursor": None,
                },
            )
        if request.url.path == "/api/v1/memory/source-trust-summaries":
            return httpx.Response(503, json={"detail": "source trust unavailable"})
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await get_wakeup_context(ctx=ctx, include_recent_jobs=False)

    result = asyncio.run(scenario())

    response_json = json.dumps(result)
    assert result["readiness"]["status"] == "partial"
    assert "source_trust_partial" in result["readiness"]["warnings"]
    assert result["scope_summaries"][0]["source_trust"]["status"] == "error"
    assert result["scope_summaries"][0]["entries"][0]["source_item_id"] == entry_item_id
    assert "source_trust" not in result["scope_summaries"][0]["entries"][0]
    assert "body" not in response_json
    assert "preview" not in response_json
    assert "chunk_text" not in response_json


def test_get_wakeup_context_marks_empty_stale_context_partial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/wakeup-brief":
            return httpx.Response(200, json={"freshness": "stale", "stale": True})
        if request.url.path == "/api/v1/memory/entries":
            return httpx.Response(200, json={"entries": [], "total": 0, "limit": 5, "next_cursor": None})
        if request.url.path == "/api/v1/memory/mcp/audit":
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> dict[str, object]:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            return await get_wakeup_context(ctx=ctx, include_recent_jobs=False)

    result = asyncio.run(scenario())

    assert result["readiness"]["status"] == "ready"
    assert result["readiness"]["stale"] is True
    assert "wakeup_brief_stale" in result["readiness"]["warnings"]
    assert result["readiness"]["empty_scopes"] == ["agent/codex", "tenant_shared"]


def test_get_wakeup_context_requires_read_scope_before_fetching_context() -> None:
    seen: list[str] = []
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "550e8400-e29b-41d4-a716-446655440001",
                    "client_id": "550e8400-e29b-41d4-a716-446655440002",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    client_scopes=("write",),
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await get_wakeup_context(ctx=ctx)

    with pytest.raises(PermissionError, match="missing read scope"):
        asyncio.run(scenario())

    assert seen == ["/api/v1/memory/mcp/audit"]
    assert audit_payload["operation"] == "get_wakeup_context"
    assert audit_payload["status"] == "denied"


def test_retrieval_doctor_api_client_posts_redacted_probe_request() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/memory/retrieval-doctor"
        seen_payload.update(json.loads(request.content.decode()))
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "tenant_id": "tenant-a",
                "selected_scopes": [{"type": "agent", "key": "codex"}],
                "probes": [{"probe_index": 0, "query_fingerprint": "abc123", "scope": {"type": "agent", "key": "codex"}, "status": "ok"}],
                "checks": [{"name": "probe_0", "status": "ok", "reasons": []}],
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                ),
                client=client,
            )
            result = await api.get_retrieval_doctor(
                agent_scope_key="codex",
                workspace_scope_keys=["palaceoftruth"],
                session_scope_key=None,
                include_tenant_shared=True,
                include_broad_corpus=False,
                candidate_limit=10,
                broad_candidate_limit=None,
                display_limit=5,
                context_budget_chars=None,
                sample_probes=[
                    {
                        "query": "sensitive probe text",
                        "scope": {"type": "agent", "key": "codex"},
                        "expected_item_ids": ["00000000-0000-0000-0000-000000000001"],
                    }
                ],
            )
            assert result["status"] == "ok"
            assert "sensitive probe text" not in json.dumps(result)

    asyncio.run(scenario())
    assert seen_payload["agent_scope_key"] == "codex"
    assert seen_payload["workspace_scope_keys"] == ["palaceoftruth"]
    assert seen_payload["sample_probes"][0]["query"] == "sensitive probe text"


def test_retrieval_doctor_tool_records_read_audit_without_raw_probe_text() -> None:
    seen: list[tuple[str, str]] = []
    audit_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/retrieval-doctor":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a", "checks": []})
        if request.url.path == "/api/v1/memory/mcp/audit":
            audit_payload.update(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "audit_event_id": "00000000-0000-0000-0000-000000000002",
                    "client_id": "00000000-0000-0000-0000-000000000003",
                    "tenant_id": "tenant-a",
                    "status": "recorded",
                },
            )
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.palaceoftruth.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.palaceoftruth.test",
                    api_key="secret",
                    client_scopes=("read",),
                ),
                client=client,
            )
            ctx = SimpleNamespace(
                request_context=SimpleNamespace(
                    lifespan_context=SecondBrainMcpRuntime(settings=api.settings, api=api)
                )
            )
            await get_retrieval_doctor(
                ctx,
                agent_scope_key="codex",
                sample_probes=[{"query": "raw sensitive query", "scope": {"type": "agent", "key": "codex"}}],
            )

    asyncio.run(scenario())
    assert seen == [
        ("POST", "/api/v1/memory/retrieval-doctor"),
        ("POST", "/api/v1/memory/mcp/audit"),
    ]
    assert audit_payload["operation"] == "get_retrieval_doctor"
    assert audit_payload["required_scope"] == "read"
    assert audit_payload["status"] == "success"
    assert "raw sensitive query" not in json.dumps(audit_payload)


def test_agent_memory_compatibility_sequence_uses_canonical_memory_facade() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/v1/memory/whoami":
            return httpx.Response(200, json={"status": "ok", "tenant_id": "tenant-a"})
        if request.url.path == "/api/v1/memory/entries":
            payload = json.loads(request.content.decode())
            assert payload["tenant_id"] == "tenant-a"
            assert payload["scope"] == {"type": "agent", "key": "codex"}
            assert payload["relationship_policy"] == "immediate"
            return httpx.Response(
                202,
                json={
                    "job_id": "550e8400-e29b-41d4-a716-446655440000",
                    "status": "queued",
                    "accepted_as": "canonical",
                },
            )
        if request.url.path == "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000":
            return httpx.Response(200, json={"job_id": "550e8400-e29b-41d4-a716-446655440000", "status": "complete"})
        if request.url.path == "/api/v1/memory/retrieve":
            payload = json.loads(request.content.decode())
            assert payload["scope"] == {"type": "agent", "key": "codex"}
            assert payload["tags"] == ["agent-memory-smoke"]
            return httpx.Response(200, json={"results": [{"title": "Compatibility smoke"}], "total": 1})
        if request.url.path == "/api/v1/memory/relationships/backfill":
            payload = json.loads(request.content.decode())
            assert payload == {"limit": 1, "defer_seconds": 0}
            return httpx.Response(
                202,
                json={
                    "status": "queued",
                    "tenant_id": "tenant-a",
                    "limit": 1,
                    "defer_seconds": 0,
                },
            )
        if request.url.path == "/api/v1/memory/wakeup-brief":
            return httpx.Response(200, json={"freshness": "fresh", "stale": False})
        if request.url.path == "/api/v1/memory/jobs":
            return httpx.Response(200, json={"jobs": [], "total": 0})
        raise AssertionError(f"Unexpected path: {request.url.path}")

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            accepted = await api.create_memory_entry(
                title="Compatibility smoke",
                body="Codex and other MCP clients should share this memory flow.",
                source="mcp",
                created_at="2026-04-30T12:00:00Z",
                summary=None,
                tags=["agent-memory-smoke"],
                scope_type="agent",
                scope_key="codex",
                source_url=None,
                created_by_role="agent",
                metadata={"smoke": "agent-memory-compatibility"},
                idempotency_key="agent-memory-smoke:20260430",
                webhook_url=None,
                enable_ai_enrichment=False,
                relationship_policy="immediate",
            )
            await api.get_memory_job(accepted["job_id"])
            await api.retrieve_memory(
                query="compatibility smoke",
                limit=5,
                tags=["agent-memory-smoke"],
                tags_mode="all",
                min_score=None,
                date_from=None,
                date_to=None,
                scope_type="agent",
                scope_key="codex",
                room_id=None,
            )
            await api.backfill_deferred_relationships(limit=1, defer_seconds=0)
            await api.get_wakeup_brief(scope_type="tenant", scope_key=None)
            await api.list_memory_jobs(status=None, page=1, per_page=10)

    asyncio.run(scenario())
    assert seen == [
        ("GET", "/api/v1/memory/whoami"),
        ("POST", "/api/v1/memory/entries"),
        ("GET", "/api/v1/memory/jobs/550e8400-e29b-41d4-a716-446655440000"),
        ("POST", "/api/v1/memory/retrieve"),
        ("POST", "/api/v1/memory/relationships/backfill"),
        ("GET", "/api/v1/memory/wakeup-brief"),
        ("GET", "/api/v1/memory/jobs"),
    ]


def test_list_items_serializes_tag_filters_as_csv() -> None:
    seen_tags: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tags.append(request.url.params.get("tags"))
        return httpx.Response(200, json={"items": [], "total": 0, "page": 1, "per_page": 20})

    async def scenario() -> None:
        async with httpx.AsyncClient(
            base_url="https://api.secondbrain.test",
            transport=httpx.MockTransport(handler),
        ) as client:
            api = SecondBrainApiClient(
                SecondBrainMcpSettings(
                    api_base_url="https://api.secondbrain.test",
                    api_key="secret",
                ),
                client=client,
            )
            await api.list_items(
                page=1,
                per_page=20,
                source_type=None,
                tags=["launch", " founder-note "],
                date_from=None,
                date_to=None,
            )

    asyncio.run(scenario())
    assert seen_tags == ["launch,founder-note"]
