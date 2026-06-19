from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.memory import MemoryScope
from app.services.transcript_sources import (
    normalize_transcript_files,
    transcript_result_to_dry_run_json,
)


def _scope() -> MemoryScope:
    return MemoryScope(type="agent", key="codex")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_codex_jsonl_normalizes_canonical_memory_entries(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "id": "msg-1",
                "timestamp": "2026-05-05T10:00:00Z",
                "message": {"role": "user", "content": [{"type": "input_text", "text": "Wire the smoke test."}]},
                "conversation_id": "thread-1",
            },
            {
                "id": "msg-2",
                "created_at": "2026-05-05T10:00:05+00:00",
                "role": "assistant",
                "content": "Added fixture coverage.",
            },
        ],
    )

    result = normalize_transcript_files(
        [transcript],
        adapter="codex",
        tenant_id="tenant-a",
        scope=_scope(),
        tags=["import-test"],
    )

    assert result.warnings == []
    assert len(result.records) == 2
    first = result.records[0]
    assert first.source_id.startswith("codex-")
    assert first.entry.tenant_id == "tenant-a"
    assert first.entry.source == "codex_transcript"
    assert first.entry.body == "Wire the smoke test."
    assert first.entry.scope.type == "agent"
    assert first.entry.scope.key == "codex"
    assert first.entry.created_at == datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc)
    assert first.entry.idempotency_key == f"transcript:{first.source_id}"
    assert first.entry.source_url == f"file://{transcript}#L1"
    assert "agent-transcript" in first.entry.tags
    assert "transcript-codex" in first.entry.tags
    assert first.entry.metadata["transcript_source"]["transformation"] == "verbatim_transcript_record"


def test_claude_jsonl_maps_session_metadata_and_privacy(tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "uuid": "claude-msg",
                "type": "assistant",
                "timestamp": "2026-05-05T11:00:00Z",
                "sessionId": "session-a",
                "cwd": "/repo",
                "message": {"content": [{"type": "text", "text": "Use API_KEY=secret-value for the test."}]},
            }
        ],
    )

    result = normalize_transcript_files(
        [transcript],
        adapter="claude",
        tenant_id="tenant-a",
        scope=MemoryScope(type="workspace", key="palaceoftruth"),
    )

    assert result.warnings == []
    record = result.records[0]
    assert record.role == "assistant"
    assert record.privacy_classification == "sensitive"
    metadata = record.entry.metadata["transcript_source"]
    assert metadata["session_id"] == "session-a"
    assert metadata["cwd"] == "/repo"
    assert "privacy-sensitive" in record.entry.tags


def test_gemini_text_log_and_jsonl_are_supported(tmp_path: Path) -> None:
    transcript = tmp_path / "gemini.log"
    transcript.write_text(
        "[2026-05-05T12:00:00Z] User: check source adapters\n"
        "Gemini: fixture parser is ready\n"
        + json.dumps({"role": "model", "text": "json log works", "timestamp": "2026-05-05T12:00:02Z"})
        + "\n",
        encoding="utf-8",
    )

    result = normalize_transcript_files(
        [transcript],
        adapter="gemini",
        tenant_id="tenant-a",
        scope=MemoryScope(type="agent", key="gemini"),
    )

    assert result.warnings == []
    assert [record.role for record in result.records] == ["user", "assistant", "assistant"]
    assert [record.entry.body for record in result.records] == [
        "check source adapters",
        "fixture parser is ready",
        "json log works",
    ]


def test_malformed_and_oversized_records_warn_without_stopping(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(
        transcript,
        [
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "x" * 20},
        ],
    )
    transcript.write_text(transcript.read_text(encoding="utf-8") + "not-json\n", encoding="utf-8")

    result = normalize_transcript_files(
        [transcript],
        adapter="codex",
        tenant_id="tenant-a",
        scope=_scope(),
        max_body_chars=5,
    )

    assert len(result.records) == 1
    assert [(warning.line_number, warning.code) for warning in result.warnings] == [
        (2, "body_too_large"),
        (3, "malformed_record"),
    ]


def test_idempotency_is_stable_across_duplicate_runs(tmp_path: Path) -> None:
    transcript = tmp_path / "codex.jsonl"
    _write_jsonl(transcript, [{"id": "stable-id", "role": "user", "content": "same content"}])

    first = normalize_transcript_files([transcript], adapter="codex", tenant_id="tenant-a", scope=_scope())
    second = normalize_transcript_files([transcript], adapter="codex", tenant_id="tenant-a", scope=_scope())

    assert first.records[0].source_id == second.records[0].source_id
    assert first.records[0].entry.idempotency_key == second.records[0].entry.idempotency_key


def test_dry_run_redacts_body_by_default(tmp_path: Path) -> None:
    transcript = tmp_path / "gemini.log"
    transcript.write_text("User: private transcript body\n", encoding="utf-8")
    result = normalize_transcript_files([transcript], adapter="gemini", tenant_id="tenant-a", scope=_scope())

    redacted = transcript_result_to_dry_run_json(result)
    included = transcript_result_to_dry_run_json(result, include_body=True)

    assert redacted["would_write"] is False
    assert redacted["records"][0]["memory_entry"]["body"] == "<redacted:23 chars>"
    assert included["records"][0]["memory_entry"]["body"] == "private transcript body"
