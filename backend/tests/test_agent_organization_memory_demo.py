from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "demo_agent_organization_memory.py"
DOC_PATH = REPO_ROOT / "docs" / "agent-organization-memory-demo.md"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("agent_organization_memory_demo_under_test", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_payloads_keep_specialist_and_orchestrator_scopes_separate() -> None:
    module = _load_script_module()

    payload = module.demo_payloads(
        workspace_key="palaceoftruth",
        orchestrator_key="orchestrator",
        specialist_keys=["security-agent", "macos-agent", "frontend-agent"],
        query="release memory",
        access_reason="demo release briefing",
    )

    assert payload["dry_run"] is True
    assert payload["privacy_contract"]["cross_agent_reads_are_server_authorized"] is True
    assert payload["privacy_contract"]["include_broad_corpus"] is False
    steps = payload["steps"]
    specialist_writes = [step for step in steps if step["phase"] == "specialist_write"]
    assert [step["arguments"]["scope_key"] for step in specialist_writes] == [
        "security-agent",
        "macos-agent",
        "frontend-agent",
    ]
    assert {step["arguments"]["scope_type"] for step in specialist_writes} == {"agent"}
    assert all(step["tool"] == "palace_remember" for step in specialist_writes)
    assert all(step["arguments"]["idempotency_key"] for step in specialist_writes)
    recall = next(step for step in steps if step["phase"] == "orchestrator_recall")
    assert recall["arguments"]["agent_scope_key"] == "orchestrator"
    assert recall["arguments"]["workspace_scope_keys"] == ["palaceoftruth"]
    assert recall["arguments"]["include_agent_scope_keys"] == [
        "security-agent",
        "macos-agent",
        "frontend-agent",
    ]
    assert recall["arguments"]["include_broad_corpus"] is False
    assert recall["arguments"]["access_reason"] == "demo release briefing"
    writeback = next(step for step in steps if step["phase"] == "orchestrator_writeback")
    assert writeback["arguments"]["scope_type"] == "agent"
    assert writeback["arguments"]["scope_key"] == "orchestrator"
    assert writeback["tool"] == "palace_remember"
    assert writeback["arguments"]["idempotency_key"]


def test_demo_rejects_ambiguous_scope_keys() -> None:
    module = _load_script_module()

    with pytest.raises(ValueError, match="must not also be a specialist"):
        module.demo_payloads(
            workspace_key="palaceoftruth",
            orchestrator_key="orchestrator",
            specialist_keys=["orchestrator"],
            query="release memory",
            access_reason="demo release briefing",
        )

    with pytest.raises(ValueError, match="must not include"):
        module.demo_payloads(
            workspace_key="palaceoftruth",
            orchestrator_key="orchestrator",
            specialist_keys=["agent/security"],
            query="release memory",
            access_reason="demo release briefing",
        )


def test_demo_cli_outputs_json_without_secrets_or_live_write_claims(capsys) -> None:
    module = _load_script_module()

    assert module.main(["--format", "json", "--specialist", "security-agent"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["dry_run"] is True
    assert payload["specialist_keys"] == ["security-agent"]
    assert "PALACEOFTRUTH_API_KEY" not in output
    assert "client_secret" not in output
    assert "raw private" in output


def test_demo_docs_include_positioning_and_smoke_script() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "Agent Organization Memory" in text
    assert "Generic vector DB" in text
    assert "security-agent" in text
    assert "macos-agent" in text
    assert "frontend-agent" in text
    assert "demo_agent_organization_memory.py" in text
    assert "server authorizes" in text
    assert "writes the reviewed synthesis only to `agent/orchestrator`" in text
