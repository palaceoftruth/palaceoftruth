from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict)
    return payload


def test_codex_plugin_manifest_points_to_palace_mcp_package() -> None:
    manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")

    assert manifest["name"] == "palaceoftruth-memory"
    assert manifest["version"] == "0.2.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "[TODO:" not in json.dumps(manifest)

    interface = manifest["interface"]
    assert interface["displayName"] == "Palace Memory"
    assert "Write" in interface["capabilities"]
    assert "delete" in interface["longDescription"]
    assert "admin" in interface["longDescription"]
    assert "retrieval-ranking" in interface["longDescription"]


def test_codex_client_version_is_separate_from_hermes_package_version() -> None:
    codex_manifest = load_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    codex_readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    hermes_readme = (
        REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth" / "README.md"
    ).read_text(encoding="utf-8")
    hermes_plugin_yaml = (
        REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth" / "plugin.yaml"
    ).read_text(encoding="utf-8")

    assert codex_manifest["version"] == "0.2.0"
    assert "version: 1.0.25" in hermes_plugin_yaml
    assert "The Hermes package version comes from this directory's `plugin.yaml`" in hermes_readme
    assert "has its own manifest\n  version" in hermes_readme
    assert "tracks the Codex/Claude\nclient install surface only" in codex_readme
    assert "does not participate in Hermes runtime release\nselection" in codex_readme


def test_codex_mcp_config_launches_primary_stdio_adapter() -> None:
    config = load_json(PLUGIN_ROOT / ".mcp.json")
    server = config["mcpServers"]["palaceoftruth-memory"]

    assert server["command"] == "uv"
    assert server["cwd"] == "."
    assert server["args"] == [
        "--directory",
        "../../../backend",
        "run",
        "python",
        "scripts/palaceoftruth_mcp.py",
    ]
    assert server["env"] == {"PALACEOFTRUTH_API_BASE_URL": "https://api.palaceoftruth.test"}
    assert "PALACEOFTRUTH_API_KEY" not in server["env"]
    assert "secondbrain_mcp.py" not in json.dumps(server)


def test_repo_marketplace_registers_palace_memory_plugin() -> None:
    marketplace = load_json(REPO_ROOT / ".agents" / "plugins" / "marketplace.json")

    assert marketplace["name"] == "palaceoftruth"
    assert marketplace["interface"]["displayName"] == "Palace of Truth"
    assert marketplace["plugins"] == [
        {
            "name": "palaceoftruth-memory",
            "source": {
                "source": "local",
                "path": "./third_party_plugins/agent_clients/palaceoftruth-memory",
            },
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
            },
            "category": "Productivity",
        }
    ]


def test_claude_plugin_manifest_uses_supported_simple_shape() -> None:
    manifest = load_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    marketplace = load_json(PLUGIN_ROOT / ".claude-plugin" / "marketplace.json")

    assert manifest["name"] == "palaceoftruth-memory"
    assert manifest["skills"] == [
        "./skills/palaceoftruth-memory",
        "./skills/palaceoftruth-codex-memory",
    ]
    assert "[TODO:" not in json.dumps(manifest)

    assert marketplace["plugins"] == [
        {
            "name": "palaceoftruth-memory",
            "source": ".",
        }
    ]


def test_package_docs_preserve_non_destructive_boundary() -> None:
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    skill = (
        PLUGIN_ROOT / "skills" / "palaceoftruth-memory" / "SKILL.md"
    ).read_text(encoding="utf-8")
    codex_skill = (
        PLUGIN_ROOT / "skills" / "palaceoftruth-codex-memory" / "SKILL.md"
    ).read_text(encoding="utf-8")

    for text in (readme, skill):
        assert "backend/scripts/palaceoftruth_mcp.py" in text
        assert "PALACEOFTRUTH_API_KEY" in text
        assert "setup_codex_palace_memory.py" in text
        assert "codex_session_lifecycle.py" in text
        assert "mcp-stdio" in text
        assert "delete/restore" in text
        assert "failed-job retry" in text
        assert "never writes to stdout" in text
        assert "normalize_agent_transcripts.py" in text
        assert "does not write memory" in text
        assert "setup_codex_palace_memory.py --check" in text

    assert "scripts/palace_plugin_manager.py plan --format json" in readme
    assert "lockfile-backed updater planning" in readme
    assert "previous version pointer" in readme
    assert "does not edit local profiles" in readme

    assert "plugins/palaceoftruth-memory" not in readme

    assert "retrieve_agent_memory" in codex_skill
    assert 'agent_scope_key="codex"' in codex_skill
    assert "setup_codex_palace_memory.py" in codex_skill
    assert "codex-session-lifecycle.md" in codex_skill
    assert "codex_session_lifecycle.py" in codex_skill
    assert "agent/codex" in codex_skill
    assert "create_memory_entry" in codex_skill
    assert "capture_checkpoint" in codex_skill
    assert "PALACEOFTRUTH_MCP_CHECKPOINT_CAPTURE_DISABLED" in codex_skill
    assert "Never store raw secrets" in codex_skill
    assert "Fall back to local Codex memory files" in codex_skill
    assert "semantic-retrieval outage" in codex_skill
    assert "list_memory_scopes" in codex_skill
    assert "Use `list_items` only when the target is an ingested library item" in codex_skill
    assert "Do not dump\n   broad raw memory bodies" in codex_skill
