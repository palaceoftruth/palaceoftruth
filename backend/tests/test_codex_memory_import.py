from __future__ import annotations

from pathlib import Path

from app.services.codex_memory_import import (
    build_codex_memory_entries,
    codex_memory_result_to_dry_run_json,
    normalize_codex_memory_files,
)
from app.schemas.memory import MemoryScope
from app.services.codex_memory_privacy import detect_secret_warnings


def test_parser_normalizes_markdown_memory_entries(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## /home/example/workspace/github_palaceoftruth/palaceoftruth",
                "",
                "- Palace retained ranking fix: task 123, PR 456, retrieve_palace",
                "  - desc: Fixed explicit-tag retrieval so retained memories remain ranked.",
                "  - learnings: Confident room routes still need global retained candidates.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert result.warnings == []
    assert len(result.records) == 1

    record = result.records[0]
    assert record.source_file == str(memory_file)
    assert record.line_number == 5
    assert record.entry.tenant_id == "tenant-a"
    assert record.entry.source == "codex_memory"
    assert record.entry.title == "Palace retained ranking fix"
    assert "Fixed explicit-tag retrieval" in record.entry.body
    assert "Confident room routes" in record.entry.body
    assert record.entry.source_url == f"file://{memory_file}#L5"
    assert record.entry.metadata["codex_memory"]["section"] == (
        "/home/example/workspace/github_palaceoftruth/palaceoftruth"
    )
    assert record.entry.metadata["codex_memory"]["transformation"] == "codex_memory_markdown_entry"


def test_secret_warnings_are_reported_without_leaking_secret_values(tmp_path: Path) -> None:
    secret_label = "OPENAI_" + "API_KEY"
    secret_value = "fixture-secret-value"
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Sensitive run",
                "",
                f"- Debug note: {secret_label}={secret_value}",
                "  - desc: This row should be imported with a privacy warning.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert len(result.records) == 1
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert warning.code == "potential_secret"
    assert warning.line_number == 5
    assert secret_value not in warning.detail
    assert secret_label in warning.detail
    assert "privacy-sensitive" in result.records[0].entry.tags


def test_privacy_detector_redacts_secret_warning_previews() -> None:
    secret_value = "ghp_abcdefghijklmnopqrstuvwxyz123456"

    warnings = detect_secret_warnings(
        f"GitHub token {secret_value} was pasted during a failed import.",
        source_file="MEMORY.md",
        line_number=42,
    )

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.code == "potential_secret"
    assert warning.source_file == "MEMORY.md"
    assert warning.line_number == 42
    assert secret_value not in warning.detail
    assert "<redacted" in warning.detail


def test_dry_run_redacts_body_by_default(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- Private handoff: operator-specific implementation detail",
                "  - desc: Keep the raw handoff body out of dry-run output by default.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    redacted = codex_memory_result_to_dry_run_json(result)
    included = codex_memory_result_to_dry_run_json(result, include_body=True)

    body = result.records[0].entry.body
    assert redacted["dry_run"] is True
    assert redacted["would_write"] is False
    assert redacted["record_count"] == 1
    assert redacted["records"][0]["memory_entry"]["body"] == f"<redacted:{len(body)} chars>"
    assert included["records"][0]["memory_entry"]["body"] == body


def test_idempotency_is_stable_across_duplicate_runs(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- Stable import: same content should replay with the same key",
                "  - desc: Duplicate sweeps must upsert instead of creating duplicate memories.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    first = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")
    second = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert first.records[0].source_id == second.records[0].source_id
    assert first.records[0].entry.idempotency_key == second.records[0].entry.idempotency_key
    assert first.records[0].entry.idempotency_key.startswith("codex-memory:")


def test_default_scope_and_tags_are_applied(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## General Tips",
                "",
                "- Default routing: use project-manager helpers for central task updates",
                "  - desc: Imported user-scoped guidance should be searchable as shared Codex memory.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    entry = result.records[0].entry
    assert entry.scope.type == "agent"
    assert entry.scope.key == "codex"
    assert entry.tags[:2] == ["codex-memory", "agent-memory"]
    assert "scope-agent" in entry.tags
    assert "agent-codex" in entry.tags
    assert "codex-memory-import" in entry.tags


def test_low_signal_bullets_are_skipped_by_default_with_signal_warning(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- User said thanks and asked to continue.",
                "",
                "- Palace retained ranking fix: retrieve_palace must merge explicit-tag routed candidates",
                "  - desc: Explicit-tag routed retrieval dropped global retained candidates before ranking.",
                "  - learnings: Confident room routes still need global retained candidates.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert [record.entry.title for record in result.records] == ["Palace retained ranking fix"]
    assert all("User said thanks" not in record.entry.body for record in result.records)

    warning = next(warning for warning in result.warnings if warning.code == "low_signal_memory_skipped")
    assert warning.line_number == 5
    assert warning.details["signal_quality"] == "low"
    assert warning.details["skipped_count"] == 1


def test_durable_learning_bullets_are_retained_when_low_signal_neighbors_are_skipped(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace Memory",
                "",
                "- Todo: revisit this later.",
                "",
                "- Durable Palace learning: codex memory import should preserve high-signal operational guidance",
                "  - desc: The import filter must keep bullets with concrete repo, API, task, or regression context.",
                "  - learnings: Memory tightening should retain durable implementation lessons even when adjacent bullets are skipped.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert len(result.records) == 1
    entry = result.records[0].entry
    assert entry.title == "Durable Palace learning"
    assert "high-signal operational guidance" in entry.body
    assert "durable implementation lessons" in entry.body
    assert entry.metadata["codex_memory"]["signal_quality"] == "durable_learning"


def test_idempotency_is_stable_when_bullet_moves_line_numbers(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    original_lines = [
        "# Codex Memory",
        "",
        "## Palace",
        "",
        "- Stable learning: same durable content should keep the same key after file reflow",
        "  - desc: Line number changes should not create duplicate imported memories.",
        "  - learnings: The stable identity should be based on path plus normalized content, not source line.",
        "",
    ]
    moved_lines = [
        "# Codex Memory",
        "",
        "## Palace",
        "",
        "- Low-signal note: continue.",
        "",
        *original_lines[4:],
    ]
    memory_file.write_text("\n".join(original_lines), encoding="utf-8")
    first = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")
    memory_file.write_text("\n".join(moved_lines), encoding="utf-8")
    second = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    first_record = first.records[0]
    second_record = next(record for record in second.records if record.entry.title == first_record.entry.title)

    assert first_record.line_number != second_record.line_number
    assert first_record.source_id == second_record.source_id
    assert first_record.entry.idempotency_key == second_record.entry.idempotency_key


def test_dry_run_json_exposes_signal_quality_stats(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- Low-signal note: sounds good.",
                "",
                "- Durable learning: dry-run output should summarize signal filtering",
                "  - desc: Operators need counts to understand skipped local memory without exposing raw bodies.",
                "  - learnings: Dry-run JSON should include retained and skipped signal-quality totals.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    dry_run = codex_memory_result_to_dry_run_json(result)

    assert dry_run["record_count"] == 1
    assert dry_run["warning_count"] == 1
    assert dry_run["signal_quality"] == {
        "total_bullets": 2,
        "retained_count": 1,
        "skipped_low_signal_count": 1,
        "low_signal_ratio": 0.5,
        "by_quality": {
            "durable_learning": 1,
            "low": 1,
        },
    }


def test_top_level_metadata_bullets_are_skipped(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Audit",
                "",
                "- cwd: /home/example/.codex/worktrees/1234/palaceoftruth",
                "- thread_id: 019df72a-0a22-7212-8722-f19e2ebd6ef6",
                "- updated_at: 2026-05-06T18:55:15Z",
                "- desc: Standalone description without parent context",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert result.records == []
    assert result.low_signal_count == 4
    assert {warning.code for warning in result.warnings} == {"low_signal_memory_skipped"}


def test_no_bullet_markdown_does_not_import_whole_file(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "This is an explanatory paragraph, not a curated memory bullet.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert result.records == []
    assert result.low_signal_count == 0
    assert [warning.code for warning in result.warnings] == ["no_parseable_entries"]


def test_short_but_concrete_identifier_is_retained(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- PR 168: fixed retrieve limit mismatch",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert len(result.records) == 1
    assert result.records[0].entry.title == "PR 168"
    assert result.records[0].entry.body == "fixed retrieve limit mismatch"


def test_legacy_build_path_idempotency_key_fits_database_limit(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Legacy Memory",
                "",
                "## Palace",
                "",
                "Legacy body",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = build_codex_memory_entries(
        tenant_id="tenant-a",
        memory_md_path=memory_file,
        scope=MemoryScope(type="agent", key="codex"),
    )

    assert result.entries
    assert all(len(entry.idempotency_key) <= 64 for entry in result.entries)


def test_standalone_metadata_bullet_is_skipped_even_with_durable_words(tmp_path: Path) -> None:
    memory_file = tmp_path / "MEMORY.md"
    memory_file.write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- desc: future runs should not preserve standalone metadata bullets",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = normalize_codex_memory_files([memory_file], tenant_id="tenant-a")

    assert result.records == []
    assert result.skipped_records[0].reason == "metadata_only"


def test_rollout_summaries_are_excluded_from_roots_unless_requested(tmp_path: Path) -> None:
    memory_root = tmp_path / "memories"
    rollout_dir = memory_root / "rollout_summaries"
    rollout_dir.mkdir(parents=True)
    (memory_root / "MEMORY.md").write_text(
        "\n".join(
            [
                "# Codex Memory",
                "",
                "## Palace",
                "",
                "- Root learning: future runs should dry-run before writing memory imports",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (rollout_dir / "2026-05-06T00-00-00-palace.md").write_text(
        "\n".join(
            [
                "# Rollout",
                "",
                "- Rollout detail: future runs should not import this unless explicitly requested",
                "",
            ]
        ),
        encoding="utf-8",
    )

    default = normalize_codex_memory_files([memory_root], tenant_id="tenant-a")
    included = normalize_codex_memory_files(
        [memory_root],
        tenant_id="tenant-a",
        include_rollout_summaries=True,
    )

    assert [record.source_file for record in default.records] == [str(memory_root / "MEMORY.md")]
    assert sorted(Path(record.source_file).name for record in included.records) == [
        "2026-05-06T00-00-00-palace.md",
        "MEMORY.md",
    ]
