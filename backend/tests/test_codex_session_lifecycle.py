from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "codex_session_lifecycle.py"
PLUGIN_README_PATH = REPO_ROOT / "plugins" / "palaceoftruth-memory" / "README.md"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("codex_session_lifecycle_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lifecycle_payloads_use_scoped_dry_run_defaults() -> None:
    module = _load_script_module()

    payload = module.lifecycle_payloads(
        cwd="/home/example/workspace/github_palaceoftruth/palaceoftruth",
        workspace_key=None,
        session_key="run-123",
        agent_scope_key="codex",
        query="What should Codex remember?",
    )

    assert payload["workspace_key"] == "palaceoftruth"
    assert payload["dry_run"] is True
    steps = {step["tool"]: step for step in payload["steps"]}
    assert steps["palace_context"]["arguments"]["memory_scope_key"] == "codex"
    assert steps["retrieve_agent_memory"]["arguments"]["workspace_scope_keys"] == ["palaceoftruth"]
    assert steps["retrieve_agent_memory"]["arguments"]["include_broad_corpus"] is False
    checkpoint_args = steps["capture_checkpoint"]["arguments"]
    assert checkpoint_args["dry_run"] is True
    assert checkpoint_args["scope_type"] == "session"
    assert checkpoint_args["scope_key"] == "run-123"
    writeback_args = steps["create_memory_entry"]["arguments"]
    assert writeback_args["scope_type"] == "workspace"
    assert writeback_args["scope_key"] == "palaceoftruth"
    assert writeback_args["relationship_policy"] == "immediate"


def test_lifecycle_markdown_and_json_avoid_raw_secrets_or_transcripts(capsys) -> None:
    module = _load_script_module()

    assert module.main(
        [
            "--cwd",
            "/tmp/palaceoftruth",
            "--session-key",
            "thread-1",
            "--format",
            "json",
        ]
    ) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["privacy"]["raw_secret_output"] is False
    assert payload["privacy"]["raw_transcript_output"] is False
    assert "PALACEOFTRUTH_API_KEY" not in output
    assert "private transcript" not in output.lower()
    assert "<concise state, decisions, and next steps>" in output


def test_plugin_readme_references_core_mcp_tools_and_fallback() -> None:
    text = PLUGIN_README_PATH.read_text(encoding="utf-8")

    assert "codex_session_lifecycle.py" in text
    assert "whoami" in text
    assert "palace_context" in text
    assert "retrieve_agent_memory" in text
    assert "capture_checkpoint" in text
    assert "create_memory_entry" in text
    assert "normalize_agent_transcripts.py dry-run" in text
    assert "Use local Codex memory files only when Palace MCP is unavailable" in text
    assert "semantic retrieval is\ndegraded" in text
    assert "`list_memory_entries` for scoped agent" in text
    assert "`list_items` for ingested library" in text
