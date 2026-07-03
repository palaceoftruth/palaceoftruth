from __future__ import annotations

import importlib.util
import io
import json
import logging
import sys
import types
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATH = (
    REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth" / "__init__.py"
)


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
                "candidate_limit": 20,
                "broad_candidate_limit": 20,
                "display_limit": 12,
                "context_budget_chars": 4000,
                "agent_scope_key": "sterling",
                "include_agent_scope_patterns": [],
                "agent_scope_pattern_limit": 5,
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

    assert tool_names == {"palace_search", "palace_remember", "palace_remember_bulk"}
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

    provider._retrieve_text = lambda *_args: (_ for _ in ()).throw(
        module.PalaceCircuitOpenError(12)
    )  # type: ignore[method-assign]

    result = json.loads(provider.handle_tool_call("palace_search", {"query": "palace status"}))

    assert result == {
        "ok": False,
        "query": "palace status",
        "error": {
            "type": "PalaceCircuitOpenError",
            "message": "Palace of Truth circuit is open; retry after 12 seconds",
            "retry_after_seconds": 12,
            "retryable": True,
        },
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
        provider._request_json("POST", "/api/v1/memory/entries", {"bad": True})
    assert calls == 1


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
