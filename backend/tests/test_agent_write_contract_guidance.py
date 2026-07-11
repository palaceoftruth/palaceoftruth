from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_agent_guidance_is_alias_first_and_fail_closed() -> None:
    """Keep normal conversational guidance on the safe Palace write contract."""

    guidance_paths = (
        "third_party_plugins/agent_clients/palaceoftruth-memory/skills/palaceoftruth-codex-memory/SKILL.md",
        "third_party_plugins/agent_clients/palaceoftruth-memory/skills/palaceoftruth-memory/SKILL.md",
        "third_party_plugins/agent_clients/palaceoftruth-memory/README.md",
        "third_party_plugins/hermes/memory/palaceoftruth/README.md",
    )
    guidance = "\n".join(_read(path) for path in guidance_paths)

    required_contract = (
        "palace_remember",
        "palace_checkpoint",
        "create_memory_entry",
        "idempotency",
        "tenant_shared",
        "deferred",
        "does not bypass OAuth",
        "accepted or queued",
        "proposed capture payload",
    )
    for phrase in required_contract:
        assert phrase in guidance

    prohibited_normal_write_guidance = (
        "Use `create_memory_entry` for durable learning",
        "`create_memory_entry` or `palace_remember`",
        "fall back to raw REST",
        "raw REST fallback",
        "MCP bypasses OAuth",
        "accepted or queued is durable",
    )
    for phrase in prohibited_normal_write_guidance:
        assert phrase not in guidance


def test_generated_agent_examples_use_explicit_alias_writes() -> None:
    lifecycle = _read("scripts/codex_session_lifecycle.py")
    demo = _read("scripts/demo_agent_organization_memory.py")
    demo_docs = _read("docs/agent-organization-memory-demo.md")

    for text in (lifecycle, demo, demo_docs):
        assert '"tool": "palace_remember"' in text
        assert "idempotency_key" in text
        assert "scope_type" in text
        assert "scope_key" in text

    assert '"tool": "create_memory_entry"' not in lifecycle
    assert '"tool": "create_memory_entry"' not in demo
    assert '"tool": "create_memory_entry"' not in demo_docs
    assert '"tool": "palace_checkpoint"' in lifecycle


def test_plugin_package_contains_the_canonical_agent_skills() -> None:
    """Avoid validating a stale installed/cache copy as a source of truth."""

    assert (PLUGIN_ROOT / "skills" / "palaceoftruth-codex-memory" / "SKILL.md").is_file()
    assert (PLUGIN_ROOT / "skills" / "palaceoftruth-memory" / "SKILL.md").is_file()
