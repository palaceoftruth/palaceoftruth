from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from app.schemas.memory import MemoryScope
from app.services.transcript_ingestion import (
    TranscriptIngestionConfig,
    expand_transcript_paths,
    sweep_transcripts,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "normalize_agent_transcripts.py"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _scope() -> MemoryScope:
    return MemoryScope(type="agent", key="codex")


def _load_script_module():
    spec = importlib.util.spec_from_file_location("normalize_agent_transcripts_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expand_transcript_paths_deduplicates_files_from_directories(tmp_path: Path) -> None:
    first = tmp_path / "a.jsonl"
    second = tmp_path / "nested" / "b.jsonl"
    second.parent.mkdir()
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")

    paths = expand_transcript_paths([tmp_path, first], glob_pattern="*.jsonl")

    assert paths == [first, second]


def test_sweep_dry_run_reports_records_without_writing(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(transcript, [{"id": "msg-1", "role": "user", "content": "remember the adapter contract"}])

    report = sweep_transcripts(
        [tmp_path],
        adapter="codex",
        tenant_id="tenant-a",
        scope=_scope(),
    )

    assert report.dry_run is True
    assert report.path_count == 1
    assert report.record_count == 1
    assert report.write_count == 0
    assert report.records[0].entry.idempotency_key == f"transcript:{report.records[0].source_id}"


def test_sweep_write_posts_canonical_memory_entries_with_api_key(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(transcript, [{"id": "msg-1", "role": "assistant", "content": "ship the hook"}])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["tenant_id"] == "tenant-a"
        assert payload["scope"] == {"type": "agent", "key": "codex"}
        assert payload["source"] == "codex_transcript"
        assert payload["idempotency_key"].startswith("transcript:codex-")
        return httpx.Response(
            202,
            json={"job_id": "job-1", "status": "queued", "accepted_as": "canonical"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = sweep_transcripts(
        [transcript],
        adapter="codex",
        tenant_id="tenant-a",
        scope=_scope(),
        write=True,
        config=TranscriptIngestionConfig(api_base_url="https://api.palace.test", api_key="tenant-key"),
        client=client,
    )

    assert report.dry_run is False
    assert report.write_count == 1
    assert report.write_error_count == 0
    assert requests[0].url == "https://api.palace.test/api/v1/memory/entries"
    assert requests[0].headers["X-API-Key"] == "tenant-key"
    assert requests[0].headers["X-MCP-Scope"] == "write"
    assert requests[0].headers["X-MCP-Scopes"] == "write,write:agent"


def test_duplicate_sweeps_replay_same_idempotency_key(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(transcript, [{"id": "stable", "role": "user", "content": "same"}])

    first = sweep_transcripts([transcript], adapter="codex", tenant_id="tenant-a", scope=_scope())
    second = sweep_transcripts([transcript], adapter="codex", tenant_id="tenant-a", scope=_scope())

    assert first.records[0].source_id == second.records[0].source_id
    assert first.records[0].entry.idempotency_key == second.records[0].entry.idempotency_key


def test_sweep_lock_refuses_concurrent_invocation(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    lock_file = tmp_path / "transcript.lock"
    _write_jsonl(transcript, [{"role": "user", "content": "blocked by lock"}])
    lock_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"already held at .*transcript\.lock.*pid"):
        sweep_transcripts(
            [transcript],
            adapter="codex",
            tenant_id="tenant-a",
            scope=_scope(),
            lock_path=lock_file,
        )


def test_sweep_recovers_dead_pid_lock_without_deleting_source(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    lock_file = tmp_path / "transcript.lock"
    _write_jsonl(transcript, [{"role": "user", "content": "recover stale lock"}])
    lock_file.write_text("999999\n", encoding="utf-8")

    report = sweep_transcripts(
        [transcript],
        adapter="codex",
        tenant_id="tenant-a",
        scope=_scope(),
        lock_path=lock_file,
    )

    assert report.record_count == 1
    assert transcript.exists()
    assert not lock_file.exists()


def test_hook_is_silent_without_configured_paths(capsys) -> None:
    module = _load_script_module()

    result = module.main(
        [
            "hook",
            "--adapter",
            "codex",
            "--tenant-id",
            "tenant-a",
            "--scope-type",
            "agent",
            "--scope-key",
            "codex",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == ""
    assert captured.err == ""


def test_hook_write_missing_api_config_fails_without_stdout(tmp_path: Path, capsys, monkeypatch) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(transcript, [{"role": "user", "content": "write me"}])
    module = _load_script_module()
    monkeypatch.delenv("PALACEOFTRUTH_API_BASE_URL", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_BASE_URL", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_BASE_URL", raising=False)
    monkeypatch.delenv("SECONDBRAIN_BASE_URL", raising=False)
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    monkeypatch.delenv("SECONDBRAIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    result = module.main(
        [
            "hook",
            str(transcript),
            "--adapter",
            "codex",
            "--tenant-id",
            "tenant-a",
            "--scope-type",
            "agent",
            "--scope-key",
            "codex",
            "--write",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == ""
