from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.schemas.memory import MemoryScope
from app.services.codex_memory_ingestion import (
    CodexMemoryIngestionConfig,
    expand_codex_memory_paths,
    sweep_codex_memory,
)
from app.services.codex_memory_import import codex_memory_result_to_dry_run_json, normalize_codex_memory_files


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "import_codex_memory_to_palace.py"


def _scope() -> MemoryScope:
    return MemoryScope(type="agent", key="codex")


def _write_memory_file(path: Path, *, body: str = "remember the routing contract") -> None:
    path.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace automation",
                "",
                f"- Import task: {body}",
                "  - desc: Canonical Codex memory should be ingested into Palace.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _load_script_module():
    spec = importlib.util.spec_from_file_location("import_codex_memory_to_palace_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expand_codex_memory_paths_expands_user_and_deduplicates_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_home = tmp_path / "home"
    memory_root = fake_home / ".codex" / "memories"
    rollout_dir = memory_root / "rollout_summaries"
    rollout_dir.mkdir(parents=True)
    memory_file = memory_root / "MEMORY.md"
    rollout_file = rollout_dir / "2026-05-06T12-00-00-palace.jsonl"
    memory_file.write_text("# Memory\n", encoding="utf-8")
    rollout_file.write_text('{"event":"ok"}\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))

    paths = expand_codex_memory_paths(
        [Path("~/.codex/memories"), memory_file],
        glob_pattern="rollout_summaries/*.jsonl",
    )

    assert paths == [memory_file, rollout_file]
    assert all(path.is_absolute() for path in paths)


def test_sweep_dry_run_reports_records_without_writing(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"dry-run sweep unexpectedly wrote {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = sweep_codex_memory(
        [memory_file],
        tenant_id="tenant-a",
        scope=_scope(),
        config=CodexMemoryIngestionConfig(api_base_url="https://api.palace.test", api_key="tenant-key"),
        client=client,
    )

    assert report.dry_run is True
    assert report.path_count == 1
    assert report.record_count == 1
    assert report.write_count == 0
    assert report.write_error_count == 0
    assert report.records[0].entry.tenant_id == "tenant-a"
    assert report.records[0].entry.scope == _scope()


def test_sweep_write_posts_canonical_memory_entries_with_api_key(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file, body="post this canonical entry")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["tenant_id"] == "tenant-a"
        assert payload["scope"] == {"type": "agent", "key": "codex"}
        assert payload["source"] in {"codex_memory", "codex-local-memory"}
        assert payload["idempotency_key"]
        assert payload["relationship_policy"] == "deferred"
        assert "codex" in payload["tags"][0]
        return httpx.Response(
            202,
            json={"job_id": "job-1", "status": "queued", "accepted_as": "canonical"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = sweep_codex_memory(
        [memory_file],
        tenant_id="tenant-a",
        scope=_scope(),
        write=True,
        config=CodexMemoryIngestionConfig(api_base_url="https://api.palace.test", api_key="tenant-key"),
        client=client,
    )

    assert report.dry_run is False
    assert report.write_count == 1
    assert report.write_error_count == 0
    assert requests[0].url == "https://api.palace.test/api/v1/memory/entries"
    assert requests[0].headers["X-API-Key"] == "tenant-key"
    assert requests[0].headers["X-MCP-Scope"] == "write"
    assert requests[0].headers["X-MCP-Scopes"] == "write,write:agent"


def test_sweep_write_missing_api_config_fails_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file)
    monkeypatch.delenv("PALACEOFTRUTH_API_BASE_URL", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_BASE_URL", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_BASE_URL", raising=False)
    monkeypatch.delenv("SECONDBRAIN_BASE_URL", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    with pytest.raises(ValueError, match="(?i)api.*(base|key)|config"):
        sweep_codex_memory([memory_file], tenant_id="tenant-a", scope=_scope(), write=True)


def test_sweep_lock_refuses_concurrent_invocation(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    lock_file = tmp_path / "codex-memory.lock"
    _write_memory_file(memory_file)
    lock_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"already held at .*codex-memory\.lock.*pid"):
        sweep_codex_memory(
            [memory_file],
            tenant_id="tenant-a",
            scope=_scope(),
            lock_path=lock_file,
        )


def test_sweep_recovers_dead_pid_lock_without_deleting_source(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    lock_file = tmp_path / "codex-memory.lock"
    _write_memory_file(memory_file)
    lock_file.write_text("999999\n", encoding="utf-8")

    report = sweep_codex_memory(
        [memory_file],
        tenant_id="tenant-a",
        scope=_scope(),
        lock_path=lock_file,
    )

    assert report.record_count == 1
    assert memory_file.exists()
    assert not lock_file.exists()


def test_dry_run_output_redacts_body_by_default(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    secret_body = "operator-only body that should stay private"
    _write_memory_file(memory_file, body=secret_body)

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a", scope=_scope())
    payload = codex_memory_result_to_dry_run_json(result)
    included = codex_memory_result_to_dry_run_json(result, include_body=True)

    redacted_json = json.dumps(payload)
    included_json = json.dumps(included)
    assert payload["dry_run"] is True
    assert payload["would_write"] is False
    assert payload["record_count"] == 1
    assert secret_body not in redacted_json
    assert "<redacted" in redacted_json
    assert secret_body in included_json


def test_script_dry_run_output_redacts_body_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    memory_file = tmp_path / "MEMORY.md"
    private_body = "raw local Codex memory body"
    _write_memory_file(memory_file, body=private_body)
    module = _load_script_module()

    result = module.main(
        [
            "dry-run",
            str(memory_file),
            "--tenant-id",
            "tenant-a",
            "--scope-type",
            "agent",
            "--scope-key",
            "codex",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["record_count"] == 1
    assert private_body not in captured.out
    assert "<redacted" in captured.out


def test_script_mcp_argument_mapping_omits_tenant_id(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file, body="send this through MCP")
    result = normalize_codex_memory_files(
        [memory_file],
        tenant_id="tenant-a",
        scope=_scope(),
        tags=["migration-staging"],
    )
    module = _load_script_module()

    arguments = module._entry_to_mcp_arguments(result.records[0].entry)

    assert "tenant_id" not in arguments
    assert arguments["title"] == "Import task"
    assert arguments["body"].startswith("send this through MCP")
    assert arguments["source"] == "codex_memory"
    assert arguments["scope_type"] == "agent"
    assert arguments["scope_key"] == "codex"
    assert arguments["relationship_policy"] == "deferred"
    assert "migration-staging" in arguments["tags"]
    assert arguments["idempotency_key"].startswith("codex-memory:")


def test_script_mcp_http_dry_run_does_not_connect(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    memory_file = tmp_path / "MEMORY.md"
    private_body = "MCP dry run should redact this body"
    _write_memory_file(memory_file, body=private_body)
    module = _load_script_module()

    result = module.main(
        [
            "mcp-http",
            str(memory_file),
            "--tenant-id",
            "tenant-a",
            "--scope-type",
            "agent",
            "--scope-key",
            "codex",
            "--mcp-url",
            "https://mcp.palace.test/mcp",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert result == 0
    assert payload["transport"] == "mcp-http"
    assert payload["dry_run"] is True
    assert payload["would_write"] is False
    assert payload["record_count"] == 1
    assert private_body not in captured.out


def test_script_pid_lock_recovers_dead_pid_and_preserves_source(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    lock_file = tmp_path / "codex-memory-import.lock"
    _write_memory_file(memory_file)
    lock_file.write_text("999999\n", encoding="utf-8")
    module = _load_script_module()

    with module._pid_lock(lock_file):
        assert memory_file.exists()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())

    assert memory_file.exists()
    assert not lock_file.exists()


def test_script_pid_lock_reports_active_holder(tmp_path: Path) -> None:
    lock_file = tmp_path / "codex-memory-import.lock"
    lock_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    module = _load_script_module()

    with pytest.raises(RuntimeError, match=r"already held at .*codex-memory-import\.lock.*pid"):
        with module._pid_lock(lock_file):
            raise AssertionError("active lock should not be acquired")


def test_script_mcp_write_uses_create_memory_entry_without_api_key(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(
        memory_file,
        body="future runs should verify MCP write mapping without requiring a local REST API key",
    )
    module = _load_script_module()
    result = module._combined_result(
        argparse_namespace(
            paths=[memory_file],
            tenant_id="tenant-a",
            scope_type="agent",
            scope_key="codex",
            tag=["migration-staging"],
            relationship_policy="deferred",
            max_body_chars=20_000,
            glob="rollout_summaries/*.md",
        ),
        module._load_services(),
    )
    entries = module._records_from_combined_result(result)
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeMcpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, object] | None = None):
            calls.append((name, arguments or {}))
            if name == "whoami":
                return {"tenant_id": "tenant-a"}
            return {"status": "queued", "job_id": "job-1", "accepted_as": "created"}

    report = module.asyncio.run(module._write_entries_with_mcp_client(entries, FakeMcpClient()))

    assert report["transport"] == "mcp-http"
    assert report["tenant_id"] == "tenant-a"
    assert report["write_count"] == 1
    assert report["write_error_count"] == 0
    assert calls[0] == ("whoami", {})
    assert calls[1][0] == "create_memory_entry"
    assert "tenant_id" not in calls[1][1]
    assert calls[1][1]["scope_type"] == "agent"
    assert calls[1][1]["scope_key"] == "codex"
    assert calls[1][1]["relationship_policy"] == "deferred"


def test_import_script_raw_write_headers_include_scope_specific_grant() -> None:
    module = _load_script_module()

    assert module._memory_entry_scope_headers({"scope": {"type": "workspace", "key": "palaceoftruth"}}) == {
        "X-MCP-Scope": "write",
        "X-MCP-Scopes": "write,write:workspace",
    }
    assert module._memory_entry_scope_headers({"scope": {"type": "tenant_shared"}}) == {
        "X-MCP-Scope": "write",
        "X-MCP-Scopes": "write",
    }


def test_script_dry_run_excludes_rollout_summaries_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    memory_root = tmp_path / "memories"
    rollout_dir = memory_root / "rollout_summaries"
    rollout_dir.mkdir(parents=True)
    _write_memory_file(
        memory_root / "MEMORY.md",
        body="future runs should import root Codex memory before considering rollout audit details",
    )
    (rollout_dir / "2026-05-06T12-00-00-palace.md").write_text(
        "\n".join(
            [
                "# Rollout",
                "",
                "- Rollout detail: future runs should include this only with the rollout flag",
                "",
            ]
        ),
        encoding="utf-8",
    )
    module = _load_script_module()

    result = module.main(["dry-run", str(memory_root), "--tenant-id", "tenant-a"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["record_count"] == 1
    assert payload["include_rollout_summaries"] is False
    assert payload["source_counts"] == {"memory_md": 1}


def test_script_dry_run_includes_rollout_summaries_when_requested(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    memory_root = tmp_path / "memories"
    rollout_dir = memory_root / "rollout_summaries"
    rollout_dir.mkdir(parents=True)
    _write_memory_file(
        memory_root / "MEMORY.md",
        body="future runs should import root Codex memory before considering rollout audit details",
    )
    (rollout_dir / "2026-05-06T12-00-00-palace.md").write_text(
        "\n".join(
            [
                "# Rollout",
                "",
                "- Rollout detail: future runs should include this only with the rollout flag",
                "",
            ]
        ),
        encoding="utf-8",
    )
    module = _load_script_module()

    result = module.main(
        [
            "dry-run",
            str(memory_root),
            "--tenant-id",
            "tenant-a",
            "--include-rollout-summaries",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["record_count"] == 2
    assert payload["include_rollout_summaries"] is True
    assert payload["source_counts"] == {"memory_md": 1, "rollout_summary": 1}


def test_script_write_blocks_when_low_signal_entries_are_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- Low-signal note: ok",
                "",
                "- Durable import: future runs should retain this high-signal write candidate",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PALACEOFTRUTH_API_BASE_URL", "https://api.palace.test")
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "tenant-key")
    module = _load_script_module()

    with pytest.raises(SystemExit) as exc_info:
        module.main(["sweep", str(memory_file), "--tenant-id", "tenant-a", "--write"])

    payload = json.loads(capsys.readouterr().out)
    assert exc_info.value.code == 1
    assert payload["ok"] is False
    assert payload["error"]["code"] == "error"
    assert "quality gate failed" in payload["error"]["message"]


def test_script_allow_low_signal_writes_only_retained_records(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- Low-signal note: ok",
                "",
                "- Durable import: future runs should retain this high-signal write candidate",
                "",
            ]
        ),
        encoding="utf-8",
    )
    module = _load_script_module()
    posted_titles: list[str] = []

    def fake_write_entries(entries: list[object], args: object) -> dict[str, object]:
        posted_titles.extend(entry.title for entry in entries)
        return {
            "write_count": len(entries),
            "write_error_count": 0,
            "writes": [],
            "write_errors": [],
        }

    monkeypatch.setattr(module, "_write_entries", fake_write_entries)

    result = module.main(
        [
            "sweep",
            str(memory_file),
            "--tenant-id",
            "tenant-a",
            "--write",
            "--allow-low-signal",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert posted_titles == ["Durable import"]
    assert payload["low_signal_count"] == 1
    assert payload["write_count"] == 1


def test_script_freshness_report_local_only_reports_file_mtimes_and_counts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    memory_root = tmp_path / "memories"
    memory_root.mkdir()
    memory_file = memory_root / "MEMORY.md"
    summary_file = memory_root / "memory_summary.md"
    _write_memory_file(memory_file, body="future runs should compare root memory mtime")
    _write_memory_file(summary_file, body="future runs should compare summary mtime")
    older = datetime(2026, 5, 13, 12, 0, tzinfo=timezone.utc).timestamp()
    newer = datetime(2026, 5, 13, 13, 30, tzinfo=timezone.utc).timestamp()
    os.utime(memory_file, (older, older))
    os.utime(summary_file, (newer, newer))
    module = _load_script_module()

    result = module.main(["freshness-report", str(memory_root), "--tenant-id", "tenant-a", "--local-only"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["report"] == "codex-memory-freshness"
    assert payload["would_write"] is False
    assert payload["palace"] is None
    assert payload["freshness"] == {"state": "local_only", "reason": "palace_check_not_requested"}
    assert payload["local"]["record_count"] == 2
    assert payload["local"]["source_counts"] == {"memory_md": 1, "memory_summary": 1}
    assert payload["local"]["latest_source_mtime"] == "2026-05-13T13:30:00Z"
    assert {Path(item["path"]).name for item in payload["local"]["source_files"]} == {"MEMORY.md", "memory_summary.md"}


def test_script_freshness_report_uses_read_only_mcp_listing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file, body="future runs should list Palace imports without writing")
    local_time = datetime(2026, 5, 13, 13, 0, tzinfo=timezone.utc).timestamp()
    os.utime(memory_file, (local_time, local_time))
    module = _load_script_module()
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeMcpClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, object] | None = None):
            calls.append((name, arguments or {}))
            if name == "whoami":
                return {"tenant_id": "tenant-a"}
            if name == "list_memory_entries":
                return {
                    "total": 1,
                    "limit": 10,
                    "entries": [
                        {
                            "title": "Imported Codex memory",
                            "source": "codex_memory",
                            "source_url": "file:///home/example/.codex/memories/MEMORY.md#L5",
                            "scope": {"type": "agent", "key": "codex"},
                            "tags": ["codex-local-memory"],
                            "created_at": "2026-05-13T13:05:00Z",
                            "updated_at": "2026-05-13T13:05:00Z",
                            "readiness_state": "ready",
                        }
                    ],
                }
            raise AssertionError(f"unexpected MCP call {name}")

    monkeypatch.setattr(module, "_StreamableMcpClient", FakeMcpClient)

    result = module.main(
        [
            "freshness-report",
            str(memory_file),
            "--tenant-id",
            "tenant-a",
            "--scope-type",
            "agent",
            "--scope-key",
            "codex",
            "--mcp-url",
            "https://mcp.palace.test/mcp",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert [name for name, _ in calls] == ["whoami", "list_memory_entries"]
    assert calls[1][1] == {
        "scope_type": "agent",
        "scope_key": "codex",
        "tags": ["codex-local-memory"],
        "tags_mode": "all",
        "limit": 10,
    }
    assert payload["palace"]["latest_import_at"] == "2026-05-13T13:05:00Z"
    assert payload["freshness"] == {
        "state": "fresh",
        "reason": "palace_import_is_at_least_as_new_as_local",
        "lag_seconds": -300,
    }


def test_script_freshness_report_marks_stale_when_local_memory_is_newer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file, body="future runs should flag stale Palace imports")
    local_time = datetime(2026, 5, 13, 14, 0, tzinfo=timezone.utc).timestamp()
    os.utime(memory_file, (local_time, local_time))
    module = _load_script_module()

    class FakeMcpClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, object] | None = None):
            if name == "whoami":
                return {"tenant_id": "tenant-a"}
            return {
                "total": 1,
                "limit": 10,
                "entries": [
                    {
                        "title": "Imported Codex memory",
                        "source": "codex_memory",
                        "created_at": "2026-05-13T13:45:00Z",
                        "updated_at": "2026-05-13T13:45:00Z",
                        "readiness_state": "ready",
                        "tags": ["codex-local-memory"],
                    }
                ],
            }

    monkeypatch.setattr(module, "_StreamableMcpClient", FakeMcpClient)

    result = module.main(["freshness-report", str(memory_file), "--tenant-id", "tenant-a"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["freshness"] == {
        "state": "stale",
        "reason": "local_memory_newer_than_palace",
        "lag_seconds": 900,
    }


def test_script_freshness_report_returns_structured_mcp_error_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_file = tmp_path / "MEMORY.md"
    _write_memory_file(memory_file, body="future runs should keep freshness failures non-destructive")
    module = _load_script_module()
    calls: list[str] = []

    class FakeMcpClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def call_tool(self, name: str, arguments: dict[str, object] | None = None):
            calls.append(name)
            raise RuntimeError("MCP unavailable")

    monkeypatch.setattr(module, "_StreamableMcpClient", FakeMcpClient)

    result = module.main(["freshness-report", str(memory_file), "--tenant-id", "tenant-a"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 1
    assert calls == ["whoami"]
    assert payload["would_write"] is False
    assert payload["palace"]["error"]["code"] == "palace_freshness_check_failed"
    assert payload["freshness"] == {"state": "unknown", "reason": "palace_check_failed"}


def argparse_namespace(**kwargs: object):
    import argparse

    return argparse.Namespace(**kwargs)
