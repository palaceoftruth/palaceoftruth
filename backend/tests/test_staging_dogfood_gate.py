import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_secondbrain_staging.py"
SPEC = importlib.util.spec_from_file_location("benchmark_secondbrain_staging", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
benchmark_module = importlib.util.module_from_spec(SPEC)
sys.modules["benchmark_secondbrain_staging"] = benchmark_module
SPEC.loader.exec_module(benchmark_module)
build_dogfood_gate_report = benchmark_module.build_dogfood_gate_report


def test_benchmark_client_attaches_mcp_scope_headers(monkeypatch) -> None:
    captured = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured.append(dict(request.header_items()))
        return FakeResponse()

    monkeypatch.setattr(benchmark_module.urllib.request, "urlopen", fake_urlopen)
    client = benchmark_module.Client(base_url="https://api.palace.test", api_key="secret")

    client.request("GET", "/api/v1/memory/whoami")
    client.request("POST", "/api/v1/memory/entries", body={"scope": {"type": "workspace", "key": "palaceoftruth"}})

    assert captured[0]["X-mcp-scope"] == "read"
    assert captured[0]["X-mcp-scopes"] == "read"
    assert captured[1]["X-mcp-scope"] == "write"
    assert captured[1]["X-mcp-scopes"] == "write,write:workspace"


def _clean_control_tower() -> dict:
    return {
        "room_artifacts": {
            "blocked_rooms": 0,
            "closets": {"fresh": 3, "stale": 0},
            "snapshots": {"fresh": 3, "stale": 0},
            "tunnels": {"fresh": 3, "stale": 0},
        },
        "wakeup_briefs": {"stale": 0, "recent_briefs": []},
        "diary_rollups": {"stale": 0},
        "memory_health": {"queued": 0, "processing": 0, "failed": 0, "retryable": 0},
        "webhook_health": {"pending": 0, "failed_jobs": 0, "retryable_jobs": 0},
        "worker_backpressure": {
            "queues": [
                {
                    "key": "memory",
                    "queued_depth": 0,
                    "deferred_depth": 0,
                    "worker_queue_depth": 0,
                    "recent_failed": 0,
                    "telemetry_error": None,
                },
                {
                    "key": "palace_builds",
                    "queued_depth": 0,
                    "deferred_depth": 0,
                    "worker_queue_depth": 0,
                    "recent_failed": 0,
                    "telemetry_error": None,
                },
            ]
        },
    }


def test_staging_dogfood_gate_passes_clean_state() -> None:
    report = build_dogfood_gate_report(
        palace={
            "dirty_generation": 12,
            "indexed_generation": 12,
            "backlog_generation": 0,
            "active_palace_run": None,
        },
        control_tower=_clean_control_tower(),
        retrieval_checks=[
            {
                "name": "exact retrieval",
                "total": 1,
                "trace": {"fallback_used": False, "completeness_warning": None},
                "results": [{"tags": ["benchmark", "benchmark-run-clean"], "source_url": "benchmark://clean/0000"}],
                "required_tags": ["benchmark", "benchmark-run-clean"],
            }
        ],
        hit_ratios={"retrieval": 1.0},
        min_hit_ratio=1.0,
    )

    assert report["passed"] is True
    assert report["failures"] == []


def test_staging_dogfood_gate_fails_stale_or_fallback_state() -> None:
    control_tower = _clean_control_tower()
    control_tower["room_artifacts"]["blocked_rooms"] = 1
    control_tower["room_artifacts"]["blocked_room_samples"] = [
        {"room_name": "NIST SP 800-207", "membership_generation": 14, "snapshot_generation": 12}
    ]
    control_tower["wakeup_briefs"] = {
        "stale": 1,
        "recent_briefs": [{"title": "Daily wake-up", "stale": True}],
    }
    control_tower["worker_backpressure"]["queues"][0]["queued_depth"] = 2
    report = build_dogfood_gate_report(
        palace={
            "dirty_generation": 13,
            "indexed_generation": 12,
            "backlog_generation": 1,
            "active_palace_run": {"status": "routing"},
        },
        control_tower=control_tower,
        retrieval_checks=[
            {
                "name": "exact retrieval",
                "total": 1,
                "trace": {"fallback_used": True, "completeness_warning": "Room content is still indexing."},
                "results": [{"tags": ["wake-up-brief"], "source_url": "memory://wake-up/tenant.md"}],
                "required_tags": ["benchmark", "benchmark-run-stale"],
            }
        ],
        hit_ratios={"retrieval": 0.0},
        min_hit_ratio=1.0,
    )

    assert report["passed"] is False
    assert any("dirty_generation" in failure for failure in report["failures"])
    assert any("blocked_rooms=1" in failure for failure in report["failures"])
    assert any("NIST SP 800-207" in failure for failure in report["failures"])
    assert any("Wake-up briefs are stale" in failure for failure in report["failures"])
    assert any("Worker queue memory is not drained" in failure for failure in report["failures"])
    assert any("used global fallback" in failure for failure in report["failures"])
    assert any("wake-up briefs instead of expected source items" in failure for failure in report["failures"])
    assert any("hit ratio 0.00 is below 1.00" in failure for failure in report["failures"])
