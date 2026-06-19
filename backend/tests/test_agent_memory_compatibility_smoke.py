import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "smoke_agent_memory_compatibility.py"
SPEC = importlib.util.spec_from_file_location("smoke_agent_memory_compatibility", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
smoke_module = importlib.util.module_from_spec(SPEC)
sys.modules["smoke_agent_memory_compatibility"] = smoke_module
SPEC.loader.exec_module(smoke_module)


def test_smoke_matrix_covers_rest_and_mcp_memory_flow() -> None:
    steps = {row["step"]: row for row in smoke_module.SMOKE_MATRIX}

    assert set(steps) == {
        "whoami",
        "write",
        "poll",
        "backfill",
        "retrieve",
        "list_entries",
        "context",
        "jobs",
    }
    assert steps["write"]["rest"] == "POST /api/v1/memory/entries"
    assert steps["write"]["mcp"] == "create_memory_entry tool"
    assert "stdio" in steps["whoami"]["mcp"]
    assert steps["backfill"]["mcp"] == "backfill_deferred_relationships tool"
    assert steps["list_entries"]["mcp"] == "list_memory_entries tool"
    assert steps["jobs"]["mcp"] == "list_memory_jobs tool"
    assert "retry" not in steps["jobs"]["proves"]


def test_make_memory_entry_builds_scoped_canonical_payload() -> None:
    entry = smoke_module.make_memory_entry(
        tenant_id="tenant-a",
        run_id="20260430-smoke",
        scope_type="workspace",
        scope_key="launch-pad",
        relationship_policy="deferred",
    )

    assert entry["tenant_id"] == "tenant-a"
    assert entry["source"] == "agent-memory-smoke"
    assert entry["scope"] == {"type": "workspace", "key": "launch-pad"}
    assert entry["relationship_policy"] == "deferred"
    assert entry["idempotency_key"] == "agent-memory-smoke:20260430-smoke"
    assert "agent-memory-smoke-20260430-smoke" in entry["tags"]


def test_make_memory_entry_can_filter_by_active_skill_tag() -> None:
    entry = smoke_module.make_memory_entry(
        tenant_id="tenant-a",
        run_id="20260430-smoke",
        scope_type="agent",
        scope_key="codex",
        relationship_policy="deferred",
        active_skills=["Codex Automation Handoff", "github:yeet", "Codex Automation Handoff"],
    )

    assert entry["tags"] == [
        "agent-memory-smoke",
        "agent-memory-smoke-20260430-smoke",
        "skill-codex-automation-handoff",
        "skill-github-yeet",
    ]
    assert entry["metadata"]["active_skills"] == ["Codex Automation Handoff", "github:yeet"]
    assert smoke_module.memory_entry_listing_query(entry, "20260430-smoke")["tags"] == [
        "agent-memory-smoke-20260430-smoke",
        "skill-codex-automation-handoff",
        "skill-github-yeet",
    ]


def test_make_memory_entry_rejects_invalid_scope_shape() -> None:
    with pytest.raises(SystemExit, match="--scope-key is required"):
        smoke_module.make_memory_entry(
            tenant_id="tenant-a",
            run_id="20260430-smoke",
            scope_type="workspace",
            scope_key=None,
            relationship_policy="immediate",
        )

    with pytest.raises(SystemExit, match="must be omitted"):
        smoke_module.make_memory_entry(
            tenant_id="tenant-a",
            run_id="20260430-smoke",
            scope_type="tenant_shared",
            scope_key="launch-pad",
            relationship_policy="immediate",
        )


def test_run_rest_smoke_exercises_canonical_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, base_url: str, api_key: str) -> None:
            assert base_url == "https://api.palaceoftruth.test"
            assert api_key == "secret"

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            requests.append((method, path, body, query))
            if (method, path) == ("GET", "/api/v1/memory/whoami"):
                return {"status": "ok", "tenant_id": "tenant-a"}
            if (method, path) == ("POST", "/api/v1/memory/entries"):
                assert body is not None
                assert body["tenant_id"] == "tenant-a"
                assert body["scope"] == {"type": "workspace", "key": "agent-memory-smoke"}
                return {"job_id": "job-1", "status": "queued", "accepted_as": "canonical"}
            if (method, path) == ("GET", "/api/v1/memory/jobs/job-1"):
                return {"job_id": "job-1", "status": "complete"}
            if (method, path) == ("POST", "/api/v1/memory/retrieve"):
                assert body is not None
                assert body["tags"] == ["agent-memory-smoke-20260430-smoke"]
                assert body["tags_mode"] == "all"
                return {"results": [{"title": "Agent memory compatibility smoke"}], "total": 1}
            if (method, path) == ("GET", "/api/v1/memory/entries"):
                assert query == {
                    "scope_type": "workspace",
                    "scope_key": "agent-memory-smoke",
                    "tags": ["agent-memory-smoke-20260430-smoke"],
                    "tags_mode": "all",
                    "limit": 10,
                }
                return {"entries": [{"title": "Agent memory compatibility smoke"}], "total": 1}
            if (method, path) == ("GET", "/api/v1/memory/wakeup-brief"):
                return {"freshness": "fresh"}
            if (method, path) == ("GET", "/api/v1/memory/jobs"):
                assert query == {"page": 1, "per_page": 10}
                return {"jobs": [{"job_id": "job-1", "status": "complete"}], "total": 1}
            raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "--api-key",
            "secret",
            "rest",
            "--run-id",
            "20260430-smoke",
            "--job-interval-seconds",
            "0",
        ]
    )

    result = smoke_module.run_rest_smoke(args)

    assert result["steps"]["whoami"] == {"status": "ok"}
    assert result["steps"]["poll"] == {"status": "complete"}
    assert result["steps"]["retrieve"] == {"status": "ok", "hit_count": 1}
    assert result["steps"]["list_entries"] == {"status": "ok", "returned": 1}
    assert [(method, path) for method, path, _, _ in requests] == [
        ("GET", "/api/v1/memory/whoami"),
        ("POST", "/api/v1/memory/entries"),
        ("GET", "/api/v1/memory/jobs/job-1"),
        ("POST", "/api/v1/memory/retrieve"),
        ("GET", "/api/v1/memory/entries"),
        ("GET", "/api/v1/memory/wakeup-brief"),
        ("GET", "/api/v1/memory/jobs"),
    ]


def test_incident_retrieval_doctor_posts_redacted_feedvalue_receipt_shelf_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, base_url: str, api_key: str) -> None:
            assert base_url == "https://api.palaceoftruth.test"
            assert api_key == "secret"

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            requests.append((method, path, body))
            assert query is None
            assert timeout == 12.0
            assert method == "POST"
            assert path == "/api/v1/memory/retrieval-doctor"
            assert body is not None
            assert body["agent_scope_key"] == "coder"
            assert body["workspace_scope_keys"] == ["feedvalue", "receipt-shelf"]
            assert body["include_tenant_shared"] is True
            assert body["include_broad_corpus"] is False
            assert body["sample_probes"] == [
                {
                    "query": "FeedValue current implementation memory",
                    "scope": {"type": "workspace", "key": "feedvalue"},
                    "expected_item_ids": [smoke_module.FEEDVALUE_INCIDENT_ITEM_ID],
                    "limit": 7,
                },
                {
                    "query": "Receipt Shelf current implementation memory",
                    "scope": {"type": "workspace", "key": "receipt-shelf"},
                    "expected_item_ids": [smoke_module.RECEIPT_SHELF_INCIDENT_ITEM_ID],
                    "limit": 7,
                },
            ]
            return {
                "status": "ok",
                "tenant_id": "tenant-a",
                "selected_scopes": [
                    {"type": "agent", "key": "coder"},
                    {"type": "workspace", "key": "feedvalue"},
                    {"type": "workspace", "key": "receipt-shelf"},
                    {"type": "tenant_shared"},
                ],
                "probes": [
                    {
                        "probe_index": 0,
                        "scope": {"type": "workspace", "key": "feedvalue"},
                        "status": "ok",
                        "expected_top_rank": 1,
                        "selected_scope_result_count": 1,
                        "top_results": [
                            {
                                "item_id": smoke_module.FEEDVALUE_INCIDENT_ITEM_ID,
                                "source_type": "note",
                                "score": 0.91,
                                "tags": ["workspace-feedvalue"],
                            }
                        ],
                    },
                    {
                        "probe_index": 1,
                        "scope": {"type": "workspace", "key": "receipt-shelf"},
                        "status": "ok",
                        "expected_top_rank": 1,
                        "selected_scope_result_count": 1,
                        "top_results": [
                            {
                                "item_id": smoke_module.RECEIPT_SHELF_INCIDENT_ITEM_ID,
                                "source_type": "note",
                                "score": 0.89,
                                "tags": ["workspace-receipt-shelf"],
                            }
                        ],
                    },
                ],
                "checks": [],
            }

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "--api-key",
            "secret",
            "incident-retrieval-doctor",
            "--agent-scope-key",
            "coder",
            "--request-timeout",
            "12",
            "--probe-limit",
            "7",
        ]
    )

    report = smoke_module.run_incident_retrieval_doctor(args)

    assert report["status"] == "ok"
    assert report["tenant_id"] == "tenant-a"
    assert report["probes"] == [
        {
            "probe_index": 0,
            "scope": {"type": "workspace", "key": "feedvalue"},
            "status": "ok",
            "expected_top_rank": 1,
            "selected_scope_result_count": 1,
            "top_result_ids": [smoke_module.FEEDVALUE_INCIDENT_ITEM_ID],
            "reasons": [],
        },
        {
            "probe_index": 1,
            "scope": {"type": "workspace", "key": "receipt-shelf"},
            "status": "ok",
            "expected_top_rank": 1,
            "selected_scope_result_count": 1,
            "top_result_ids": [smoke_module.RECEIPT_SHELF_INCIDENT_ITEM_ID],
            "reasons": [],
        },
    ]
    assert "FeedValue current implementation memory" not in str(report)
    assert "Receipt Shelf current implementation memory" not in str(report)
    assert [(method, path) for method, path, _ in requests] == [
        ("POST", "/api/v1/memory/retrieval-doctor")
    ]


def test_operator_readiness_default_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, base_url: str, api_key: str) -> None:
            assert base_url == "https://api.palaceoftruth.test"
            assert api_key == "secret"

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            requests.append((method, path, body, query))
            if (method, path) == ("GET", "/api/v1/health"):
                return {"status": "ok"}
            if (method, path) == ("GET", "/api/v1/memory/whoami"):
                return {"status": "ok", "tenant_id": "tenant-a"}
            if (method, path) == ("GET", "/api/v1/stats"):
                return {
                    "total_items": 10,
                    "ready_items": 10,
                    "indexed_items": 10,
                    "active_jobs": 0,
                    "active_memory_jobs": 0,
                    "failed_memory_jobs": 0,
                    "orphaned_ready_items": 0,
                }
            if (method, path) == ("GET", "/api/v1/palace/control-tower"):
                return {
                    "dirty_generation": 4,
                    "indexed_generation": 4,
                    "backlog_generation": 0,
                    "active_palace_run": None,
                    "room_artifacts": {"blocked_rooms": 0},
                    "wakeup_briefs": {"stale": 0},
                    "worker_backpressure": {"queues": []},
                }
            if (method, path) == ("GET", "/api/v1/memory/jobs"):
                assert query == {"page": 1, "per_page": 10}
                return {"jobs": [], "total": 0}
            raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "--api-key",
            "secret",
            "operator-readiness",
            "--run-id",
            "20260503-ready",
            "--skip-retained-corpus",
        ]
    )

    report = smoke_module.build_operator_readiness_report(args)

    assert report["status"] == "ready"
    assert report["read_only"] is True
    assert {check["name"]: check["status"] for check in report["checks"]}["live_smoke"] == "skipped"
    assert all(method == "GET" for method, _, _, _ in requests)
    assert all(
        "/admin" not in path
        and "retry" not in path
        and "relationships/backfill" not in path
        for _, path, _, _ in requests
    )


def test_codex_memory_health_is_read_only_and_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    class FakeClient:
        def __init__(self, base_url: str, api_key: str) -> None:
            assert base_url == "https://api.palaceoftruth.test"
            assert api_key == "secret"

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            requests.append((method, path, body, query))
            if (method, path) == ("GET", "/api/v1/memory/whoami"):
                return {"status": "ok", "tenant_id": "tenant-a"}
            if (method, path) == ("GET", "/api/v1/memory/scopes"):
                assert query == {"limit": 100, "sample_limit": 0}
                return {
                    "scopes": [
                        {"scope": {"type": "agent", "key": "codex"}, "entry_count": 5},
                        {"scope": {"type": "workspace", "key": "palaceoftruth"}, "entry_count": 3},
                    ],
                    "total": 2,
                }
            if (method, path) == ("GET", "/api/v1/memory/entries"):
                assert query == {"scope_type": "agent", "scope_key": "codex", "limit": 5}
                return {"entries": [{"title": "Hidden title"}], "total": 1}
            if (method, path) == ("POST", "/api/v1/memory/retrieve"):
                assert body == {
                    "query": "Codex Palace MCP memory integration test",
                    "limit": 5,
                    "context_budget_chars": 1200,
                    "scope": {"type": "agent", "key": "codex"},
                }
                return {
                    "results": [{"title": "Hidden title", "chunk_text": "Hidden body"}],
                    "trace": {"fallback_used": False},
                }
            if (method, path) == ("POST", "/api/v1/memory/retrieve-agent"):
                assert body is not None
                assert body["agent_scope_key"] == "codex"
                assert body["workspace_scope_keys"] == ["palaceoftruth"]
                assert body["include_broad_corpus"] is False
                assert body["include_tenant_shared"] is False
                return {
                    "results": [{"title": "Hidden title", "chunk_text": "Hidden body"}],
                    "trace": {
                        "searched_scopes": [
                            {"type": "agent", "key": "codex"},
                            {"type": "workspace", "key": "palaceoftruth"},
                        ],
                        "selected_scope_result_count": 2,
                        "broad_result_count": 0,
                        "deduped_result_count": 2,
                        "fallback_used": False,
                    },
                }
            raise AssertionError(f"Unexpected request: {method} {path}")

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "--api-key",
            "secret",
            "codex-memory-health",
        ]
    )

    report = smoke_module.run_codex_memory_health(args)
    report_text = str(report)

    assert report["status"] == "passed"
    assert report["read_only"] is True
    assert report["privacy"] == {
        "writes_memory": False,
        "queues_backfill": False,
        "raw_memory_content_reported": False,
    }
    assert "Hidden title" not in report_text
    assert "Hidden body" not in report_text
    assert [(method, path) for method, path, _, _ in requests] == [
        ("GET", "/api/v1/memory/whoami"),
        ("GET", "/api/v1/memory/scopes"),
        ("GET", "/api/v1/memory/entries"),
        ("POST", "/api/v1/memory/retrieve"),
        ("POST", "/api/v1/memory/retrieve-agent"),
    ]
    assert all(
        path
        not in {
            "/api/v1/memory/entries",
            "/api/v1/memory/relationships/backfill",
        }
        or method == "GET"
        for method, path, _, _ in requests
    )


def test_codex_memory_health_reports_http_500_without_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **_: Any) -> None:
            pass

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            if path == "/api/v1/memory/whoami":
                return {"tenant_id": "tenant-a"}
            if path == "/api/v1/memory/scopes":
                return {
                    "scopes": [
                        {"scope": {"type": "agent", "key": "codex"}},
                        {"scope": {"type": "workspace", "key": "palaceoftruth"}},
                    ]
                }
            if path == "/api/v1/memory/entries":
                return {"entries": [{"title": "Hidden title"}]}
            if path == "/api/v1/memory/retrieve":
                raise smoke_module.ApiError(method, path, 500, "raw sensitive traceback")
            if path == "/api/v1/memory/retrieve-agent":
                return {"results": [{"title": "Hidden title"}], "trace": {}}
            raise AssertionError(path)

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(["--api-key", "secret", "codex-memory-health"])

    report = smoke_module.run_codex_memory_health(args)
    failed = {step["name"]: step for step in report["steps"] if step["status"] == "failed"}
    report_text = str(report)

    assert report["status"] == "failed"
    assert report["failures"] == ["retrieve_memory: HTTP 500 POST /api/v1/memory/retrieve"]
    assert failed["retrieve_memory"]["http_status"] == 500
    assert "raw sensitive traceback" not in report_text


def test_operator_readiness_flags_unhealthy_control_tower(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **_: Any) -> None:
            pass

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            if path == "/api/v1/health":
                return {"status": "ok"}
            if path == "/api/v1/memory/whoami":
                return {"tenant_id": "tenant-a"}
            if path == "/api/v1/stats":
                return {"active_jobs": 0, "active_memory_jobs": 1, "failed_memory_jobs": 2}
            if path == "/api/v1/palace/control-tower":
                return {
                    "dirty_generation": 7,
                    "indexed_generation": 4,
                    "backlog_generation": 3,
                    "active_palace_run": {"run_id": "run-1"},
                    "room_artifacts": {"blocked_rooms": 2},
                    "wakeup_briefs": {"stale": 1},
                    "worker_backpressure": {"queues": [{"queue_name": "relationships", "queued_depth": 1}]},
                }
            if path == "/api/v1/memory/jobs":
                return {"jobs": [], "total": 0}
            raise AssertionError(path)

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-key",
            "secret",
            "operator-readiness",
            "--run-id",
            "20260503-bad",
            "--skip-retained-corpus",
        ]
    )

    report = smoke_module.build_operator_readiness_report(args)

    assert report["status"] == "failed"
    assert "stats: 2 failed memory jobs" in report["failures"]
    assert "stats: 1 active memory jobs" in report["failures"]
    assert "control_tower: Palace backlog generation is 3" in report["failures"]
    assert "control_tower: 2 blocked rooms" in report["failures"]


def test_activation_report_scores_read_only_onboarding_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self, **_: Any) -> None:
            pass

        def request(
            self,
            method: str,
            path: str,
            *,
            body: dict[str, Any] | None = None,
            query: dict[str, Any] | None = None,
            timeout: float = 30.0,
        ) -> Any:
            if path == "/api/v1/health":
                return {"status": "ok"}
            if path == "/api/v1/memory/whoami":
                return {"tenant_id": "tenant-a"}
            if path == "/api/v1/stats":
                return {"active_jobs": 0, "active_memory_jobs": 0, "failed_memory_jobs": 0}
            if path == "/api/v1/palace/control-tower":
                return {
                    "dirty_generation": 1,
                    "indexed_generation": 1,
                    "backlog_generation": 0,
                    "room_artifacts": {"blocked_rooms": 0},
                    "wakeup_briefs": {"stale": 0},
                    "worker_backpressure": {"queues": []},
                }
            if path == "/api/v1/memory/jobs":
                return {"jobs": [], "total": 0}
            if path == "/api/v1/memory/scopes":
                return {
                    "total": 2,
                    "limit": 100,
                    "scopes": [
                        {"scope": {"type": "agent", "key": "codex"}, "entry_count": 3},
                        {"scope": {"type": "workspace", "key": "palaceoftruth"}, "entry_count": 7},
                    ],
                }
            if path == "/api/v1/memory/trajectory":
                assert body is not None
                assert body["include_broad_corpus"] is False
                return {
                    "total": 1,
                    "current_entries": [{"title": "Current decision"}],
                    "trace": {"searched_scopes": [{"type": "agent", "key": "codex"}]},
                }
            if path == "/api/v1/memory/retrieval-doctor":
                assert body is not None
                assert body["sample_probes"][0]["scope"] == {"type": "agent", "key": "codex"}
                return {"status": "ok", "checks": [{"name": "selected_scope", "status": "ok"}]}
            raise AssertionError(path)

    monkeypatch.setattr(smoke_module, "Client", FakeClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-key",
            "secret",
            "activation-report",
            "--run-id",
            "20260527-activate",
            "--skip-retained-corpus",
        ]
    )

    report = smoke_module.build_activation_onboarding_report(args)
    categories = {category["name"]: category for category in report["categories"]}

    assert report["report"] == "palace-activation-onboarding"
    assert report["read_only"] is True
    assert report["status"] == "needs_data"
    assert set(categories) == set(smoke_module.ACTIVATION_CATEGORY_NAMES)
    assert categories["tenant_health"]["status"] == "ready"
    assert categories["scoped_memory_coverage"]["status"] == "ready"
    assert categories["conversation_trajectory_readiness"]["status"] == "ready"
    assert categories["live_graph_signal_readiness"]["status"] == "ready"
    assert categories["benchmark_artifact_freshness"]["status"] == "needs_data"
    assert "Pass --nist-run-id" in report["remediations"][0]
    assert report["privacy"]["writes_memory"] is False
    assert report["privacy"]["queues_backfill"] is False


def test_activation_report_dry_run_documents_expected_shape() -> None:
    args = smoke_module.build_parser().parse_args(
        [
            "activation-report",
            "--run-id",
            "20260527-dry",
            "--dry-run",
        ]
    )

    report = smoke_module.build_activation_onboarding_report(args)

    assert report["status"] == "dry-run"
    assert [category["name"] for category in report["categories"]] == list(smoke_module.ACTIVATION_CATEGORY_NAMES)
    assert report["activation_targets"]["workspace_scopes"] == [{"type": "workspace", "key": "palaceoftruth"}]


def test_strict_retrieve_trace_flags_fallback_and_shared_merge() -> None:
    failures = smoke_module.strict_retrieve_trace_failures(
        {
            "steps": {
                "retrieve": {
                    "status": "ok",
                    "hit_count": 1,
                    "fallback_used": True,
                    "tenant_shared_results_merged": True,
                }
            }
        }
    )

    assert failures == ["retrieval used global fallback", "retrieval merged tenant_shared results"]


def test_mcp_create_memory_arguments_omit_tenant_and_flatten_scope() -> None:
    entry = smoke_module.make_memory_entry(
        tenant_id="tenant-a",
        run_id="20260430-smoke",
        scope_type="workspace",
        scope_key="launch-pad",
        relationship_policy="deferred",
    )

    arguments = smoke_module.mcp_create_memory_arguments(entry)

    assert "tenant_id" not in arguments
    assert arguments["scope_type"] == "workspace"
    assert arguments["scope_key"] == "launch-pad"
    assert arguments["relationship_policy"] == "deferred"
    assert arguments["idempotency_key"] == "agent-memory-smoke:20260430-smoke"


def test_run_mcp_smoke_exercises_streamable_http_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeMcpClient:
        def __init__(
            self,
            *,
            url: str,
            headers: dict[str, str],
            timeout_seconds: float,
            sse_read_timeout_seconds: float,
        ) -> None:
            assert url == "https://mcp.secondbrain.test/mcp"
            assert headers == {"X-Test": "smoke"}
            assert timeout_seconds == 12.0
            assert sse_read_timeout_seconds == 34.0

        async def __aenter__(self) -> "FakeMcpClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
            args = arguments or {}
            calls.append((name, args))
            if name == "whoami":
                return {"status": "ok", "tenant_id": "tenant-a"}
            if name == "create_memory_entry":
                assert "tenant_id" not in args
                assert args["scope_type"] == "workspace"
                assert args["scope_key"] == "agent-memory-smoke"
                assert args["relationship_policy"] == "deferred"
                return {"job_id": "job-1", "status": "queued", "accepted_as": "canonical"}
            if name == "get_memory_job":
                assert args == {"job_id": "job-1"}
                return {"job_id": "job-1", "status": "complete"}
            if name == "backfill_deferred_relationships":
                assert args == {"limit": 1, "defer_seconds": 0}
                return {"status": "queued", "tenant_id": "tenant-a", "limit": 1, "defer_seconds": 0}
            if name == "retrieve_memory":
                assert args["tags"] == ["agent-memory-smoke-20260430-smoke"]
                assert args["tags_mode"] == "all"
                assert args["scope_type"] == "workspace"
                assert args["scope_key"] == "agent-memory-smoke"
                return {"results": [{"title": "Agent memory compatibility smoke"}], "total": 1}
            if name == "list_memory_entries":
                assert args == {
                    "scope_type": "workspace",
                    "scope_key": "agent-memory-smoke",
                    "tags": ["agent-memory-smoke-20260430-smoke"],
                    "tags_mode": "all",
                    "limit": 10,
                }
                return {"entries": [{"title": "Agent memory compatibility smoke"}], "total": 1}
            if name == "get_wakeup_brief":
                assert args == {"scope_type": "tenant"}
                return {"freshness": "fresh"}
            if name == "list_memory_jobs":
                assert args == {"page": 1, "per_page": 10}
                return {"jobs": [{"job_id": "job-1", "status": "complete"}], "total": 1}
            raise AssertionError(f"Unexpected MCP tool: {name}")

    monkeypatch.setattr(smoke_module, "McpClient", FakeMcpClient)
    args = smoke_module.build_parser().parse_args(
        [
            "mcp-http",
            "--mcp-url",
            "https://mcp.secondbrain.test/mcp",
            "--header",
            "X-Test=smoke",
            "--run-id",
            "20260430-smoke",
            "--request-timeout",
            "12",
            "--sse-read-timeout",
            "34",
            "--job-interval-seconds",
            "0",
        ]
    )

    result = smoke_module.run_mcp_smoke(args)

    assert result["steps"]["whoami"] == {"status": "ok"}
    assert result["steps"]["poll"] == {"status": "complete"}
    assert result["steps"]["backfill"] == {"status": "queued", "limit": 1, "defer_seconds": 0}
    assert result["steps"]["retrieve"] == {"status": "ok", "hit_count": 1}
    assert result["steps"]["list_entries"] == {"status": "ok", "returned": 1}
    assert [name for name, _ in calls] == [
        "whoami",
        "create_memory_entry",
        "get_memory_job",
        "backfill_deferred_relationships",
        "retrieve_memory",
        "list_memory_entries",
        "get_wakeup_brief",
        "list_memory_jobs",
    ]


def test_run_mcp_smoke_skips_backfill_for_immediate_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeMcpClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> "FakeMcpClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
            calls.append(name)
            if name == "whoami":
                return {"tenant_id": "tenant-a"}
            if name == "create_memory_entry":
                return {"job_id": "job-1", "status": "queued"}
            if name == "get_memory_job":
                return {"job_id": "job-1", "status": "complete"}
            if name == "retrieve_memory":
                return {"results": [{"title": "Agent memory compatibility smoke"}]}
            if name == "list_memory_entries":
                return {"entries": [{"title": "Agent memory compatibility smoke"}]}
            if name == "list_memory_jobs":
                return {"jobs": []}
            raise AssertionError(f"Unexpected MCP tool: {name}")

    monkeypatch.setattr(smoke_module, "McpClient", FakeMcpClient)
    args = smoke_module.build_parser().parse_args(
        [
            "mcp-http",
            "--run-id",
            "20260430-smoke",
            "--relationship-policy",
            "immediate",
            "--skip-wakeup-brief",
            "--job-interval-seconds",
            "0",
        ]
    )

    result = smoke_module.run_mcp_smoke(args)

    assert "backfill_deferred_relationships" not in calls
    assert result["steps"]["backfill"] == {
        "status": "skipped",
        "reason": "relationship_policy is not deferred",
    }


def test_run_mcp_stdio_smoke_launches_local_adapter_and_reuses_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    launched: dict[str, Any] = {}

    class FakeStdioMcpClient:
        def __init__(
            self,
            *,
            command: str,
            args: list[str],
            env: dict[str, str],
            cwd: str | None,
        ) -> None:
            launched["command"] = command
            launched["args"] = args
            launched["cwd"] = cwd
            launched["base_url"] = env["PALACEOFTRUTH_API_BASE_URL"]
            launched["api_key"] = env["PALACEOFTRUTH_API_KEY"]

        async def __aenter__(self) -> "FakeStdioMcpClient":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
            args = arguments or {}
            calls.append((name, args))
            if name == "whoami":
                return {"status": "ok", "tenant_id": "tenant-a"}
            if name == "create_memory_entry":
                assert args["relationship_policy"] == "deferred"
                return {"job_id": "job-1", "status": "queued", "accepted_as": "canonical"}
            if name == "get_memory_job":
                return {"job_id": "job-1", "status": "complete"}
            if name == "backfill_deferred_relationships":
                assert args == {"limit": 1, "defer_seconds": 0}
                return {"status": "queued", "limit": 1, "defer_seconds": 0}
            if name == "retrieve_memory":
                return {"results": [{"title": "Agent memory compatibility smoke"}], "total": 1}
            if name == "list_memory_entries":
                return {"entries": [{"title": "Agent memory compatibility smoke"}], "total": 1}
            if name == "get_wakeup_brief":
                return {"freshness": "fresh"}
            if name == "list_memory_jobs":
                return {"jobs": [{"job_id": "job-1", "status": "complete"}], "total": 1}
            raise AssertionError(f"Unexpected MCP tool: {name}")

    monkeypatch.setattr(smoke_module, "StdioMcpClient", FakeStdioMcpClient)
    args = smoke_module.build_parser().parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "--api-key",
            "secret",
            "mcp-stdio",
            "--stdio-command",
            "python",
            "--stdio-arg",
            "backend/scripts/palaceoftruth_mcp.py",
            "--stdio-cwd",
            "/repo",
            "--run-id",
            "20260430-smoke",
            "--job-interval-seconds",
            "0",
        ]
    )

    result = smoke_module.run_mcp_stdio_smoke(args)

    assert launched == {
        "command": "python",
        "args": ["backend/scripts/palaceoftruth_mcp.py"],
        "cwd": "/repo",
        "base_url": "https://api.palaceoftruth.test",
        "api_key": "secret",
    }
    assert result["transport"] == "stdio"
    assert result["steps"]["retrieve"] == {"status": "ok", "hit_count": 1}
    assert [name for name, _ in calls] == [
        "whoami",
        "create_memory_entry",
        "get_memory_job",
        "backfill_deferred_relationships",
        "retrieve_memory",
        "list_memory_entries",
        "get_wakeup_brief",
        "list_memory_jobs",
    ]


def test_scorecard_dry_run_scores_all_transports_without_live_calls() -> None:
    args = smoke_module.build_parser().parse_args(
        [
            "scorecard",
            "--run-id",
            "20260508-score",
            "--dry-run",
        ]
    )

    report = smoke_module.build_agent_memory_scorecard(args)

    assert report["status"] == "passed"
    assert report["score"] == report["max_score"]
    assert report["privacy"] == {
        "destructive_operations": False,
        "cleanup_automation": False,
        "raw_memory_content_reported": False,
    }
    assert [item["transport"] for item in report["results"]] == ["rest", "mcp-http", "mcp-stdio"]
    assert [item["run_id"] for item in report["results"]] == [
        "20260508-score-rest",
        "20260508-score-mcp_http",
        "20260508-score-mcp_stdio",
    ]


def test_scorecard_dry_run_can_write_json_report(tmp_path: Path) -> None:
    output = tmp_path / "scorecard.json"
    args = smoke_module.build_parser().parse_args(
        [
            "scorecard",
            "--run-id",
            "20260508-score",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    status = smoke_module.cmd_scorecard(args)

    assert status == 0
    report = smoke_module.json.loads(output.read_text())
    assert report["status"] == "passed"
    assert report["privacy"]["raw_memory_content_reported"] is False
    assert [item["transport"] for item in report["results"]] == ["rest", "mcp-http", "mcp-stdio"]


def test_scorecard_report_can_redact_memory_bodies(tmp_path: Path) -> None:
    output = tmp_path / "scorecard.json"
    args = smoke_module.build_parser().parse_args(
        [
            "scorecard",
            "--run-id",
            "20260508-score",
            "--dry-run",
            "--redact-memory-bodies",
            "--output",
            str(output),
        ]
    )

    status = smoke_module.cmd_scorecard(args)

    assert status == 0
    report_text = output.read_text()
    assert "This memory verifies" not in report_text
    report = smoke_module.json.loads(report_text)
    assert report["results"][0]["result"]["entry"]["body"] == "<redacted memory body>"
    assert report["results"][1]["result"]["create_memory_entry_arguments"]["body"] == "<redacted memory body>"


def test_benchmark_cli_compatibility_report_writes_normalized_pack(tmp_path: Path) -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_agent_memory_retrieval.py"
    spec = importlib.util.spec_from_file_location("benchmark_agent_memory_retrieval", script_path)
    assert spec is not None
    assert spec.loader is not None
    benchmark_module = importlib.util.module_from_spec(spec)
    sys.modules["benchmark_agent_memory_retrieval"] = benchmark_module
    spec.loader.exec_module(benchmark_module)

    report_output = tmp_path / "compat-report.json"
    pack_output = tmp_path / "compat-pack.json"
    args = benchmark_module.build_parser().parse_args(
        [
            "compatibility-report",
            "--output",
            str(report_output),
            "--output-pack",
            str(pack_output),
        ]
    )

    status = benchmark_module.cmd_compatibility_report(args)

    assert status == 0
    report = smoke_module.json.loads(report_output.read_text())
    normalized_pack = smoke_module.json.loads(pack_output.read_text())
    assert report["summary"]["passed"] is True
    assert report["summary"]["per_transport"]["hermes"]["case_count"] == 3
    assert normalized_pack["artifact_metadata"]["offline_report_only"] is True
    assert len(normalized_pack["cases"]) == 12


def test_codex_bridge_report_can_write_json_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_setup_report",
        lambda args: {
            "dry_run": True,
            "mutating": False,
            "skillpack": "/repo/plugins/palaceoftruth-memory/skills/palaceoftruth-codex-memory/SKILL.md",
            "live_smoke_command": [
                "python",
                "scripts/smoke_agent_memory_compatibility.py",
                "mcp-stdio",
                "--skip-backfill",
            ],
        },
    )
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_lifecycle_payload",
        lambda args: {
            "steps": [
                {
                    "tool": "retrieve_agent_memory",
                    "arguments": {
                        "agent_scope_key": "codex",
                        "workspace_scope_keys": ["palaceoftruth"],
                        "include_broad_corpus": False,
                    },
                },
                {
                    "tool": "capture_checkpoint",
                    "arguments": {"scope_type": "session", "dry_run": True},
                },
            ]
        },
    )
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_tool_names",
        lambda: sorted(smoke_module.CODEX_BRIDGE_REQUIRED_TOOLS),
    )
    output = tmp_path / "codex-bridge.json"
    args = smoke_module.build_parser().parse_args(
        [
            "codex-bridge",
            "--output",
            str(output),
        ]
    )

    status = smoke_module.cmd_codex_bridge(args)

    assert status == 0
    report = smoke_module.json.loads(output.read_text())
    assert report["status"] == "passed"
    assert report["privacy"]["production_writes_by_default"] is False
    assert report["live_smoke_requested"] is False


def test_codex_bridge_report_checks_setup_lifecycle_and_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_setup_report",
        lambda args: {
            "dry_run": True,
            "mutating": False,
            "skillpack": "/repo/plugins/palaceoftruth-memory/skills/palaceoftruth-codex-memory/SKILL.md",
            "live_smoke_command": [
                "python",
                "scripts/smoke_agent_memory_compatibility.py",
                "mcp-stdio",
                "--skip-backfill",
            ],
        },
    )
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_lifecycle_payload",
        lambda args: {
            "steps": [
                {
                    "tool": "retrieve_agent_memory",
                    "arguments": {
                        "agent_scope_key": "codex",
                        "workspace_scope_keys": ["palaceoftruth"],
                        "include_broad_corpus": False,
                    },
                },
                {
                    "tool": "capture_checkpoint",
                    "arguments": {"scope_type": "session", "dry_run": True},
                },
            ]
        },
    )
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_tool_names",
        lambda: sorted(smoke_module.CODEX_BRIDGE_REQUIRED_TOOLS | {"list_tags"}),
    )
    args = smoke_module.build_parser().parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "codex-bridge",
            "--run-id",
            "codex-bridge",
        ]
    )

    report = smoke_module.build_codex_bridge_report(args)

    assert report["status"] == "passed"
    assert report["dry_run"] is True
    assert report["privacy"] == {
        "destructive_operations": False,
        "raw_secret_output": False,
        "raw_transcript_output": False,
        "production_writes_by_default": False,
    }
    assert {check["name"]: check["status"] for check in report["checks"]} == {
        "skillpack_presence": "ok",
        "setup_verifier": "ok",
        "lifecycle_payload": "ok",
        "mcp_tool_surface": "ok",
    }


def test_codex_bridge_report_fails_closed_on_unsafe_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_setup_report",
        lambda args: {
            "dry_run": True,
            "mutating": False,
            "skillpack": "palaceoftruth-codex-memory",
            "live_smoke_command": ["mcp-stdio", "--skip-backfill"],
        },
    )
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_lifecycle_payload",
        lambda args: {
            "steps": [
                {
                    "tool": "retrieve_agent_memory",
                    "arguments": {
                        "agent_scope_key": "codex",
                        "workspace_scope_keys": ["palaceoftruth"],
                        "include_broad_corpus": False,
                    },
                },
                {
                    "tool": "capture_checkpoint",
                    "arguments": {"scope_type": "workspace", "dry_run": True},
                },
            ]
        },
    )
    monkeypatch.setattr(
        smoke_module,
        "codex_bridge_tool_names",
        lambda: sorted((smoke_module.CODEX_BRIDGE_REQUIRED_TOOLS - {"palace_context"}) | {"delete_item"}),
    )
    args = smoke_module.build_parser().parse_args(["codex-bridge"])

    report = smoke_module.build_codex_bridge_report(args)

    assert report["status"] == "failed"
    assert any("missing tools: palace_context" in failure for failure in report["failures"])
    assert any("prohibited tools: delete_item" in failure for failure in report["failures"])


def test_scorecard_captures_transport_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_rest(_: Any) -> dict[str, Any]:
        return {
            "steps": {
                "whoami": {"status": "ok"},
                "write": {"status": "queued"},
                "poll": {"status": "complete"},
                "retrieve": {"status": "ok", "hit_count": 1},
                "list_entries": {"status": "ok", "returned": 1},
                "jobs": {"status": "ok", "returned": 1},
            }
        }

    def fake_mcp(_: Any) -> dict[str, Any]:
        raise RuntimeError("mcp unavailable")

    monkeypatch.setattr(smoke_module, "run_rest_smoke", fake_rest)
    monkeypatch.setattr(smoke_module, "run_mcp_smoke", fake_mcp)
    args = smoke_module.build_parser().parse_args(
        [
            "scorecard",
            "--run-id",
            "20260508-score",
            "--transport",
            "rest",
            "--transport",
            "mcp-http",
        ]
    )

    report = smoke_module.build_agent_memory_scorecard(args)

    assert report["status"] == "failed"
    assert report["failures"] == [
        {
            "transport": "mcp-http",
            "error": "mcp unavailable",
            "failed_steps": [
                "whoami",
                "write",
                "poll",
                "backfill",
                "retrieve",
                "list_entries",
                "jobs",
            ],
        }
    ]
    assert report["results"][0]["score"]["required_steps"]["write"] == {
        "status": "passed",
    }


def test_mcp_stdio_smoke_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    args = smoke_module.build_parser().parse_args(
        [
            "mcp-stdio",
            "--run-id",
            "20260430-smoke",
        ]
    )

    with pytest.raises(SystemExit, match="API_KEY is required"):
        smoke_module.build_stdio_env(args)
