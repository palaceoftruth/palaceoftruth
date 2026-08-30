from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
import threading
import types
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATH = (
    REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth" / "__init__.py"
)


@pytest.fixture(autouse=True)
def isolate_oauth_environment(monkeypatch) -> None:
    """Keep API-key fixtures independent from the host's OAuth configuration."""
    for name in (
        "PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET",
        "PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL",
        "PALACEOFTRUTH_MCP_OAUTH_RESOURCE",
        "PALACEOFTRUTH_MCP_OAUTH_AUDIENCE",
        "PALACEOFTRUTH_MCP_CLIENT_KEY",
        "PALACEOFTRUTH_MCP_CLIENT_SCOPES",
        "SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET",
        "SECONDBRAIN_MCP_OAUTH_TOKEN_URL",
        "SECONDBRAIN_MCP_OAUTH_RESOURCE",
        "SECONDBRAIN_MCP_OAUTH_AUDIENCE",
        "SECONDBRAIN_MCP_CLIENT_KEY",
        "SECONDBRAIN_MCP_CLIENT_SCOPES",
    ):
        monkeypatch.delenv(name, raising=False)


def load_palaceoftruth_plugin():
    agent_pkg = types.ModuleType("agent")
    agent_memory_provider = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    agent_memory_provider.MemoryProvider = MemoryProvider
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.memory_provider"] = agent_memory_provider

    module_name = "palaceoftruth_repo_hermes_memory_plugin"
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load plugin from {PLUGIN_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeJsonResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def request_path(request) -> str:
    parsed = urlparse(request.full_url)
    return parsed.path


def oauth_whoami(agent_scope_key: str) -> dict:
    return {
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "agent_scope_key": agent_scope_key,
        "containment_mode": "hermes_agent",
    }


def test_plugin_files_exist() -> None:
    assert PLUGIN_PATH.exists()
    assert (PLUGIN_PATH.parent / "plugin.yaml").exists()


def test_palaceoftruth_provider_is_available_checks_required_env(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    provider = module.PalaceOfTruthMemoryProvider()

    monkeypatch.delenv("PALACEOFTRUTH_BASE_URL", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    assert provider.is_available() is False

    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    assert provider.is_available() is True


def test_palaceoftruth_provider_is_available_uses_palaceoftruth_json(
    monkeypatch, tmp_path: Path
) -> None:
    module = load_palaceoftruth_plugin()
    provider = module.PalaceOfTruthMemoryProvider()

    monkeypatch.delenv("PALACEOFTRUTH_BASE_URL", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "palaceoftruth.json").write_text(
        '{"base_url":"https://api.palaceoftruth.example.com","api_key":"tenant-key"}',
        encoding="utf-8",
    )

    assert provider.is_available() is True


def test_palaceoftruth_provider_is_available_accepts_oauth_without_api_key(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    provider = module.PalaceOfTruthMemoryProvider()

    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")

    assert provider.is_available() is True


def test_palaceoftruth_provider_mints_oauth_token_and_sends_bearer(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "https://api.palace.test")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "helm-mcp")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_SCOPES", "read,write,write:agent")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    seen: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def fake_urlopen(request, timeout: int):
        seen.append((request.get_method(), request_path(request), request.data, dict(request.headers)))
        if request_path(request).endswith("/oauth/token"):
            return FakeJsonResponse({"access_token": "minted-token", "expires_in": 3600})
        return FakeJsonResponse({"tenant_id": "tenant-a", "auth_mode": "mcp_oauth"})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    result = provider._request_json("GET", "/api/v1/memory/whoami")

    assert result["auth_mode"] == "mcp_oauth"
    assert [path for _, path, _, _ in seen] == [
        "/api/v1/memory/mcp/oauth/token",
        "/api/v1/memory/whoami",
    ]
    token_body = seen[0][2].decode("utf-8") if seen[0][2] else ""
    assert "grant_type=client_credentials" in token_body
    assert "client_id=helm-mcp" in token_body
    assert "client_secret=client-secret" in token_body
    assert "scope=read+write+write%3Aagent" in token_body
    assert "resource=https%3A%2F%2Fapi.palace.test%2Fapi%2Fv1" in token_body
    assert seen[1][3]["Authorization"] == "Bearer minted-token"
    assert "x-api-key" not in {key.lower(): value for key, value in seen[1][3].items()}


def test_palaceoftruth_provider_uses_oauth_resource_env_for_backend_api(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "https://api.palace.test")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_RESOURCE", "https://api.palace.test/api/v1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    bodies: list[str] = []

    def fake_urlopen(request, timeout: int):
        if request_path(request).endswith("/oauth/token"):
            bodies.append(request.data.decode("utf-8"))
            return FakeJsonResponse({"access_token": "minted-token", "expires_in": 3600})
        return FakeJsonResponse({"tenant_id": "tenant-a"})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    provider._request_json("GET", "/api/v1/memory/whoami")

    assert bodies == [
        "grant_type=client_credentials&client_id=default&client_secret=client-secret"
        "&scope=read+write+write%3Aagent+write%3Aworkspace+write%3Asession"
        "&resource=https%3A%2F%2Fapi.palace.test%2Fapi%2Fv1"
    ]


def test_palaceoftruth_provider_derives_https_oauth_resource_for_internal_http_base_url(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    bodies: list[str] = []

    def fake_urlopen(request, timeout: int):
        if request_path(request).endswith("/oauth/token"):
            bodies.append(request.data.decode("utf-8"))
            return FakeJsonResponse({"access_token": "minted-token", "expires_in": 3600})
        return FakeJsonResponse({"tenant_id": "tenant-a"})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    provider._request_json("GET", "/api/v1/memory/whoami")

    assert "resource=https%3A%2F%2Fpalaceoftruth-backend%3A8000%2Fapi%2Fv1" in bodies[0]


def test_palaceoftruth_provider_ignores_legacy_mcp_oauth_resource(
    monkeypatch,
    caplog,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "https://api.palace.test")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_RESOURCE", "https://mcp.palace.test/mcp")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    bodies: list[str] = []

    def fake_urlopen(request, timeout: int):
        if request_path(request).endswith("/oauth/token"):
            bodies.append(request.data.decode("utf-8"))
            return FakeJsonResponse({"access_token": "minted-token", "expires_in": 3600})
        return FakeJsonResponse({"tenant_id": "tenant-a"})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    caplog.set_level(logging.WARNING)

    provider._request_json("GET", "/api/v1/memory/whoami")

    assert "resource=https%3A%2F%2Fapi.palace.test%2Fapi%2Fv1" in bodies[0]
    assert "client-secret" not in caplog.text
    assert "Ignoring legacy MCP OAuth resource" in caplog.text


def test_palaceoftruth_provider_oauth_token_failure_is_secret_safe(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "https://api.palace.test")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    def fake_urlopen(request, timeout: int):
        raise module.URLError("client-secret should not leak")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc_info:
        provider._request_json("GET", "/api/v1/memory/whoami")

    assert str(exc_info.value) == "Palace OAuth token endpoint network failure"
    assert "client-secret" not in str(exc_info.value)


def test_palaceoftruth_provider_retrieve_uses_memory_retrieve_contract(monkeypatch, caplog) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "sterling")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="sterling",
        agent_workspace="sterling",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET" and path == "/api/v1/memory/scopes":
            assert params == {"limit": 100, "sample_limit": 5}
            return {
                "scopes": [
                    {"scope": {"type": "agent", "key": "sterling"}, "entry_count": 2},
                    {"scope": {"type": "workspace", "key": "sterling"}, "entry_count": 1},
                ],
                "total": 2,
                "limit": 100,
            }
        if method == "POST" and path == "/api/v1/memory/retrieve-agent":
            assert payload["agent_scope_key"] == "sterling"
            assert payload["workspace_scope_keys"] == ["sterling"]
            return {
                "trace": {
                    "searched_scopes": [
                        {"type": "agent", "key": "sterling"},
                        {"type": "workspace", "key": "sterling"},
                    ],
                },
                "results": [
                    {
                        "item_id": "item-agent",
                        "title": "Sterling memory",
                        "source_type": "note",
                        "chunk_text": "Remember prior trading constraints.",
                        "score": 0.91,
                    }
                ],
                "total": 1,
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    with caplog.at_level(logging.INFO):
        text = provider.prefetch("position sizing rules", session_id="session-1")

    assert requests_seen == [
        (
            "GET",
            "/api/v1/memory/scopes",
            None,
        ),
        (
            "POST",
            "/api/v1/memory/retrieve-agent",
            {
                    "query": "position sizing rules",
                    "limit": 5,
                    "corpus_class": "raw_capture",
                "candidate_limit": 20,
                "broad_candidate_limit": 20,
                "display_limit": 12,
                "context_budget_chars": 4000,
                "agent_scope_key": "sterling",
                "include_agent_scope_patterns": [],
                "agent_scope_pattern_limit": 5,
                "include_all_permitted_agent_scopes": False,
                "include_all_permitted_workspace_scopes": False,
                "workspace_scope_keys": ["sterling"],
                "include_tenant_shared": False,
                "tenant_shared_policy": "never",
                "include_broad_corpus": False,
                "broad_corpus_policy": "disabled",
                "workspace_strict": True,
            },
        )
    ]
    assert "Retrieval searched scopes: agent/sterling, workspace/sterling." in text
    assert "[0.91] Sterling memory [note]" in text
    assert "Evidence: item_id=item-agent" in text
    assert "item_url=http://palaceoftruth-backend:8000/api/v1/items/item-agent" in text
    assert "event=route_aware_success" in caplog.text
    assert "searched_scope_count" in caplog.text
    assert "agent/sterling" in caplog.text
    assert "result_count" in caplog.text
    assert "position sizing rules" not in caplog.text
    assert "Remember prior trading constraints" not in caplog.text
    assert "tenant-key" not in caplog.text


def test_palaceoftruth_hermes_oauth_default_retrieval_stays_in_canonical_agent_scope(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL",
        "http://palaceoftruth-backend:8000/api/v1/memory/mcp/oauth/token",
    )
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-clara")
    # A stale workspace default must not leak into a bound Hermes OAuth read.
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "workspace")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "hermes")
    # Broad recall alone must not activate delegated retrieval for a bound client.
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_BROAD_CORPUS", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET" and path == "/api/v1/memory/whoami":
            return oauth_whoami("clara")
        if method == "GET" and path == "/api/v1/memory/scopes":
            assert params == {"limit": 100, "sample_limit": 5}
            return {
                "scopes": [
                    {"scope": {"type": "agent", "key": "clara"}, "entry_count": 2},
                    {"scope": {"type": "workspace", "key": "hermes"}, "entry_count": 1},
                    {"scope": {"type": "tenant_shared"}, "entry_count": 1},
                ],
                "total": 3,
                "limit": 100,
            }
        if method == "POST" and path == "/api/v1/memory/retrieve":
            assert payload is not None
            assert payload["scope"] == {"type": "agent", "key": "clara"}
            return {
                "trace": {"fallback_used": False},
                "results": [
                    {
                        "item_id": "clara-memory",
                        "title": "Clara memory",
                        "source_type": "note",
                        "chunk_text": "Canonical agent recall remains available.",
                        "score": 0.9,
                    }
                ],
                "total": 1,
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]

    text = provider.prefetch("agent recall", session_id="session-1")

    assert not any(
        method == "POST" and path == "/api/v1/memory/retrieve-agent"
        for method, path, _ in requests_seen
    )
    assert [
        payload["scope"]
        for method, path, payload in requests_seen
        if method == "POST" and path == "/api/v1/memory/retrieve" and payload
    ] == [{"type": "agent", "key": "clara"}]
    assert "Clara memory" in text


def test_palaceoftruth_hermes_oauth_tenant_shared_uses_constrained_agent_route(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-karen")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "workspace")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "hermes")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_BROAD_CORPUS", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="karen",
        agent_workspace="hermes",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/whoami":
            return oauth_whoami("karen")
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path in {"/api/v1/memory/retrieve-agent", "/api/v1/memory/retrieve"}
        seen_payload.update(payload or {})
        return {"trace": {"searched_scopes": []}, "results": [], "total": 0}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.prefetch("shared operating context", session_id="session-1")

    assert seen_payload["agent_scope_key"] == "karen"
    assert seen_payload["workspace_scope_keys"] == []
    assert "session_scope_key" not in seen_payload
    assert seen_payload["include_tenant_shared"] is True
    assert seen_payload["tenant_shared_policy"] == "fallback_only"
    assert seen_payload["include_broad_corpus"] is False
    assert seen_payload["broad_corpus_policy"] == "disabled"
    assert seen_payload["access_reason"] == (
        "Hermes agent karen recall of operator-configured shared memory scopes."
    )


def test_palaceoftruth_hermes_oauth_mara_sibling_patterns_are_bounded_and_audited(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-mara")
    monkeypatch.setenv(
        "PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS",
        "agent/karen, agent/clara",
    )
    monkeypatch.setenv("PALACEOFTRUTH_AGENT_SCOPE_PATTERN_LIMIT", "2")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="mara",
        agent_workspace="hermes",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/whoami":
            return oauth_whoami("mara")
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path in {"/api/v1/memory/retrieve-agent", "/api/v1/memory/retrieve"}
        seen_payload.update(payload or {})
        return {"trace": {"searched_scopes": []}, "results": [], "total": 0}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.prefetch("agent campaign context", session_id="session-1")

    assert seen_payload["agent_scope_key"] == "mara"
    assert seen_payload["include_agent_scope_patterns"] == [
        "agent/karen",
        "agent/clara",
    ]
    assert seen_payload["agent_scope_pattern_limit"] == 2
    assert seen_payload["workspace_scope_keys"] == []
    assert "session_scope_key" not in seen_payload
    assert seen_payload["include_tenant_shared"] is False
    assert seen_payload["include_broad_corpus"] is False
    assert seen_payload["access_reason"] == (
        "Hermes agent mara recall of operator-configured shared memory scopes."
    )


def test_palaceoftruth_hermes_oauth_delegated_route_fallback_is_canonical_self_only(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-mara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "agent/*")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="mara",
        agent_workspace="hermes",
        platform="discord",
    )

    fallback_scopes: list[dict] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/whoami":
            return oauth_whoami("mara")
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        if path == "/api/v1/memory/retrieve-agent":
            raise RuntimeError("delegated route unavailable")
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve"
        assert payload is not None
        fallback_scopes.append(payload["scope"])
        return {"trace": {"fallback_used": False}, "results": [], "total": 0}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.prefetch("shared context", session_id="session-1")

    assert fallback_scopes == [{"type": "agent", "key": "mara"}]


def test_palaceoftruth_provider_sends_agent_scope_patterns(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "agent/*, agent/sec*")
    monkeypatch.setenv("PALACEOFTRUTH_AGENT_SCOPE_PATTERN_LIMIT", "3")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="palaceoftruth",
        platform="cli",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {"trace": {"searched_scopes": []}, "results": [], "total": 0}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.prefetch("security recovery", session_id="session-1")

    assert seen_payload["workspace_scope_keys"] == ["palaceoftruth"]
    assert seen_payload["workspace_strict"] is False
    assert seen_payload["include_agent_scope_patterns"] == ["agent/*", "agent/sec*"]
    assert seen_payload["agent_scope_pattern_limit"] == 3


def test_palaceoftruth_provider_uses_route_aware_budgets(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "codex")
    monkeypatch.setenv("PALACEOFTRUTH_AGENT_CANDIDATE_LIMIT", "24")
    monkeypatch.setenv("PALACEOFTRUTH_AGENT_BROAD_CANDIDATE_LIMIT", "36")
    monkeypatch.setenv("PALACEOFTRUTH_AGENT_DISPLAY_LIMIT", "9")
    monkeypatch.setenv("PALACEOFTRUTH_CONTEXT_BUDGET_CHARS", "2000")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="codex",
        agent_workspace="exampleos",
        platform="cli",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {
            "trace": {
                "searched_scopes": [
                    {"type": "agent", "key": "codex"},
                    {"type": "workspace", "key": "exampleos"},
                ],
                "selected_scope_candidate_limit": 24,
                "broad_candidate_limit": 36,
                "display_limit": 9,
                "context_budget_chars": 2000,
                "budget_truncated": True,
            },
            "results": [
                {
                    "item_id": f"item-{index}",
                    "title": f"ExampleOS memory {index}",
                    "source_type": "note",
                    "chunk_text": "Relevant workspace context.",
                    "score": 0.9 - (index * 0.01),
                }
                for index in range(7)
            ],
            "total": 7,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("exampleos recall", session_id="session-1")

    assert seen_payload["candidate_limit"] == 24
    assert seen_payload["broad_candidate_limit"] == 36
    assert seen_payload["display_limit"] == 9
    assert seen_payload["context_budget_chars"] == 2000
    assert "Retrieval budgets: selected candidates: 24, broad candidates: 36, display: 9, context chars: 2000." in text
    assert "Retrieval returned the highest-ranked memories within the configured budget." in text
    assert "ExampleOS memory 6" in text


def test_palaceoftruth_provider_stricts_active_workspace_retrieval(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="project-a",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {
                "scopes": [
                    {"scope": {"type": "workspace", "key": "project-a"}, "entry_count": 1},
                    {"scope": {"type": "workspace", "key": "project-b"}, "entry_count": 1},
                ],
                "total": 2,
                "limit": 100,
            }
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {
            "trace": {
                "searched_scopes": [{"type": "workspace", "key": "project-a"}],
                "workspace_strict": True,
                "broad_corpus_searched": False,
            },
            "results": [
                {
                    "item_id": "project-a-note",
                    "title": "Project A memory",
                    "source_type": "note",
                    "chunk_text": "Only project A context.",
                    "score": 0.93,
                    "tags": ["workspace-project-a"],
                }
            ],
            "total": 1,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("status update", session_id="session-1")

    assert seen_payload["workspace_scope_keys"] == ["project-a"]
    assert seen_payload["workspace_strict"] is True
    assert seen_payload["include_tenant_shared"] is False
    assert seen_payload["tenant_shared_policy"] == "never"
    assert seen_payload["include_broad_corpus"] is False
    assert seen_payload["broad_corpus_policy"] == "disabled"
    assert "Project A memory" in text


def test_palaceoftruth_provider_exposes_explicit_search_and_remember_tools(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    schemas = provider.get_tool_schemas()
    tool_names = {schema["name"] for schema in schemas}

    assert tool_names == {
        "palace_search",
        "palace_fact_recall",
        "palace_semantic_recall",
        "palace_remember",
        "palace_remember_bulk",
        "palace_memory_job_status",
        "palace_exact_scope_recall",
    }
    assert "palace_search" in provider.system_prompt_block()


def test_palaceoftruth_prompt_requires_search_before_no_memory_answer(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    prompt = provider.system_prompt_block()

    assert "Do not answer that Palace has no memory" in prompt
    assert "unless you called palace_search" in prompt
    assert "if search was unavailable, say that explicitly" in prompt


def test_palaceoftruth_prompt_includes_active_workspace_boundary(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="palaceoftruth",
        platform="discord",
    )

    prompt = provider.system_prompt_block()

    assert "ACTIVE PROJECT: palaceoftruth" in prompt
    assert "Memories from other projects must not influence decisions" in prompt
    assert "unless the user explicitly requests cross-project context" in prompt


def test_palaceoftruth_prefetch_cache_is_scoped_by_workspace(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="project-a",
        platform="discord",
    )

    calls: list[tuple[str, str, str]] = []

    def fake_retrieve_text(query: str, session_id: str) -> str:
        calls.append((query, session_id, provider._agent_workspace))
        return f"memory for {provider._agent_workspace}"

    provider._retrieve_text = fake_retrieve_text  # type: ignore[method-assign]

    assert provider.prefetch("status update", session_id="session-1") == "memory for project-a"
    assert provider.prefetch("status update", session_id="session-1") == "memory for project-a"

    provider._agent_workspace = "project-b"

    assert provider.prefetch("status update", session_id="session-1") == "memory for project-b"
    assert calls == [
        ("status update", "session-1", "project-a"),
        ("status update", "session-1", "project-b"),
    ]


def test_palaceoftruth_on_session_switch_resets_session_cache_and_tenant(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="palaceoftruth",
        platform="discord",
    )
    provider._tenant_id = "tenant-a"
    provider._prefetch_cache = {
        "query": "old query",
        "session_id": "session-1",
        "workspace": "palaceoftruth",
        "text": "stale memory",
    }

    provider.on_session_switch("session-2", parent_session_id="session-1", reset=True)

    assert provider._session_id == "session-2"
    assert provider._tenant_id == ""
    assert provider._prefetch_cache == {
        "query": "",
        "session_id": "",
        "workspace": "",
        "text": "",
        "epoch": 1,
    }


def test_palaceoftruth_search_tool_uses_route_aware_retrieval(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {
            "trace": {
                "searched_scopes": [
                    {"type": "agent", "key": "orchestrator"},
                    {"type": "workspace", "key": "hermes"},
                ],
            },
            "results": [
                {
                    "item_id": "palace-note",
                    "title": "Palace memory note",
                    "source_type": "note",
                    "chunk_text": "Use Palace when Andrew asks what the agent remembers.",
                    "score": 0.94,
                }
            ],
            "total": 1,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_search",
            {"query": "what should you remember about Palace?"},
        )
    )

    assert result["ok"] is True
    assert seen_payload["agent_scope_key"] == "orchestrator"
    assert seen_payload["workspace_scope_keys"] == ["hermes"]
    assert seen_payload["include_tenant_shared"] is False
    assert seen_payload["include_broad_corpus"] is False
    assert "Palace memory note" in result["result"]
    assert "agent/orchestrator, workspace/hermes" in result["result"]


def test_palaceoftruth_semantic_recall_tool_posts_temporal_request(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "hermes1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="hermes1",
        agent_workspace="palaceoftruth",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        assert method == "POST"
        assert path == "/api/v1/memory/semantic-recall"
        seen_payload.update(payload or {})
        return {
            "trace": {
                "searched_scope": {"type": "agent", "key": "hermes1"},
                "valid_at": "2026-07-09T12:00:00Z",
                "fact_kind_filter": ["world"],
                "candidate_limit": 25,
                "display_limit": 2,
                "budget_truncated": False,
            },
            "items": [
                {
                    "entry_id": "entry-1",
                    "source_item_id": "source-1",
                    "title": "Semantic routing fact",
                    "body": "Hermes should cite Palace semantic memory provenance.",
                    "source": "hermes-agent-memory-tool",
                    "source_url": "https://example.test/evidence",
                    "scope": {"type": "agent", "key": "hermes1"},
                    "tags": ["semantic-memory"],
                    "semantic_tags": ["routing"],
                    "system_tags": ["scope-agent"],
                    "valid_from": "2026-07-01T00:00:00Z",
                    "valid_until": None,
                    "fact_kind": "world",
                    "score": 0.91,
                    "temporal_status": "current",
                }
            ],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {
                "query": "semantic routing",
                "valid_at": "2026-07-09T12:00:00Z",
                "fact_kind_filter": ["world", "world"],
                "candidate_limit": 25,
                "top_k": 2,
                "recall_max_tokens": 900,
            },
        )
    )

    assert result["ok"] is True
    assert result["fallback_used"] is False
    assert seen_payload == {
        "query": "semantic routing",
        "scope_type": "agent",
        "scope_key": "hermes1",
        "top_k": 2,
        "candidate_limit": 25,
        "recall_max_tokens": 900,
        "valid_at": "2026-07-09T12:00:00Z",
        "fact_kind_filter": ["world"],
    }
    assert "Semantic routing fact [agent/hermes1, world, current]" in result["result"]
    assert "entry_id=entry-1" in result["result"]
    assert "source_item_id=source-1" in result["result"]
    assert "scope=agent/hermes1" in result["result"]
    assert "fact_kind=world" in result["result"]
    assert "valid_from=2026-07-01T00:00:00Z" in result["result"]
    assert "semantic_tags=routing" in result["result"]
    assert "score=0.91" in result["result"]


def test_palaceoftruth_semantic_prefetch_is_disabled_by_default(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.delenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", raising=False)

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    calls: list[tuple[str, str]] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        calls.append((method, path))
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        return {
            "trace": {"searched_scopes": [{"type": "agent", "key": "hermes"}]},
            "results": [
                {
                    "item_id": "route-aware-item",
                    "title": "Route-aware memory",
                    "chunk_text": "Default pre-turn recall remains route-aware.",
                    "score": 0.9,
                }
            ],
            "total": 1,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]

    text = provider.prefetch("default recall", session_id="session-1")

    assert "Route-aware memory" in text
    assert ("POST", "/api/v1/memory/semantic-recall") not in calls
    assert {schema["name"] for schema in provider.get_tool_schemas()} >= {"palace_semantic_recall"}


def test_palaceoftruth_semantic_prefetch_posts_strict_scope_request(monkeypatch, caplog) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "iris")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_TOP_K", "3")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_CANDIDATE_LIMIT", "21")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS", "800")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_CONTEXT_BUDGET_CHARS", "1200")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="iris",
        agent_workspace="hermes",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scope-profile":
            assert params == {"scope_type": "agent", "scope_key": "iris"}
            return {"scope": {"type": "agent", "key": "iris"}, "quiet_recall": False}
        assert method == "POST"
        assert path == "/api/v1/memory/semantic-recall"
        seen_payload.update(payload or {})
        return {
            "trace": {
                "searched_scope": {"type": "agent", "key": "iris"},
                "candidate_limit": 21,
                "display_limit": 3,
                "recall_max_tokens": 800,
            },
            "items": [
                {
                    "entry_id": "entry-iris",
                    "source_item_id": "item-iris",
                    "title": "Iris launch preference",
                    "body": "Iris prefers source-backed semantic recall at wake-up.",
                    "scope": {"type": "agent", "key": "iris"},
                    "source": "hermes-agent-memory-tool",
                    "fact_kind": "experience",
                    "score": 0.93,
                    "temporal_status": "current",
                }
            ],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.INFO)

    text = provider.prefetch("startup recall", session_id="session-1")

    assert seen_payload == {
        "query": "startup recall",
        "scope_type": "agent",
        "scope_key": "iris",
        "top_k": 3,
        "candidate_limit": 21,
        "recall_max_tokens": 800,
        "context_budget_chars": 1200,
    }
    assert "Iris launch preference [agent/iris, experience, current]" in text
    assert "entry_id=entry-iris" in text
    assert "event=semantic_prefetch_success" in caplog.text
    assert "startup recall" not in caplog.text
    assert "Iris prefers source-backed" not in caplog.text


def test_palaceoftruth_semantic_prefetch_empty_respects_quiet_recall(monkeypatch, caplog) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "quiet-agent")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="quiet-agent")

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scope-profile":
            return {"scope": {"type": "agent", "key": "quiet-agent"}, "quiet_recall": True}
        assert method == "POST"
        assert path == "/api/v1/memory/semantic-recall"
        return {
            "trace": {"searched_scope": {"type": "agent", "key": "quiet-agent"}},
            "items": [],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.INFO)

    assert provider.prefetch("no matching memory", session_id="session-1") == ""
    assert "event=semantic_prefetch_empty" in caplog.text
    assert "'quiet_recall': True" in caplog.text


def test_palaceoftruth_semantic_prefetch_empty_renders_when_not_quiet(monkeypatch, caplog) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "workspace")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "hermes")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_workspace="hermes")

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scope-profile":
            return {"scope": {"type": "workspace", "key": "hermes"}, "quiet_recall": False}
        return {"trace": {"searched_scope": {"type": "workspace", "key": "hermes"}}, "items": []}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.INFO)

    text = provider.prefetch("no matching memory", session_id="session-1")

    assert text == (
        "Palace of Truth semantic recall searched workspace/hermes and found no matching semantic memory."
    )
    assert "event=semantic_prefetch_empty" in caplog.text
    assert "'quiet_recall': False" in caplog.text


def test_palaceoftruth_semantic_prefetch_reports_budget_truncation(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "iris")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_CONTEXT_BUDGET_CHARS", "320")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="iris")

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scope-profile":
            return {"scope": {"type": "agent", "key": "iris"}, "quiet_recall": False}
        return {
            "trace": {
                "searched_scope": {"type": "agent", "key": "iris"},
                "budget_truncated": True,
            },
            "items": [
                {
                    "entry_id": f"entry-{index}",
                    "title": f"Iris semantic memory {index}",
                    "body": "This recalled semantic memory is deliberately long enough to consume budget.",
                    "scope": {"type": "agent", "key": "iris"},
                    "score": 0.9,
                }
                for index in range(5)
            ],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]

    text = provider.prefetch("budget recall", session_id="session-1")

    assert "Semantic recall returned the highest-ranked memories within budget." in text
    assert "Additional semantic memories were omitted to stay within the context budget." in text


def test_palaceoftruth_semantic_prefetch_fails_closed_when_route_unavailable(
    monkeypatch, caplog
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "iris")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_BROAD_CORPUS", "true")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "agent/*")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="iris")

    calls: list[tuple[str, str]] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        calls.append((method, path))
        if path == "/api/v1/memory/semantic-recall":
            raise RuntimeError("Palace of Truth POST /api/v1/memory/semantic-recall failed with HTTP 404")
        raise AssertionError(f"semantic prefetch must not fall back to {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.WARNING)

    assert provider.prefetch("older server", session_id="session-1") == ""
    assert calls == [("POST", "/api/v1/memory/semantic-recall")]
    assert "event=semantic_prefetch_unavailable" in caplog.text
    assert "semantic_recall_route_unavailable_fail_closed" in caplog.text


def test_palaceoftruth_semantic_prefetch_rejects_sibling_agent_scope(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "other-agent")
    monkeypatch.setenv("PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="iris")

    def fake_request_json(*_args, **_kwargs) -> dict:
        raise AssertionError("sibling-agent semantic prefetch must fail before HTTP")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]

    with pytest.raises(module.PalaceScopeConflictError, match="canonical agent scope conflict"):
        provider.prefetch("sibling recall", session_id="session-1")


def test_palaceoftruth_semantic_recall_does_not_cross_to_raw_capture_on_404(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        assert path == "/api/v1/memory/semantic-recall"
        raise RuntimeError("Palace of Truth POST /api/v1/memory/semantic-recall failed with HTTP 404")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call("palace_semantic_recall", {"query": "older server"})
    )

    assert result["ok"] is False
    assert result["query"] == "older server"
    assert result["retrieval_mode"] == "fact_token_overlap"
    assert result["corpus_class"] == "curated_memory_entry"
    assert result["broader_authorized_fallback_used"] is False
    assert "Raw capture search was not used" in result["result"]


def test_palaceoftruth_semantic_recall_rejects_sibling_agent_scope(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "hermes1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="hermes1",
        agent_workspace="palaceoftruth",
    )

    def fake_request_json(*_args, **_kwargs) -> dict:
        raise AssertionError("sibling-agent semantic recall must fail before HTTP")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {
                "query": "other agent memory",
                "scope_type": "agent",
                "scope_key": "other-agent",
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert "sibling-agent semantic recall is not exposed" in result["error"]["message"]


def test_palaceoftruth_semantic_recall_rejects_foreign_workspace_by_name(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
    )

    def fake_request_json(*_args, **_kwargs) -> dict:
        raise AssertionError("cross-workspace semantic recall must fail before HTTP")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {
                "query": "quietfirm history",
                "scope_type": "workspace",
                "scope_key": "quietfirm",
            },
        )
    )

    message = result["error"]["message"]
    assert result["ok"] is False
    assert "workspace/quietfirm is not the active Hermes workspace (hermes)" in message
    assert "cross-workspace semantic recall is not exposed" in message
    # The refusal must not be blamed on sibling agents or on OAuth client scopes.
    assert "sibling-agent" not in message
    assert "not by OAuth client scopes" in message
    assert "Readable scopes for this session: agent/clara, workspace/hermes" in message


def test_palaceoftruth_semantic_recall_tenant_shared_rejection_names_the_flag(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "false")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
    )

    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {"query": "shared context", "scope_type": "tenant_shared"},
        )
    )

    message = result["error"]["message"]
    assert result["ok"] is False
    assert "PALACEOFTRUTH_INCLUDE_TENANT_SHARED=true" in message
    assert "sibling-agent" not in message


def test_palaceoftruth_search_empty_result_names_the_searched_scopes(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
    )

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        if method == "POST" and path == "/api/v1/memory/retrieve-agent":
            return {"trace": {"searched_scopes": []}, "results": [], "total": 0}
        if method == "POST" and path == "/api/v1/memory/retrieve":
            return {"trace": {"fallback_used": False}, "results": [], "total": 0}
        raise AssertionError(f"Unexpected request: {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(provider.handle_tool_call("palace_search", {"query": "QuietFirm"}))

    assert result["ok"] is True
    assert result["searched_scopes"] == ["agent/clara"]
    assert "agent/clara" in result["result"]
    assert "Other scopes were not searched." in result["result"]


def test_palaceoftruth_search_reports_unavailable_when_no_scope_answers(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
    )

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        raise RuntimeError("Palace of Truth request failed")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(provider.handle_tool_call("palace_search", {"query": "QuietFirm"}))

    assert result["ok"] is True
    assert result["searched_scopes"] == []
    assert "search was unavailable" in result["result"]
    assert "Do not report this as an empty memory." in result["result"]


def test_palaceoftruth_background_prefetch_survives_failure(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="clara")
    provider._prefetch_cache = {
        "query": "old query",
        "session_id": "session-1",
        "workspace": "",
        "text": "stale recall from a previous turn",
    }

    def failing_prefetch_text(*_args, **_kwargs) -> str:
        raise module.PalaceCircuitOpenError(12)

    provider._prefetch_text = failing_prefetch_text  # type: ignore[attr-defined]

    provider.queue_prefetch("new query", session_id="session-1")
    thread = provider._prefetch_thread
    assert thread is not None
    thread.join(timeout=5)

    assert thread.is_alive() is False
    # A failed background prefetch must clear the cache, not replay stale text.
    assert provider._prefetch_cache["query"] == "new query"
    assert provider._prefetch_cache["text"] == ""


def test_palaceoftruth_all_permitted_agent_scopes_reaches_route_aware_recall(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_AGENT_SCOPES", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/whoami":
            return oauth_whoami("clara")
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {
            "trace": {"searched_scopes": [{"type": "agent", "key": "karen"}]},
            "results": [
                {
                    "item_id": "karen-note",
                    "title": "Karen memory",
                    "source_type": "note",
                    "chunk_text": "Sibling agent recall.",
                    "score": 0.9,
                }
            ],
            "total": 1,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("sibling recall", session_id="session-1")

    # The opt-in must reach the server switch the tenant policy gates on, and
    # carry the audit reason that policy requires.
    assert seen_payload["include_all_permitted_agent_scopes"] is True
    assert seen_payload["agent_scope_key"] == "clara"
    assert "clara" in seen_payload["access_reason"]
    # A bound Hermes client still may not ask for workspace or broad corpus:
    # the server 403s the whole request when it does.
    assert seen_payload["workspace_scope_keys"] == []
    assert seen_payload["include_broad_corpus"] is False
    assert "Karen memory" in text


def test_palaceoftruth_semantic_recall_allows_opted_in_sibling_agent(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "agent/*")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        assert path == "/api/v1/memory/semantic-recall"
        seen_payload.update(payload or {})
        return {
            "items": [{"title": "Karen semantic memory", "body": "Sibling recall."}],
            "trace": {"searched_scope": {"type": "agent", "key": "karen"}},
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {"query": "sibling", "scope_type": "agent", "scope_key": "karen"},
        )
    )

    assert result["ok"] is True
    assert seen_payload["scope_type"] == "agent"
    assert seen_payload["scope_key"] == "karen"
    # Delegated semantic reads must be auditable, like the keyword path.
    assert "clara" in seen_payload["access_reason"]
    assert "Karen semantic memory" in result["result"]


def test_palaceoftruth_semantic_recall_honours_pattern_boundaries(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "kar*")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="clara")

    def fake_request_json(*_args, **_kwargs) -> dict:
        raise AssertionError("an unmatched sibling scope must fail before HTTP")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {"query": "sibling", "scope_type": "agent", "scope_key": "mara"},
        )
    )

    message = result["error"]["message"]
    assert result["ok"] is False
    assert "sibling-agent semantic recall is not exposed" in message
    assert "agent scopes matching kar*" in message


def test_palaceoftruth_semantic_recall_opt_in_does_not_open_workspaces(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_AGENT_SCOPES", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
    )

    def fake_request_json(*_args, **_kwargs) -> dict:
        raise AssertionError("cross-workspace recall must fail before HTTP")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_semantic_recall",
            {"query": "quietfirm", "scope_type": "workspace", "scope_key": "quietfirm"},
        )
    )

    # The cross-agent opt-in is agent-scoped on both client and server. It must
    # not silently widen into other workspaces.
    assert result["ok"] is False
    assert "cross-workspace semantic recall is not exposed" in result["error"]["message"]


def test_palaceoftruth_search_tool_exposes_generic_title_match_evidence(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "hermes1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="hermes1",
        agent_workspace="palaceoftruth",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {
            "trace": {
                "searched_scopes": [
                    {"type": "agent", "key": "hermes1"},
                    {"type": "workspace", "key": "palaceoftruth"},
                ],
            },
            "results": [
                {
                    "item_id": "449e1b34-f300-48f4-91bf-bedc1a9fb0c4",
                    "title": "Memory",
                    "source_type": "note",
                    "chunk_text": "Generic titled hit whose decisive evidence is a tag-only match.",
                    "score": 0.87,
                    "tags": ["retro-95", "project-retro95", "scope-agent", "agent-hermes1"],
                }
            ],
            "total": 1,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(provider.handle_tool_call("palace_search", {"query": "retro-95"}))

    assert result["ok"] is True
    assert seen_payload["query"] == "retro-95"
    assert "449e1b34-f300-48f4-91bf-bedc1a9fb0c4" in result["result"]
    assert (
        "item_url=http://palaceoftruth-backend:8000/api/v1/items/"
        "449e1b34-f300-48f4-91bf-bedc1a9fb0c4"
    ) in result["result"]
    assert "tags=retro-95, project-retro95, scope-agent, agent-hermes1" in result["result"]
    assert "Snippet: Generic titled hit whose decisive evidence is a tag-only match." in result["result"]


def test_palaceoftruth_search_tool_reports_degraded_search(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    provider._retrieve_text_with_scopes = lambda *_args: (_ for _ in ()).throw(
        module.PalaceCircuitOpenError(12)
    )  # type: ignore[method-assign]

    result = json.loads(provider.handle_tool_call("palace_search", {"query": "palace status"}))

    assert result["ok"] is False
    assert result["query"] == "palace status"
    assert result["retrieval_mode"] == "hybrid"
    assert result["corpus_class"] == "raw_capture"
    assert result["error"] == {
        "type": "PalaceCircuitOpenError",
        "message": "Palace of Truth circuit is open; retry after 12 seconds",
        "retry_after_seconds": 12,
        "retryable": True,
    }


def test_palaceoftruth_remember_tool_writes_to_memory_entries(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET" and path == "/api/v1/memory/whoami":
            return {"tenant_id": "tenant-a"}
        if method == "POST" and path == "/api/v1/memory/entries":
            assert payload is not None
            assert payload["tenant_id"] == "tenant-a"
            assert payload["scope"] == {"type": "agent", "key": "orchestrator"}
            assert payload["source"] == "hermes-agent-memory-tool"
            assert payload["metadata"]["memory_tool"] == {
                "action": "add",
                "target": "memory",
            }
            return {"job_id": "job-1", "status": "accepted"}
        raise AssertionError(f"Unexpected request: {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {"content": "Hermes should use Palace explicitly for memory lookups."},
        )
    )

    assert result["ok"] is True
    assert result["scope"] == {"type": "agent", "key": "orchestrator"}
    assert result["durability"] == {
        "status": "accepted",
        "contract_status": "accepted",
        "durable": False,
        "retryable": False,
        "job_id": "job-1",
    }
    assert result["response"] == {"job_id": "job-1", "status": "accepted"}
    assert [request[:2] for request in requests_seen] == [
        ("GET", "/api/v1/memory/whoami"),
        ("POST", "/api/v1/memory/entries"),
    ]


def test_palaceoftruth_remember_tool_sends_temporal_retention_fields(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        assert path == "/api/v1/memory/entries"
        return {"job_id": "job-1", "status": "accepted"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {
                "content": "Hermes semantic recall should cite provenance.",
                "valid_from": "2026-07-01T00:00:00Z",
                "valid_until": "2026-08-01T00:00:00Z",
                "supersedes_entry_id": "11111111-1111-1111-1111-111111111111",
                "fact_kind": "world",
                "enable_ai_enrichment": True,
                "relationship_policy": "deferred",
            },
        )
    )

    assert result["ok"] is True
    payload = requests_seen[1][2]
    assert payload is not None
    assert payload["valid_from"] == "2026-07-01T00:00:00Z"
    assert payload["valid_until"] == "2026-08-01T00:00:00Z"
    assert payload["supersedes_entry_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["fact_kind"] == "world"
    assert payload["enable_ai_enrichment"] is True
    assert payload["relationship_policy"] == "deferred"
    assert payload["metadata"]["memory_tool"] == {
        "action": "add",
        "target": "memory",
        "valid_from": "2026-07-01T00:00:00Z",
        "valid_until": "2026-08-01T00:00:00Z",
        "supersedes_entry_id": "11111111-1111-1111-1111-111111111111",
        "fact_kind": "world",
        "enable_ai_enrichment": True,
        "relationship_policy": "deferred",
    }


def test_palaceoftruth_request_json_retries_429_retry_after(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("PALACEOFTRUTH_RETRY_BACKOFF_SECONDS", "0.1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    sleeps: list[float] = []
    calls = 0

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_urlopen(_request, timeout: int):
        nonlocal calls
        assert timeout == 10
        calls += 1
        if calls == 1:
            error = module.HTTPError(
                "http://palaceoftruth-backend:8000/api/v1/memory/whoami",
                429,
                "Too Many Requests",
                {"Retry-After": "7"},
                io.BytesIO(b'{"detail":"rate limited"}'),
            )
            raise error
        return FakeResponse()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(module, "sleep", lambda delay: sleeps.append(delay))

    assert provider._request_json("GET", "/api/v1/memory/whoami") == {"status": "ok"}
    assert calls == 2
    assert sleeps == [7.0]


def test_palaceoftruth_request_json_attaches_read_scope_for_get_memory_routes(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    captured: list[tuple[str, str, str | None]] = []

    def fake_urlopen(request, timeout: int):
        captured.append(
            (request.get_method(), request_path(request), request.get_header("X-mcp-scope"))
        )
        return FakeJsonResponse({"ok": True, "scopes": []})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    provider._request_json("GET", "/api/v1/memory/whoami")
    provider._request_json("GET", "/api/v1/memory/scopes", params={"limit": 100})

    assert captured == [
        ("GET", "/api/v1/memory/whoami", "read"),
        ("GET", "/api/v1/memory/scopes", "read"),
    ]


def test_palaceoftruth_route_aware_search_sends_read_scope_for_post(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="palaceoftruth",
    )

    captured: list[tuple[str, str, str | None, str | None]] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        captured.append(
            (
                request.get_method(),
                path,
                request.get_header("X-mcp-scope"),
                request.get_header("X-mcp-scopes"),
            )
        )
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": [], "total": 0, "limit": 100})
        if path == "/api/v1/memory/retrieve-agent":
            return FakeJsonResponse(
                {
                    "trace": {"searched_scopes": [{"type": "agent", "key": "orchestrator"}]},
                    "results": [
                        {
                            "item_id": "item-agent",
                            "title": "Agent memory",
                            "source_type": "note",
                            "chunk_text": "Remember route-aware retrieval.",
                            "score": 0.9,
                        }
                    ],
                    "total": 1,
                }
            )
        raise AssertionError(f"Unexpected request path: {path}")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    text = provider.prefetch("route-aware recall", session_id="session-1")

    assert "Agent memory" in text
    assert captured == [
        ("GET", "/api/v1/memory/scopes", "read", "read"),
        ("POST", "/api/v1/memory/retrieve-agent", "read", "read"),
    ]


def test_palaceoftruth_semantic_recall_sends_read_scope_for_post(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    captured: list[tuple[str, str, str | None, str | None]] = []

    def fake_urlopen(request, timeout: int):
        captured.append(
            (
                request.get_method(),
                request_path(request),
                request.get_header("X-mcp-scope"),
                request.get_header("X-mcp-scopes"),
            )
        )
        return FakeJsonResponse({"trace": {"searched_scope": {"type": "agent", "key": "orchestrator"}}, "items": []})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    result = json.loads(
        provider.handle_tool_call("palace_semantic_recall", {"query": "temporal recall"})
    )

    assert result["ok"] is True
    assert captured == [
        ("POST", "/api/v1/memory/semantic-recall", "read", "read"),
    ]


def test_palaceoftruth_fallback_retrieval_sends_read_scope_for_post(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    captured: list[tuple[str, str, str | None]] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        captured.append((request.get_method(), path, request.get_header("X-mcp-scope")))
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": [], "total": 0, "limit": 100})
        if path == "/api/v1/memory/retrieve-agent":
            return FakeJsonResponse({"trace": {"searched_scopes": []}, "results": [], "total": 0})
        if path == "/api/v1/memory/retrieve":
            return FakeJsonResponse(
                {
                    "results": [
                        {
                            "item_id": "item-fallback",
                            "title": "Fallback memory",
                            "source_type": "note",
                            "chunk_text": "Remember fallback retrieval.",
                            "score": 0.8,
                        }
                    ],
                    "total": 1,
                }
            )
        raise AssertionError(f"Unexpected request path: {path}")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    text = provider.prefetch("fallback recall", session_id="session-1")

    assert "Fallback memory" in text
    assert captured == [
        ("GET", "/api/v1/memory/scopes", "read"),
        ("POST", "/api/v1/memory/retrieve-agent", "read"),
        ("POST", "/api/v1/memory/retrieve", "read"),
    ]


def test_palaceoftruth_memory_job_status_tool_polls_read_only_job(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")
    job_id = "631ecf94-5521-4eb3-92a4-938c1a4d4b49"

    def fake_urlopen(request, timeout: int):
        assert request.get_method() == "GET"
        assert request_path(request) == f"/api/v1/memory/jobs/{job_id}"
        assert request.get_header("X-mcp-scope") == "read"
        return FakeJsonResponse(
            {"job_id": job_id, "status": "complete", "contract_status": "completed"}
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    result = json.loads(provider.handle_tool_call("palace_memory_job_status", {"job_id": job_id}))

    assert result == {
        "ok": True,
        "job_id": job_id,
        "status": "complete",
        "contract_status": "completed",
        "retryable": False,
        "poll_after_seconds": None,
        "job": {"job_id": job_id, "status": "complete", "contract_status": "completed"},
    }


def test_palaceoftruth_exact_scope_recall_stays_in_active_agent_scope(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "barbara")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")
    seen_payload: dict = {}

    def fake_urlopen(request, timeout: int):
        assert request.get_method() == "POST"
        assert request_path(request) == "/api/v1/memory/retrieve"
        assert request.get_header("X-mcp-scope") == "read"
        seen_payload.update(json.loads(request.data.decode("utf-8")))
        return FakeJsonResponse(
            {
                "results": [
                    {
                        "item_id": "2fa8de6f-c754-4de7-b13f-8ca8ef31ff47",
                        "title": "Barbara OAuth canary",
                        "chunk_text": "terminal job verified",
                        "scope": {"type": "agent", "key": "barbara"},
                    }
                ]
            }
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    result = json.loads(
        provider.handle_tool_call("palace_exact_scope_recall", {"query": "barbara canary"})
    )

    assert seen_payload == {
        "query": "barbara canary",
        "limit": 5,
        "scope": {"type": "agent", "key": "barbara"},
    }
    assert result["ok"] is True
    assert result["scope"] == {"type": "agent", "key": "barbara"}
    assert "Exact-scope recall" in result["result"]
    assert "agent/barbara" in result["result"]


def test_palaceoftruth_request_json_honors_retry_after_http_date(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("PALACEOFTRUTH_RETRY_BACKOFF_SECONDS", "0.1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=9), usegmt=True)
    sleeps: list[float] = []
    calls = 0

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_urlopen(_request, timeout: int):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise module.HTTPError(
                "http://palaceoftruth-backend:8000/api/v1/memory/whoami",
                429,
                "Too Many Requests",
                {"Retry-After": retry_at},
                io.BytesIO(b'{"detail":"rate limited"}'),
            )
        return FakeResponse()

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    monkeypatch.setattr(module, "sleep", lambda delay: sleeps.append(delay))

    assert provider._request_json("GET", "/api/v1/memory/whoami") == {"status": "ok"}
    assert calls == 2
    assert 1.0 < sleeps[0] <= 9.0


def test_palaceoftruth_request_json_does_not_retry_permanent_4xx(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_RETRY_ATTEMPTS", "3")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    calls = 0

    def fake_urlopen(_request, timeout: int):
        nonlocal calls
        calls += 1
        raise module.HTTPError(
            "http://palaceoftruth-backend:8000/api/v1/memory/entries",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"detail":"validation failed"}'),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="400"):
        provider._request_json(
            "POST",
            "/api/v1/memory/entries",
            {"scope": {"type": "agent", "key": "orchestrator"}},
        )
    assert calls == 1


def test_palaceoftruth_request_json_keeps_validation_detail_from_error_body(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    body = json.dumps(
        {
            "detail": [
                {
                    "type": "datetime_from_date_parsing",
                    "loc": ["body", "valid_from"],
                    "input": "tomorrow",
                }
            ]
        }
    ).encode("utf-8")

    def fake_urlopen(_request, timeout: int):
        raise module.HTTPError(
            "http://palaceoftruth-backend:8000/api/v1/memory/entries",
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        provider._request_json(
            "POST",
            "/api/v1/memory/entries",
            {"scope": {"type": "agent", "key": "orchestrator"}},
        )

    message = str(excinfo.value)
    assert "HTTP 422" in message
    assert "valid_from" in message
    assert "datetime_from_date_parsing" in message


def test_palaceoftruth_request_json_truncates_a_long_error_body(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    body = b'{"detail":"' + b"x" * 5000 + b'"}'

    def fake_urlopen(_request, timeout: int):
        raise module.HTTPError(
            "http://palaceoftruth-backend:8000/api/v1/memory/entries",
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        provider._request_json(
            "POST",
            "/api/v1/memory/entries",
            {"scope": {"type": "agent", "key": "orchestrator"}},
        )

    message = str(excinfo.value)
    assert message.endswith("...")
    assert len(message) < module.MAX_HTTP_ERROR_DETAIL_CHARS + 200


def test_palaceoftruth_request_json_handles_an_empty_error_body(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    def fake_urlopen(_request, timeout: int):
        raise module.HTTPError(
            "http://palaceoftruth-backend:8000/api/v1/memory/entries",
            404,
            "Not Found",
            {},
            io.BytesIO(b""),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as excinfo:
        provider._request_json(
            "POST",
            "/api/v1/memory/entries",
            {"scope": {"type": "agent", "key": "orchestrator"}},
        )

    assert str(excinfo.value).endswith("failed with HTTP 404")


def test_palaceoftruth_request_json_opens_circuit_after_repeated_transient_failures(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("PALACEOFTRUTH_CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("PALACEOFTRUTH_CIRCUIT_COOLDOWN_SECONDS", "9")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    calls = 0

    def fake_urlopen(_request, timeout: int):
        nonlocal calls
        calls += 1
        raise module.URLError("temporary DNS failure")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(module.PalaceTransientError):
        provider._request_json("GET", "/api/v1/memory/whoami")
    with pytest.raises(module.PalaceTransientError):
        provider._request_json("GET", "/api/v1/memory/whoami")
    with pytest.raises(module.PalaceCircuitOpenError):
        provider._request_json("GET", "/api/v1/memory/whoami")
    assert calls == 2


def test_palaceoftruth_remember_tool_rejects_oversized_explicit_memory(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    requests_seen: list[tuple[str, str]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        raise AssertionError("oversized explicit write should not POST")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {"content": "x" * (module.MAX_MEMORY_BODY_CHARS + 1)},
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "PalacePayloadTooLargeError"
    assert requests_seen == [("GET", "/api/v1/memory/whoami")]


def _writing_provider(module, monkeypatch, requests_seen: list):
    """Build an initialized provider whose POSTs fail the test loudly.

    Used by the argument-validation tests below: a rejected write must never
    reach /api/v1/memory/entries, so any POST is an assertion failure.
    """
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        raise AssertionError(f"invalid write arguments should not POST {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    return provider


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid_from", "tomorrow"),
        ("valid_from", "08/17/2026"),
        ("valid_from", "next Tuesday"),
        ("valid_until", "next week"),
        ("valid_until", "2026-13-01T00:00:00Z"),
        ("valid_until", "in 30 days"),
    ],
)
def test_palaceoftruth_remember_tool_rejects_unparsable_timestamps(
    monkeypatch, field, value
) -> None:
    module = load_palaceoftruth_plugin()
    requests_seen: list[tuple[str, str]] = []
    provider = _writing_provider(module, monkeypatch, requests_seen)

    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {"content": "Hermes should cite provenance.", field: value},
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert field in result["error"]["message"]
    assert "ISO-8601" in result["error"]["message"]
    assert requests_seen == [("GET", "/api/v1/memory/whoami")]


@pytest.mark.parametrize(
    "value",
    [
        "entry-123",
        "Palace write validation fix",
        "11111111-1111-1111-1111-11111111111",
        "not a uuid at all",
    ],
)
def test_palaceoftruth_remember_tool_rejects_non_uuid_supersedes_entry_id(
    monkeypatch, value
) -> None:
    module = load_palaceoftruth_plugin()
    requests_seen: list[tuple[str, str]] = []
    provider = _writing_provider(module, monkeypatch, requests_seen)

    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {
                "content": "Hermes should cite provenance.",
                "supersedes_entry_id": value,
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert "supersedes_entry_id" in result["error"]["message"]
    assert "UUID" in result["error"]["message"]
    assert requests_seen == [("GET", "/api/v1/memory/whoami")]


def test_palaceoftruth_remember_tool_rejects_inverted_temporal_window(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    requests_seen: list[tuple[str, str]] = []
    provider = _writing_provider(module, monkeypatch, requests_seen)

    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {
                "content": "Hermes should cite provenance.",
                "valid_from": "2026-08-01T00:00:00Z",
                "valid_until": "2026-07-01T00:00:00Z",
            },
        )
    )

    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert "valid_until must be greater than or equal to valid_from" in (
        result["error"]["message"]
    )
    assert requests_seen == [("GET", "/api/v1/memory/whoami")]


def test_palaceoftruth_remember_tool_allows_a_naive_and_aware_window_pair(monkeypatch) -> None:
    """A naive/aware mix is not ordered client-side; the API normalizes it."""
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="orchestrator")

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        return {"job_id": "job-1", "status": "accepted"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_remember",
            {
                "content": "Hermes should cite provenance.",
                "valid_from": "2026-08-01T00:00:00Z",
                "valid_until": "2026-07-01T00:00:00",
            },
        )
    )

    assert result["ok"] is True


@pytest.mark.parametrize(
    ("valid_from", "valid_until", "supersedes_entry_id"),
    [
        ("2026-07-01T00:00:00Z", "2026-08-01T00:00:00Z", None),
        ("2026-07-01", "2026-08-01", None),
        ("2026-07-01T00:00:00+02:00", None, "11111111-1111-1111-1111-111111111111"),
        ("2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", None),
        (None, None, "11111111111111111111111111111111"),
        # Forms the API accepts that a naive fromisoformat/UUID check would
        # wrongly reject. Rejecting these would break writes that work today.
        ("2026-07-01T00:00:00z", None, None),
        ("2026-07-01 00:00:00", None, None),
        ("2026-07-01T00:00:00.123456Z", None, None),
        ("1751328000", None, None),
        ("1751328000.5", None, None),
        (None, None, "{11111111-1111-1111-1111-111111111111}"),
        (None, None, "urn:uuid:11111111-1111-1111-1111-111111111111"),
    ],
)
def test_palaceoftruth_remember_tool_accepts_valid_arguments_verbatim(
    monkeypatch, valid_from, valid_until, supersedes_entry_id
) -> None:
    """Accepted values must reach the API unchanged.

    Normalizing them would change the idempotency key and break client-side
    dedup of repeated writes.
    """
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home", agent_identity="orchestrator")

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        return {"job_id": "job-1", "status": "accepted"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    args = {"content": "Hermes should cite provenance."}
    for key, value in (
        ("valid_from", valid_from),
        ("valid_until", valid_until),
        ("supersedes_entry_id", supersedes_entry_id),
    ):
        if value is not None:
            args[key] = value

    result = json.loads(provider.handle_tool_call("palace_remember", args))

    assert result["ok"] is True
    payload = requests_seen[1][2]
    assert payload is not None
    for key, value in (
        ("valid_from", valid_from),
        ("valid_until", valid_until),
        ("supersedes_entry_id", supersedes_entry_id),
    ):
        if value is None:
            assert key not in payload
        else:
            assert payload[key] == value


@pytest.mark.parametrize(
    "args",
    [
        {"contents": [{"content": "Remember A.", "valid_from": "tomorrow"}]},
        {"contents": [{"content": "Remember A.", "valid_until": "next week"}]},
        {"contents": [{"content": "Remember A.", "supersedes_entry_id": "entry-123"}]},
        {
            "contents": [
                {
                    "content": "Remember A.",
                    "valid_from": "2026-08-01T00:00:00Z",
                    "valid_until": "2026-07-01T00:00:00Z",
                }
            ]
        },
        {"contents": ["Remember A."], "default_valid_from": "tomorrow"},
        {"contents": ["Remember A."], "default_valid_until": "next week"},
        {"contents": ["Remember A.", {"content": "Remember B.", "valid_from": "yesterday"}]},
    ],
)
def test_palaceoftruth_remember_bulk_rejects_invalid_temporal_arguments(
    monkeypatch, args
) -> None:
    module = load_palaceoftruth_plugin()
    requests_seen: list[tuple[str, str]] = []
    provider = _writing_provider(module, monkeypatch, requests_seen)

    result = json.loads(provider.handle_tool_call("palace_remember_bulk", args))

    assert result["ok"] is False
    assert result["error"]["type"] == "ValueError"
    assert requests_seen == [("GET", "/api/v1/memory/whoami")]


def test_palaceoftruth_remember_bulk_uses_batch_endpoint(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET" and path == "/api/v1/memory/whoami":
            return {"tenant_id": "tenant-a"}
        if method == "POST" and path == "/api/v1/memory/entries:batch":
            assert payload is not None
            assert [entry["body"] for entry in payload["entries"]] == [
                "Remember A.",
                "Remember B.",
            ]
            assert all(entry["tenant_id"] == "tenant-a" for entry in payload["entries"])
            return {
                "status": "accepted",
                "accepted": 2,
                "failed": 0,
                "poll_after_seconds": 5,
                "retryable": False,
                "results": [
                    {"index": 0, "status": "queued", "contract_status": "queued"},
                    {"index": 1, "status": "queued", "contract_status": "queued"},
                ],
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_remember_bulk",
            {"contents": ["Remember A.", "Remember B."]},
        )
    )

    assert result["ok"] is True
    assert result["accepted"] == 2
    assert result["failed"] == 0
    assert [request[:2] for request in requests_seen] == [
        ("GET", "/api/v1/memory/whoami"),
        ("POST", "/api/v1/memory/entries:batch"),
    ]


def test_palaceoftruth_remember_bulk_accepts_temporal_entry_objects(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET" and path == "/api/v1/memory/whoami":
            return {"tenant_id": "tenant-a"}
        if method == "POST" and path == "/api/v1/memory/entries:batch":
            assert payload is not None
            return {
                "status": "accepted",
                "accepted": len(payload["entries"]),
                "failed": 0,
                "results": [],
            }
        raise AssertionError(f"Unexpected request: {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            "palace_remember_bulk",
            {
                "contents": [
                    {
                        "content": "Remember temporal A.",
                        "valid_from": "2026-07-01T00:00:00Z",
                        "fact_kind": "experience",
                    },
                    {
                        "content": "Remember temporal B.",
                        "target": "user",
                        "relationship_policy": "skip",
                        "enable_ai_enrichment": False,
                    },
                ],
                "default_fact_kind": "world",
                "default_enable_ai_enrichment": True,
                "default_relationship_policy": "deferred",
            },
        )
    )

    assert result["ok"] is True
    payload = requests_seen[1][2]
    assert payload is not None
    assert payload["entries"][0]["body"] == "Remember temporal A."
    assert payload["entries"][0]["valid_from"] == "2026-07-01T00:00:00Z"
    assert payload["entries"][0]["fact_kind"] == "experience"
    assert payload["entries"][0]["enable_ai_enrichment"] is True
    assert payload["entries"][0]["relationship_policy"] == "deferred"
    assert payload["entries"][1]["body"] == "Remember temporal B."
    assert payload["entries"][1]["fact_kind"] == "world"
    assert payload["entries"][1]["enable_ai_enrichment"] is False
    assert payload["entries"][1]["relationship_policy"] == "skip"
    assert payload["entries"][1]["metadata"]["memory_tool"] == {
        "action": "add",
        "target": "user",
        "fact_kind": "world",
        "relationship_policy": "skip",
    }


def test_palaceoftruth_write_paths_send_write_scope_for_entries(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    captured: list[tuple[str, str, str | None, str | None]] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        captured.append(
            (
                request.get_method(),
                path,
                request.get_header("X-mcp-scope"),
                request.get_header("X-mcp-scopes"),
            )
        )
        if path == "/api/v1/memory/whoami":
            return FakeJsonResponse({"tenant_id": "tenant-a"})
        if path == "/api/v1/memory/entries":
            return FakeJsonResponse({"job_id": "job-1", "status": "queued"})
        raise AssertionError(f"Unexpected request path: {path}")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    remember_result = json.loads(
        provider.handle_tool_call("palace_remember", {"content": "Remember explicit write."})
    )
    provider.sync_turn("User asks for recall.", "Assistant answers from Palace.")
    provider.shutdown()
    provider.on_memory_write("add", "memory", "Remember mirrored memory.")
    provider.shutdown()

    assert remember_result["ok"] is True
    assert captured == [
        ("GET", "/api/v1/memory/whoami", "read", "read"),
        ("POST", "/api/v1/memory/entries", "write", "write,write:agent"),
        ("POST", "/api/v1/memory/entries", "write", "write,write:agent"),
        ("POST", "/api/v1/memory/entries", "write", "write,write:agent"),
    ]


def test_palaceoftruth_remember_bulk_sends_write_scope_for_entries_batch(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    captured: list[tuple[str, str, str | None, str | None]] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        captured.append(
            (
                request.get_method(),
                path,
                request.get_header("X-mcp-scope"),
                request.get_header("X-mcp-scopes"),
            )
        )
        if path == "/api/v1/memory/whoami":
            return FakeJsonResponse({"tenant_id": "tenant-a"})
        if path == "/api/v1/memory/entries:batch":
            return FakeJsonResponse(
                {
                    "status": "accepted",
                    "accepted": 2,
                    "failed": 0,
                    "results": [],
                }
            )
        raise AssertionError(f"Unexpected request path: {path}")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    result = json.loads(
        provider.handle_tool_call(
            "palace_remember_bulk",
            {"contents": ["Remember A.", "Remember B."]},
        )
    )

    assert result["ok"] is True
    assert captured == [
        ("GET", "/api/v1/memory/whoami", "read", "read"),
        ("POST", "/api/v1/memory/entries:batch", "write", "write,write:agent"),
    ]


def test_palaceoftruth_batch_write_sends_each_distinct_scoped_write_grant(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    captured: list[tuple[str | None, str | None]] = []

    def fake_urlopen(request, timeout: int):
        captured.append(
            (
                request.get_header("X-mcp-scope"),
                request.get_header("X-mcp-scopes"),
            )
        )
        return FakeJsonResponse({"status": "accepted", "accepted": 3, "failed": 0, "results": []})

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    response = provider._request_json(
        "POST",
        "/api/v1/memory/entries:batch",
        {
            "entries": [
                {"scope": {"type": "agent", "key": "barbara"}},
                {"scope": {"type": "workspace", "key": "palaceoftruth"}},
                {"scope": {"type": "tenant_shared"}},
            ]
        },
    )

    assert response["status"] == "accepted"
    assert captured == [("write", "write,write:agent,write:workspace")]


def test_palaceoftruth_request_json_fails_closed_for_unmapped_memory_route(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes-home")

    def fake_urlopen(_request, timeout: int):
        raise AssertionError("unmapped memory route should fail before HTTP")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="missing an explicit MCP scope mapping"):
        provider._request_json("GET", "/api/v1/memory/future-route")


def test_palaceoftruth_plugin_config_schema_includes_write_quota_defaults() -> None:
    module = load_palaceoftruth_plugin()
    provider = module.PalaceOfTruthMemoryProvider()

    schema = {item["key"]: item for item in provider.get_config_schema()}

    assert schema["write_quotas_enabled"]["default"] == "true"
    assert schema["max_writes_per_turn"]["default"] == "5"
    assert schema["max_writes_per_session"]["default"] == "100"
    assert schema["max_bulk_calls_per_turn"]["default"] == "2"
    assert schema["dedup_cache_ttl_seconds"]["default"] == "300"


def test_palaceoftruth_remember_bulk_enforces_per_turn_bulk_cap(
    monkeypatch,
    caplog,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")
    monkeypatch.setenv("PALACEOFTRUTH_MAX_WRITES_PER_TURN", "20")
    monkeypatch.setenv("PALACEOFTRUTH_MAX_BULK_CALLS_PER_TURN", "2")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        assert path == "/api/v1/memory/entries:batch"
        return {
            "status": "accepted",
            "accepted": len(payload["entries"]),
            "failed": 0,
            "results": [],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.WARNING)
    results = [
        json.loads(
            provider.handle_tool_call(
                "palace_remember_bulk",
                {"contents": [f"Remember batch {batch} entry {index}." for index in range(100)]},
            )
        )
        for batch in range(3)
    ]

    assert [result["ok"] for result in results] == [True, True, False]
    assert results[2]["error"]["type"] == "PalaceRateLimitError"
    assert "per-turn bulk-call cap exceeded" in results[2]["error"]["message"]
    assert "Palace bulk write cap reached: 2/2 bulk calls this turn" in caplog.text
    assert [
        (method, path)
        for method, path, _ in requests_seen
        if method == "POST"
    ] == [
        ("POST", "/api/v1/memory/entries:batch"),
        ("POST", "/api/v1/memory/entries:batch"),
    ]


def test_palaceoftruth_write_quota_counts_sync_and_memory_mirror(
    monkeypatch,
    caplog,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")
    monkeypatch.setenv("PALACEOFTRUTH_MAX_WRITES_PER_TURN", "1")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        return {"job_id": "job-1", "status": "queued"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.WARNING)
    provider.on_memory_write("add", "memory", "First write consumes the turn quota.")
    provider.shutdown()
    provider.sync_turn("User", "Assistant")
    provider.shutdown()

    assert [
        (method, path)
        for method, path, _ in requests_seen
        if method == "POST"
    ] == [("POST", "/api/v1/memory/entries")]
    assert "per-turn write cap exceeded" in caplog.text
    assert "Palace of Truth sync failed" in caplog.text


def test_palaceoftruth_remember_uses_client_side_dedup(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            return {"tenant_id": "tenant-a"}
        assert path == "/api/v1/memory/entries"
        return {"job_id": "job-dedup", "status": "queued", "contract_status": "queued"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    first = json.loads(
        provider.handle_tool_call("palace_remember", {"content": "Remember once."})
    )
    second = json.loads(
        provider.handle_tool_call("palace_remember", {"content": "Remember once."})
    )

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["response"] == second["response"]
    assert [
        (method, path)
        for method, path, _ in requests_seen
        if method == "POST"
    ] == [("POST", "/api/v1/memory/entries")]


def test_palaceoftruth_provider_demotes_self_recall_when_workspace_docs_exist(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="",
        platform="discord",
    )

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {
                "scopes": [
                    {"scope": {"type": "agent", "key": "orchestrator"}, "entry_count": 1},
                    {"scope": {"type": "workspace", "key": "exampleos"}, "entry_count": 1},
                ],
                "total": 2,
                "limit": 100,
            }
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        assert payload["workspace_scope_keys"] == ["exampleos"]
        return {
            "trace": {
                "searched_scopes": [
                    {"type": "agent", "key": "orchestrator"},
                    {"type": "workspace", "key": "exampleos"},
                ],
                "fallback_used": True,
                "completeness_warnings": [
                    "Room routing confidence was low, so results include global semantic matches."
                ],
            },
            "results": [
                {
                    "item_id": "orchestrator-self-recall",
                    "title": "default: [Andrew] ok check now",
                    "source_type": "note",
                    "chunk_text": "# Conversation Turn\n\n## User\nExampleOS?\n\n## Assistant\nI couldn't find ExampleOS in Palace.",
                    "score": 0.92,
                    "tags": ["scope-agent", "agent-orchestrator"],
                },
                {
                    "item_id": "exampleos-current-state",
                    "title": "ExampleOS current-state documentation",
                    "source_type": "note",
                    "chunk_text": "ExampleOS is a multi-tenant AI company operating system.",
                    "score": 0.48,
                    "tags": [
                        "scope-workspace",
                        "workspace-exampleos",
                        "exampleos",
                        "current-state",
                    ],
                },
            ],
            "total": 2,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("what is ExampleOS", session_id="session-1")

    assert "[0.48] ExampleOS current-state documentation [note, workspace/exampleos]" in text
    assert "orchestrator-self-recall" not in text
    assert "I couldn't find ExampleOS" not in text


def test_palaceoftruth_provider_retrieve_merges_agent_and_tenant_shared(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "true")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_BROAD_CORPUS", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="orchestrator",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {
                "scopes": [
                    {"scope": {"type": "agent", "key": "orchestrator"}, "entry_count": 1},
                    {"scope": {"type": "workspace", "key": "exampleos"}, "entry_count": 1},
                    {"scope": {"type": "tenant_shared"}, "entry_count": 1},
                ],
                "total": 3,
                "limit": 100,
            }
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        assert payload["agent_scope_key"] == "orchestrator"
        assert payload["workspace_scope_keys"] == ["orchestrator"]
        return {
            "trace": {
                "searched_scopes": [
                    {"type": "agent", "key": "orchestrator"},
                    {"type": "workspace", "key": "orchestrator"},
                    {"type": "tenant_shared"},
                ],
                "broad_corpus_searched": True,
            },
            "results": [
                {
                    "item_id": "item-agent-note",
                    "title": "Prior note",
                    "source_type": "note",
                    "chunk_text": "I do not know Henry Intelligent Machines.",
                    "score": 0.41,
                },
                {
                    "item_id": "item-shared-media",
                    "title": "Henry Intelligent Machines",
                    "source_type": "media",
                    "chunk_text": "Shared media briefing about Henry Intelligent Machines.",
                    "score": 0.96,
                },
            ],
            "total": 2,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("henry intelligent machines", session_id="session-1")

    assert seen_payload["include_tenant_shared"] is True
    assert seen_payload["include_broad_corpus"] is True
    assert "Available memory scopes include: agent/orchestrator, workspace/exampleos, tenant_shared." in text
    assert "[0.96] Henry Intelligent Machines [media]" in text
    assert "[0.41] Prior note [note]" in text


def test_palaceoftruth_provider_prioritizes_shared_notes_the_same_way(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="orchestrator",
        platform="discord",
    )

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        if method == "POST" and path == "/api/v1/memory/retrieve-agent":
            raise RuntimeError("404 route unavailable")
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve"
        scope = payload["scope"] if isinstance(payload, dict) else None
        if scope == {"type": "agent", "key": "orchestrator"}:
            return {
                "trace": {"fallback_used": False},
                "results": [
                    {
                        "item_id": "local-turn",
                        "title": "default: [Andrew] old Henry confusion",
                        "summary": "I still do not know Henry Intelligent Machines.",
                        "source_type": "note",
                        "chunk_text": "# Conversation Turn\n\n## User\nold\n\n## Assistant\nI still do not know Henry Intelligent Machines.",
                        "score": 0.83,
                    }
                ],
                "total": 1,
            }
        if scope == {"type": "tenant_shared"}:
            return {
                "trace": {"fallback_used": False},
                "results": [
                    {
                        "item_id": "shared-note",
                        "title": "Henry Intelligent Machines summary",
                        "summary": "Shared note summary.",
                        "source_type": "note",
                        "chunk_text": "Shared note about Henry Intelligent Machines.",
                        "score": 0.42,
                    }
                ],
                "total": 1,
            }
        raise AssertionError(f"Unexpected scope: {scope}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("henry intelligent machines", session_id="session-1")

    assert "[0.42] Henry Intelligent Machines summary [note, tenant_shared]" in text
    assert "Evidence: item_id=shared-note" in text
    assert "scope=tenant_shared" in text
    assert "I still do not know Henry Intelligent Machines." not in text


def test_palaceoftruth_provider_fallback_stays_inside_active_workspace(
    monkeypatch,
    caplog,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="palaceoftruth",
        platform="discord",
    )

    fallback_scopes_seen: list[dict] = []

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {
                "scopes": [
                    {"scope": {"type": "agent", "key": "orchestrator"}, "entry_count": 1},
                    {"scope": {"type": "workspace", "key": "palaceoftruth"}, "entry_count": 1},
                    {"scope": {"type": "workspace", "key": "exampleos"}, "entry_count": 1},
                    {"scope": {"type": "tenant_shared"}, "entry_count": 1},
                ],
                "total": 4,
                "limit": 100,
            }
        if method == "POST" and path == "/api/v1/memory/retrieve-agent":
            raise RuntimeError("route-aware timeout")
        assert method == "POST"
        assert path == "/api/v1/memory/retrieve"
        assert payload is not None
        scope = payload["scope"]
        fallback_scopes_seen.append(scope)
        if scope == {"type": "workspace", "key": "palaceoftruth"}:
            return {
                "trace": {"fallback_used": False},
                "results": [
                    {
                        "item_id": "palaceoftruth-feedvalue-brief",
                        "title": "FeedValue Palace recall",
                        "source_type": "note",
                        "chunk_text": "FeedValue memory should be recalled only from the active workspace.",
                        "score": 0.88,
                    }
                ],
                "total": 1,
            }
        return {"trace": {"fallback_used": False}, "results": [], "total": 0}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    with caplog.at_level(logging.WARNING):
        text = provider.prefetch("FeedValue recall", session_id="session-1")

    assert fallback_scopes_seen == [
        {"type": "agent", "key": "orchestrator"},
        {"type": "workspace", "key": "palaceoftruth"},
    ]
    assert "event=route_aware_failed" in caplog.text
    assert "error_class" in caplog.text
    assert "fallback_scope_count" in caplog.text
    assert "workspace/palaceoftruth" in caplog.text
    assert "FeedValue recall" not in caplog.text
    assert "tenant-key" not in caplog.text
    assert "FeedValue Palace recall" not in caplog.text
    assert "Retrieval searched scopes: agent/orchestrator, workspace/palaceoftruth." in text
    assert "[0.88] FeedValue Palace recall [note, workspace/palaceoftruth]" in text
    assert "Evidence: item_id=palaceoftruth-feedvalue-brief" in text
    assert "workspace/exampleos" not in text


def test_palaceoftruth_provider_sync_turn_uses_resolved_tenant(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            assert path == "/api/v1/memory/whoami"
            assert payload is None
            return {"tenant_id": "tenant-acme"}
        assert method == "POST"
        assert path == "/api/v1/memory/entries"
        return {"job_id": "job-1", "status": "queued"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.sync_turn(
        "Andrew prefers staging deploys before prod.",
        "I'll use staging first.",
    )
    provider.shutdown()

    assert len(requests_seen) == 2
    assert requests_seen[0] == ("GET", "/api/v1/memory/whoami", None)
    method, path, payload = requests_seen[1]
    assert method == "POST"
    assert path == "/api/v1/memory/entries"
    assert payload is not None
    assert payload["tenant_id"] == "tenant-acme"
    assert payload["scope"] == {"type": "agent", "key": "orchestrator"}
    assert payload["summary"] == "I'll use staging first."


def test_palaceoftruth_provider_mirrors_memory_tool_writes_with_resolved_tenant(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        if method == "GET":
            assert path == "/api/v1/memory/whoami"
            assert payload is None
            requests_seen.append((method, path, payload))
            return {"tenant_id": "tenant-acme"}
        requests_seen.append((method, path, payload))
        return {"job_id": "job-1", "status": "queued"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.on_memory_write(
        "add",
        "memory",
        "Andrew prefers Kubernetes for production deployments.",
    )
    provider.shutdown()

    assert len(requests_seen) == 2
    assert requests_seen[0] == ("GET", "/api/v1/memory/whoami", None)
    method, path, payload = requests_seen[1]
    assert method == "POST"
    assert path == "/api/v1/memory/entries"
    assert payload is not None
    assert payload["tenant_id"] == "tenant-acme"
    assert payload["source"] == "hermes-agent-memory-tool"
    assert payload["created_by_role"] == "system"
    assert payload["scope"] == {"type": "agent", "key": "orchestrator"}
    assert payload["summary"] == "Andrew prefers Kubernetes for production deployments."
    assert payload["tags"] == [
        "hermes-memory-tool",
        "hermes-memory-target-memory",
        "hermes-memory-action-add",
    ]
    assert payload["metadata"]["memory_tool"] == {
        "action": "add",
        "target": "memory",
    }
    assert payload["body"] == "Andrew prefers Kubernetes for production deployments."


def test_palaceoftruth_provider_tags_memory_tool_write_with_active_skills(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "codex")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="codex",
        agent_workspace="palaceoftruth",
        platform="codex",
        active_skills=[
            "codex-automation-handoff",
            "Browser Use: Browser",
            {"name": "github:yeet"},
            "codex-automation-handoff",
            " ",
        ],
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        if method == "GET":
            assert path == "/api/v1/memory/whoami"
            return {"tenant_id": "tenant-acme"}
        requests_seen.append((method, path, payload))
        return {"job_id": "job-1", "status": "queued"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.on_memory_write("add", "memory", "Categorize this with runtime skills.")
    provider.shutdown()

    assert len(requests_seen) == 1
    method, path, payload = requests_seen[0]
    assert method == "POST"
    assert path == "/api/v1/memory/entries"
    assert payload is not None
    assert payload["tags"] == [
        "hermes-memory-tool",
        "hermes-memory-target-memory",
        "hermes-memory-action-add",
        "skill-codex-automation-handoff",
        "skill-browser-use-browser",
        "skill-github-yeet",
    ]
    assert payload["metadata"]["active_skills"] == [
        "codex-automation-handoff",
        "Browser Use: Browser",
        "github:yeet",
    ]
    assert payload["metadata"]["memory_tool"] == {
        "action": "add",
        "target": "memory",
    }


def test_palaceoftruth_provider_caches_whoami_between_writes(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            assert path == "/api/v1/memory/whoami"
            return {"tenant_id": "tenant-acme"}
        return {"job_id": "job-1", "status": "queued"}

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.sync_turn("User reminder", "Assistant reply")
    provider.shutdown()
    provider.on_memory_write("add", "memory", "Andrew prefers idempotent deploy scripts.")
    provider.shutdown()

    assert [
        (method, path)
        for method, path, _ in requests_seen
        if path == "/api/v1/memory/whoami"
    ] == [("GET", "/api/v1/memory/whoami")]
    entry_payloads = [
        payload
        for method, path, payload in requests_seen
        if method == "POST" and path == "/api/v1/memory/entries"
    ]
    assert len(entry_payloads) == 2
    assert all(payload is not None and payload["tenant_id"] == "tenant-acme" for payload in entry_payloads)


def test_palaceoftruth_provider_skips_write_when_whoami_fails(
    monkeypatch,
    caplog,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "orchestrator")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="orchestrator",
        agent_workspace="hermes",
        platform="discord",
    )

    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload: dict | None = None) -> dict:
        requests_seen.append((method, path, payload))
        if method == "GET":
            raise RuntimeError("403 tenant lookup failed")
        raise AssertionError("write POST should be skipped after whoami failure")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    caplog.set_level(logging.WARNING)
    provider.on_memory_write("add", "memory", "Do not send invalid tenant payloads.")
    provider.shutdown()

    assert requests_seen == [("GET", "/api/v1/memory/whoami", None)]
    assert "Palace of Truth tenant resolution failed; skipping write" in caplog.text


def _lux_oauth_provider(module, monkeypatch, *, runtime_identity: str = "default"):
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "https://api.palace.test")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-lux")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "lux")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "turn-1",
        hermes_home="/tmp/hermes-home",
        agent_identity=runtime_identity,
    )
    return provider


def test_palaceoftruth_canonical_scope_uses_server_binding_over_generic_runtime_identity(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    requests_seen: list[tuple[str, str]] = []

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        requests_seen.append((method, path))
        assert (method, path) == ("GET", "/api/v1/memory/whoami")
        return {
            "tenant_id": "tenant-a",
            "auth_mode": "mcp_oauth",
            "mcp_client_key": "hermes-lux",
            "agent_scope_key": "lux",
            "containment_mode": "hermes_agent",
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]

    assert provider._build_scope("turn-1") == {"type": "agent", "key": "lux"}
    assert provider._canonical_bound_agent_scope_key() == "lux"
    assert provider._build_retrieve_scopes("turn-1") == [
        {"type": "agent", "key": "lux"}
    ]
    assert requests_seen == [("GET", "/api/v1/memory/whoami")]


def test_palaceoftruth_canonical_scope_fails_closed_on_non_generic_runtime_conflict(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch, runtime_identity="iris")
    provider._request_json = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "agent_scope_key": "lux",
        "containment_mode": "hermes_agent",
    }

    with pytest.raises(module.PalaceScopeConflictError, match="configured=agent/lux"):
        provider._build_scope("turn-1")


def test_palaceoftruth_canonical_scope_fails_closed_on_server_binding_conflict(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    provider._request_json = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "agent_scope_key": "iris",
        "containment_mode": "hermes_agent",
    }

    with pytest.raises(module.PalaceScopeConflictError, match="server=agent/iris"):
        provider._build_scope("turn-1")


def test_palaceoftruth_oauth_client_key_is_never_agent_scope_authority(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "https://api.palace.test")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-lux")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.delenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", raising=False)
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "turn-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="default",
    )
    provider._request_json = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "mcp_client_key": "hermes-lux",
    }

    assert provider._canonical_bound_agent_scope_key() is None
    assert provider._build_scope("turn-1") is None


def test_palaceoftruth_all_read_and_write_paths_use_one_canonical_agent_key(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", "true")
    provider = _lux_oauth_provider(module, monkeypatch)
    requests_seen: list[tuple[str, str, dict | None]] = []

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        requests_seen.append((method, path, payload))
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        if path == "/api/v1/memory/retrieve-agent":
            return {"trace": {"searched_scopes": []}, "results": []}
        if path == "/api/v1/memory/retrieve":
            return {"trace": {}, "results": []}
        if path == "/api/v1/memory/semantic-recall":
            return {"trace": {"searched_scope": {"type": "agent", "key": "lux"}}, "items": []}
        raise AssertionError(f"unexpected request {method} {path}")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    provider.handle_tool_call("palace_search", {"query": "route-aware"})
    provider.handle_tool_call("palace_semantic_recall", {"query": "semantic"})
    provider.handle_tool_call("palace_exact_scope_recall", {"query": "exact"})
    provider.prefetch("prefetch", session_id="turn-1")
    write_payload = provider._build_memory_write_payload(
        action="add",
        target="memory",
        content="canonical write",
        tenant_id="tenant-a",
    )

    selected_keys: list[str] = []
    for _method, path, payload in requests_seen:
        if not isinstance(payload, dict):
            continue
        if path == "/api/v1/memory/retrieve-agent":
            selected_keys.append(payload["agent_scope_key"])
        elif path == "/api/v1/memory/retrieve":
            selected_keys.append(payload["scope"]["key"])
        elif path == "/api/v1/memory/semantic-recall":
            selected_keys.append(payload["scope_key"])
    selected_keys.append(write_payload["scope"]["key"])
    requested_paths = {path for _method, path, _payload in requests_seen}
    assert "/api/v1/memory/retrieve-agent" in requested_paths
    assert "/api/v1/memory/retrieve" in requested_paths
    assert selected_keys
    assert set(selected_keys) == {"lux"}


def test_palaceoftruth_canonical_403_latches_for_turn_and_new_turn_clears_it(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "lux")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("turn-1", hermes_home="/tmp/hermes-home", agent_identity="default")
    calls: list[str] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        calls.append(path)
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": []})
        raise module.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"detail":"canonical agent scope required"}'),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    first = json.loads(provider.handle_tool_call("palace_search", {"query": "first"}))
    second = json.loads(provider.handle_tool_call("palace_search", {"query": "second"}))

    assert first["ok"] is False
    assert first["error"]["type"] == "PalaceAuthorizationError"
    assert second["ok"] is False
    assert second["error"]["type"] == "PalaceAuthorizationLatchError"
    assert calls.count("/api/v1/memory/retrieve-agent") == 1
    assert calls.count("/api/v1/memory/retrieve") == 0

    provider.sync_turn("", "")
    third = json.loads(provider.handle_tool_call("palace_search", {"query": "third"}))
    assert third["ok"] is False
    assert third["error"]["type"] == "PalaceAuthorizationError"
    assert calls.count("/api/v1/memory/retrieve-agent") == 2


def test_palaceoftruth_same_query_prefetch_retries_after_turn_boundary(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "lux")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("turn-1", hermes_home="/tmp/hermes-home", agent_identity="default")
    calls: list[str] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        calls.append(path)
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": []})
        raise module.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"detail":"canonical agent scope required"}'),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    assert "search is unavailable" in provider.prefetch("same query", session_id="turn-1")
    provider.sync_turn("", "")
    assert "search is unavailable" in provider.prefetch("same query", session_id="turn-1")
    assert calls.count("/api/v1/memory/retrieve-agent") == 2


def test_palaceoftruth_concurrent_same_turn_reads_share_one_403_request(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "lux")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("turn-1", hermes_home="/tmp/hermes-home", agent_identity="default")
    request_started = threading.Event()
    release_request = threading.Event()
    count_lock = threading.Lock()
    canonical_calls = 0

    def fake_urlopen(request, timeout: int):
        nonlocal canonical_calls
        path = request_path(request)
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": []})
        with count_lock:
            canonical_calls += 1
        request_started.set()
        assert release_request.wait(timeout=2)
        raise module.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"detail":"canonical agent scope required"}'),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    results: list[dict] = []

    def search(query: str) -> None:
        results.append(json.loads(provider.handle_tool_call("palace_search", {"query": query})))

    first = threading.Thread(target=search, args=("first",))
    second = threading.Thread(target=search, args=("second",))
    first.start()
    assert request_started.wait(timeout=2)
    second.start()
    release_request.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert canonical_calls == 1
    assert {result["error"]["type"] for result in results} == {
        "PalaceAuthorizationError",
        "PalaceAuthorizationLatchError",
    }


def test_palaceoftruth_stale_background_prefetch_cannot_latch_new_turn(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "lux")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("turn-1", hermes_home="/tmp/hermes-home", agent_identity="default")
    old_request_started = threading.Event()
    release_old_request = threading.Event()
    count_lock = threading.Lock()
    canonical_calls = 0

    def fake_urlopen(request, timeout: int):
        nonlocal canonical_calls
        path = request_path(request)
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": []})
        with count_lock:
            canonical_calls += 1
            call_number = canonical_calls
        if call_number == 1:
            old_request_started.set()
            assert release_old_request.wait(timeout=2)
            raise module.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(b'{"detail":"canonical agent scope required"}'),
            )
        return FakeJsonResponse(
            {
                "trace": {"searched_scopes": [{"type": "agent", "key": "lux"}]},
                "results": [
                    {
                        "item_id": "lux-memory",
                        "title": "Lux memory",
                        "chunk_text": "The new turn can read Palace.",
                        "score": 0.9,
                    }
                ],
            }
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    provider.queue_prefetch("old turn", session_id="turn-1")
    assert old_request_started.wait(timeout=2)

    provider.sync_turn("", "")
    new_turn = json.loads(provider.handle_tool_call("palace_search", {"query": "new turn"}))
    release_old_request.set()
    provider.shutdown()

    assert new_turn["ok"] is True
    assert "Lux memory" in new_turn["result"]
    assert canonical_calls == 2
    assert provider._prefetch_cache.get("epoch") != provider._current_turn_epoch()
    final = json.loads(provider.handle_tool_call("palace_search", {"query": "still new"}))
    assert final["ok"] is True
    assert canonical_calls == 3


def test_palaceoftruth_delayed_prefetch_cancels_before_new_turn_http(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "lux")
    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize("turn-1", hermes_home="/tmp/hermes-home", agent_identity="default")
    worker_started = threading.Event()
    release_worker = threading.Event()
    paths: list[str] = []

    def fake_urlopen(request, timeout: int):
        path = request_path(request)
        paths.append(path)
        if path == "/api/v1/memory/scopes":
            return FakeJsonResponse({"scopes": []})
        return FakeJsonResponse(
            {
                "trace": {"searched_scopes": [{"type": "agent", "key": "lux"}]},
                "results": [
                    {
                        "item_id": "lux-new-turn",
                        "title": "Lux new-turn memory",
                        "chunk_text": "Only the new turn reached Palace.",
                        "score": 0.9,
                    }
                ],
            }
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    original_prefetch_text = provider._prefetch_text

    def delayed_prefetch_text(query: str, session_id: str) -> str:
        worker_started.set()
        assert release_worker.wait(timeout=2)
        return original_prefetch_text(query, session_id)

    provider._prefetch_text = delayed_prefetch_text  # type: ignore[method-assign]
    provider.queue_prefetch("old queued turn", session_id="turn-1")
    assert worker_started.wait(timeout=2)
    provider.sync_turn("", "")
    release_worker.set()
    provider.shutdown()

    assert paths == []
    new_turn = json.loads(provider.handle_tool_call("palace_search", {"query": "new turn"}))
    assert new_turn["ok"] is True
    assert "Lux new-turn memory" in new_turn["result"]
    assert paths.count("/api/v1/memory/retrieve-agent") == 1


def test_palaceoftruth_all_permitted_workspace_scopes_reaches_route_aware_recall(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_WORKSPACE_SCOPES", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
        platform="discord",
    )

    seen_payload: dict = {}

    def fake_request_json(
        method: str,
        path: str,
        payload: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if method == "GET" and path == "/api/v1/memory/whoami":
            return oauth_whoami("clara")
        if method == "GET" and path == "/api/v1/memory/scopes":
            return {"scopes": [], "total": 0, "limit": 100}
        assert path == "/api/v1/memory/retrieve-agent"
        seen_payload.update(payload or {})
        return {
            "trace": {"searched_scopes": [{"type": "workspace", "key": "quietfirm"}]},
            "results": [
                {
                    "item_id": "quietfirm-note",
                    "title": "QuietFirm memory",
                    "source_type": "note",
                    "chunk_text": "Workspace recall.",
                    "score": 0.9,
                }
            ],
            "total": 1,
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    text = provider.prefetch("quietfirm history", session_id="session-1")

    # The opt-in reaches the server switch the tenant policy gates on and carries
    # the audit reason that policy requires.
    assert seen_payload["include_all_permitted_workspace_scopes"] is True
    assert "clara" in seen_payload["access_reason"]
    # The client still names no workspace itself, and never asks for the broad
    # corpus: the server resolves which workspaces the policy permits.
    assert seen_payload["workspace_scope_keys"] == []
    assert seen_payload["include_broad_corpus"] is False
    assert "QuietFirm memory" in text


def test_palaceoftruth_workspace_reach_is_off_by_default(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_WORKSPACE_SCOPES", raising=False)
    monkeypatch.setenv("PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("PALACEOFTRUTH_MCP_CLIENT_KEY", "hermes-clara")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
    )
    assert provider._include_all_permitted_workspace_scopes is False


def test_palaceoftruth_semantic_recall_allows_opted_in_workspace(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_WORKSPACE_SCOPES", "true")

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
    )

    provider._validate_semantic_recall_scope(
        {"type": "workspace", "key": "quietfirm"},
        {"type": "agent", "key": "clara"},
    )


def test_palaceoftruth_semantic_recall_workspace_refusal_names_the_flag(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_BASE_URL", "http://palaceoftruth-backend:8000")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent")
    monkeypatch.setenv("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "clara")
    monkeypatch.delenv("PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_WORKSPACE_SCOPES", raising=False)

    provider = module.PalaceOfTruthMemoryProvider()
    provider.initialize(
        "session-1",
        hermes_home="/tmp/hermes-home",
        agent_identity="clara",
        agent_workspace="hermes",
    )

    with pytest.raises(ValueError) as excinfo:
        provider._validate_semantic_recall_scope(
            {"type": "workspace", "key": "quietfirm"},
            {"type": "agent", "key": "clara"},
        )
    message = str(excinfo.value)
    # The refusal must name the workspace and the flag, not a sibling-agent cause.
    assert "workspace/quietfirm" in message
    assert "PALACEOFTRUTH_INCLUDE_ALL_PERMITTED_WORKSPACE_SCOPES" in message
    assert "sibling-agent" not in message


def test_sar1381_replay_fixture_separates_raw_capture_from_curated_facts() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "retrieval_replay"
        / "sar1381_capture_fact_split.json"
    )
    fixture_text = fixture_path.read_text()
    fixture = json.loads(fixture_text)

    assert fixture["raw_capture"]["source_type"] == "media"
    assert fixture["raw_capture"]["embedding_count"] > 0
    assert fixture["raw_capture"]["memory_entry_count"] == 0
    assert fixture["competing_curated_entries"]
    assert all(entry.get("fact_kind") for entry in fixture["competing_curated_entries"])
    assert "bd26babe-c135-4e17-b1c1-be82ae0f1b8b" not in fixture_text


def test_sar1381_tool_contract_routes_captures_facts_and_canaries(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    schemas = {schema["name"]: schema for schema in provider.get_tool_schemas()}
    prompt = provider.system_prompt_block()

    assert "palace_fact_recall" in schemas
    assert "palace_semantic_recall" in schemas
    assert "captured URL" in schemas["palace_search"]["description"]
    assert "curated temporal fact" in schemas["palace_fact_recall"]["description"]
    assert "compatibility alias" in schemas["palace_semantic_recall"]["description"]
    assert "captured URL, video, transcript, document, title, or source" in prompt
    assert "palace_fact_recall" in prompt
    assert "exact-scope canary" in prompt


@pytest.mark.parametrize("tool_name", ["palace_fact_recall", "palace_semantic_recall"])
def test_sar1381_fact_tool_and_alias_report_curated_fact_mode(
    monkeypatch, tool_name: str
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        assert method == "POST"
        assert path == "/api/v1/memory/semantic-recall"
        assert payload["valid_at"] == "2026-08-29T00:00:00Z"
        return {
            "trace": {"searched_scope": {"type": "agent", "key": "lux"}},
            "items": [],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(
        provider.handle_tool_call(
            tool_name,
            {"query": "current inference host", "valid_at": "2026-08-29T00:00:00Z"},
        )
    )

    assert result["ok"] is True
    assert result["retrieval_mode"] == "fact_token_overlap"
    assert result["corpus_class"] == "curated_memory_entry"
    assert result["searched_scopes"] == ["agent/lux"]
    assert result["broader_authorized_fallback_used"] is False
    assert "curated fact memory only" in result["result"]


def test_sar1381_search_reports_hybrid_capture_corpus_and_scope(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        assert payload["corpus_class"] == "raw_capture"
        assert path in {"/api/v1/memory/retrieve-agent", "/api/v1/memory/retrieve"}
        return {
            "trace": {
                "searched_scopes": [{"type": "agent", "key": "lux"}],
                "retrieval_mode": "hybrid",
                "tenant_shared_fallback_used": False,
                "broad_corpus_searched": False,
            },
            "results": [
                {
                    "item_id": "11111111-2222-4333-8444-555555555555",
                    "title": "Fast local model serving stack - Video",
                    "source_type": "media",
                    "scope": {"type": "agent", "key": "lux"},
                    "score": 0.98,
                }
            ],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    result = json.loads(provider.handle_tool_call("palace_search", {"query": "serving video"}))

    assert result["retrieval_mode"] == "hybrid"
    assert result["corpus_class"] == "raw_capture"
    assert result["searched_scopes"] == ["agent/lux"]
    assert result["broader_authorized_fallback_used"] is False


def test_sar1381_exact_scope_serialized_output_respects_context_budget(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_CONTEXT_BUDGET_CHARS", "400")
    provider = _lux_oauth_provider(module, monkeypatch)

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        assert path == "/api/v1/memory/retrieve"
        return {
            "trace": {"retrieval_mode": "hybrid"},
            "results": [
                {
                    "item_id": f"item-{index}",
                    "title": f"Long result {index}",
                    "chunk_text": "x" * 500,
                    "scope": {"type": "agent", "key": "lux"},
                    "source_type": "media",
                    "corpus_class": "raw_capture",
                    "score": 0.9 - index / 100,
                }
                for index in range(5)
            ],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    raw = provider.handle_tool_call("palace_exact_scope_recall", {"query": "canary"})
    result = json.loads(raw)

    assert len(raw) <= 400
    assert result["truncated"] is True
    assert "omitted" in result["result"]
    assert result["retrieval_mode"] == "hybrid"
    assert result["corpus_class"] == "raw_capture"


def test_sar1381_exact_scope_reports_raw_note_and_mixed_corpus(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    responses = iter(
        [
            {
                "results": [
                    {
                        "item_id": "raw-note",
                        "title": "Captured note",
                        "source_type": "note",
                        "corpus_class": "raw_capture",
                        "scope": {"type": "agent", "key": "lux"},
                    }
                ]
            },
            {
                "results": [
                    {
                        "item_id": "raw-note",
                        "title": "Captured note",
                        "source_type": "note",
                        "corpus_class": "raw_capture",
                        "scope": {"type": "agent", "key": "lux"},
                    },
                    {
                        "item_id": "curated-note",
                        "title": "Curated fact",
                        "source_type": "note",
                        "corpus_class": "curated_memory_entry",
                        "scope": {"type": "agent", "key": "lux"},
                    },
                ]
            },
        ]
    )

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        assert path == "/api/v1/memory/retrieve"
        return next(responses)

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    raw_note = json.loads(
        provider.handle_tool_call("palace_exact_scope_recall", {"query": "raw note"})
    )
    mixed = json.loads(
        provider.handle_tool_call("palace_exact_scope_recall", {"query": "mixed note"})
    )

    assert raw_note["corpus_class"] == "raw_capture"
    assert "raw_capture" in raw_note["result"]
    assert mixed["corpus_class"] == "mixed"
    assert "raw_capture" in mixed["result"]
    assert "curated_memory_entry" in mixed["result"]


def test_sar1381_normal_and_canary_reads_share_one_per_turn_budget(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    route_calls = 0
    exact_calls = 0

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        nonlocal route_calls, exact_calls
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        if path == "/api/v1/memory/retrieve-agent":
            route_calls += 1
            return {
                "trace": {
                    "searched_scopes": [{"type": "agent", "key": "lux"}],
                    "retrieval_mode": "hybrid",
                },
                "results": [{"item_id": f"item-{route_calls}", "title": "Healthy source"}],
            }
        if path == "/api/v1/memory/retrieve":
            if payload.get("query") == "canary":
                exact_calls += 1
                return {"trace": {"retrieval_mode": "hybrid"}, "results": []}
            route_calls += 1
            return {
                "trace": {"retrieval_mode": "hybrid"},
                "results": [{"item_id": f"item-{route_calls}", "title": "Healthy source"}],
            }
        raise AssertionError(path)

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    for query in ("one", "two", "three"):
        assert json.loads(provider.handle_tool_call("palace_search", {"query": query}))["ok"]
    blocked = json.loads(provider.handle_tool_call("palace_search", {"query": "four"}))
    assert blocked["ok"] is False
    assert blocked["error"]["type"] == "PalaceReadBudgetError"
    assert route_calls == 3

    exact_blocked = json.loads(
        provider.handle_tool_call("palace_exact_scope_recall", {"query": "canary"})
    )
    assert exact_blocked["ok"] is False
    assert exact_blocked["error"]["type"] == "PalaceReadBudgetError"
    assert exact_calls == 0
    provider.sync_turn("", "")
    assert json.loads(
        provider.handle_tool_call("palace_exact_scope_recall", {"query": "canary"})
    )["ok"]
    assert exact_calls == 1


def test_sar1381_fact_cache_keys_include_scope_and_temporal_filters(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "agent/*")
    provider = _lux_oauth_provider(module, monkeypatch)
    payloads: list[dict] = []

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        assert path == "/api/v1/memory/semantic-recall"
        payloads.append(dict(payload))
        return {
            "trace": {"searched_scope": {"type": payload["scope_type"], "key": payload.get("scope_key")}},
            "items": [],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    first = provider.handle_tool_call(
        "palace_fact_recall",
        {
            "query": "same fact",
            "scope_type": "agent",
            "scope_key": "iris",
            "valid_at": "2026-08-01T00:00:00Z",
            "fact_kind_filter": ["world"],
        },
    )
    second = provider.handle_tool_call(
        "palace_semantic_recall",
        {
            "query": "same fact",
            "valid_at": "2026-08-29T00:00:00Z",
            "fact_kind_filter": ["observation"],
        },
    )

    assert first != second
    assert len(payloads) == 2
    assert payloads[0]["scope_key"] == "iris"
    assert payloads[1]["scope_key"] == "lux"
    assert payloads[0]["valid_at"] != payloads[1]["valid_at"]
    assert payloads[0]["fact_kind_filter"] != payloads[1]["fact_kind_filter"]


@pytest.mark.parametrize("tool_name", ["palace_search", "palace_exact_scope_recall"])
def test_sar1381_failed_read_output_is_bounded(monkeypatch, tool_name: str) -> None:
    module = load_palaceoftruth_plugin()
    monkeypatch.setenv("PALACEOFTRUTH_CONTEXT_BUDGET_CHARS", "300")
    provider = _lux_oauth_provider(module, monkeypatch)

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        raise RuntimeError("bounded transport failure")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    raw = provider.handle_tool_call(tool_name, {"query": "q" * 10000})

    assert len(raw) <= 300
    result = json.loads(raw)
    if tool_name == "palace_search":
        assert result["truncated"] is True
        assert "omitted" in result["result"].lower()
    else:
        assert result["ok"] is False


@pytest.mark.parametrize(
    ("tool_name", "target_path"),
    [
        ("palace_search", "/api/v1/memory/retrieve"),
        ("palace_fact_recall", "/api/v1/memory/semantic-recall"),
        ("palace_exact_scope_recall", "/api/v1/memory/retrieve"),
    ],
)
def test_sar1381_identical_concurrent_failed_reads_share_one_attempt(
    monkeypatch, tool_name: str, target_path: str
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    provider._request_json = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "agent_scope_key": "lux",
        "containment_mode": "hermes_agent",
    }
    assert provider._canonical_bound_agent_scope_key() == "lux"

    request_started = threading.Event()
    release_request = threading.Event()
    request_lock = threading.Lock()
    target_calls = 0

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        nonlocal target_calls
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        assert path == target_path
        with request_lock:
            target_calls += 1
        request_started.set()
        assert release_request.wait(timeout=2)
        raise RuntimeError("bounded shared failure")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    results: list[str] = []

    def call_tool() -> None:
        results.append(provider.handle_tool_call(tool_name, {"query": "same failed read"}))

    first = threading.Thread(target=call_tool)
    second = threading.Thread(target=call_tool)
    first.start()
    assert request_started.wait(timeout=2)
    second.start()
    release_request.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert target_calls == 1
    assert len(results) == 2
    assert results[0] == results[1]


def test_sar1381_prefetch_and_tools_share_three_read_budget(monkeypatch) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    target_calls: list[str] = []

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        if path == "/api/v1/memory/whoami":
            return {
                "tenant_id": "tenant-a",
                "auth_mode": "mcp_oauth",
                "agent_scope_key": "lux",
                "containment_mode": "hermes_agent",
            }
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        target_calls.append(path)
        if path == "/api/v1/memory/semantic-recall":
            return {
                "trace": {"searched_scope": {"type": "agent", "key": "lux"}},
                "items": [],
            }
        assert path == "/api/v1/memory/retrieve"
        return {
            "trace": {"retrieval_mode": "hybrid"},
            "results": [
                {
                    "item_id": "raw-capture",
                    "title": "Captured source",
                    "source_type": "media",
                    "corpus_class": "raw_capture",
                    "scope": {"type": "agent", "key": "lux"},
                }
            ],
        }

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    assert provider.prefetch("first prefetch")
    assert json.loads(provider.handle_tool_call("palace_search", {"query": "second search"}))["ok"]
    assert json.loads(provider.handle_tool_call("palace_fact_recall", {"query": "third fact"}))["ok"]
    assert provider.prefetch("fourth prefetch") == ""
    assert target_calls == [
        "/api/v1/memory/retrieve",
        "/api/v1/memory/retrieve",
        "/api/v1/memory/semantic-recall",
    ]


def test_sar1381_identical_concurrent_failed_prefetch_shares_one_attempt(
    monkeypatch,
) -> None:
    module = load_palaceoftruth_plugin()
    provider = _lux_oauth_provider(module, monkeypatch)
    provider._request_json = lambda *_args, **_kwargs: {  # type: ignore[attr-defined]
        "tenant_id": "tenant-a",
        "auth_mode": "mcp_oauth",
        "agent_scope_key": "lux",
        "containment_mode": "hermes_agent",
    }
    assert provider._canonical_bound_agent_scope_key() == "lux"
    request_started = threading.Event()
    release_request = threading.Event()
    target_calls = 0

    def fake_request_json(method: str, path: str, payload=None, params=None) -> dict:
        nonlocal target_calls
        if path == "/api/v1/memory/scopes":
            return {"scopes": []}
        assert path == "/api/v1/memory/retrieve"
        target_calls += 1
        request_started.set()
        assert release_request.wait(timeout=2)
        raise RuntimeError("shared prefetch failure")

    provider._request_json = fake_request_json  # type: ignore[attr-defined]
    results: list[str] = []
    first = threading.Thread(target=lambda: results.append(provider.prefetch("same prefetch")))
    second = threading.Thread(target=lambda: results.append(provider.prefetch("same prefetch")))
    first.start()
    assert request_started.wait(timeout=2)
    second.start()
    release_request.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert target_calls == 1
    assert results == ["", ""]
