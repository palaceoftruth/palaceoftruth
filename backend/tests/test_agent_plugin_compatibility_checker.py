from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_agent_plugin_compatibility.py"
SPEC = importlib.util.spec_from_file_location("check_agent_plugin_compatibility", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
checker = importlib.util.module_from_spec(SPEC)
sys.modules["check_agent_plugin_compatibility"] = checker
SPEC.loader.exec_module(checker)


REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_skill(root: Path, name: str, frontmatter: dict[str, str] | None = None) -> None:
    metadata = {"name": name, "description": f"{name} skill"}
    if frontmatter:
        metadata.update(frontmatter)
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in metadata.items())
    lines.extend(["---", "", f"# {name}", ""])
    path = root / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _plugin_fixture(root: Path) -> Path:
    plugin_root = root / "palaceoftruth-memory"
    _write_json(
        plugin_root / ".codex-plugin" / "plugin.json",
        {
            "name": "palaceoftruth-memory",
            "version": "1.2.3",
            "description": "Scoped Palace memory",
            "repository": "https://github.com/palaceoftruth/palaceoftruth",
            "skills": "./skills/",
            "mcpServers": "./.mcp.json",
        },
    )
    _write_json(
        plugin_root / ".mcp.json",
        {
            "mcpServers": {
                "palaceoftruth-memory": {
                    "command": "uv",
                    "env": {"PALACEOFTRUTH_API_BASE_URL": "https://api.palaceoftruth.test"},
                }
            }
        },
    )
    _write_json(
        plugin_root / ".claude-plugin" / "plugin.json",
        {
            "name": "palaceoftruth-memory",
            "version": "1.2.3",
            "description": "Scoped Palace memory",
            "skills": ["./skills/palaceoftruth-memory"],
        },
    )
    _write_json(
        plugin_root / ".claude-plugin" / "marketplace.json",
        {"plugins": [{"name": "palaceoftruth-memory", "source": "."}]},
    )
    _write_skill(plugin_root, "palaceoftruth-memory", {"repository": "https://example.test/repo"})
    return plugin_root


def test_real_package_reports_codex_clawhub_first_and_openclaw_deferred() -> None:
    report = checker.build_compatibility_report(PLUGIN_ROOT)

    assert report["report"] == "palace-agent-plugin-compatibility"
    assert report["dry_run"] is True
    assert report["mutating"] is False
    assert report["status"] == "ok"
    assert report["compatibility_target"]["primary"] == ["codex-plugin", "clawhub-skill"]
    assert report["compatibility_target"]["deferred"] == ["openclaw-plugin-native-runtime"]
    assert "native OpenClaw plugin support is deferred" in report["compatibility_target"]["rationale"]

    targets = {target["target"]: target for target in report["targets"]}
    assert targets["codex-plugin"]["supported"] is True
    assert targets["codex-plugin"]["source_metadata"]["version"] == "0.2.0"
    assert targets["codex-plugin"]["relative_paths"] == {
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
    }
    assert "PALACEOFTRUTH_API_BASE_URL" in targets["codex-plugin"]["env_declarations"]
    assert "PALACEOFTRUTH_API_KEY must be supplied" in targets["codex-plugin"]["warnings"][0]
    assert targets["clawhub-skill"]["supported"] is True
    assert {skill["name"] for skill in targets["clawhub-skill"]["skills"]} == {
        "palaceoftruth-memory",
        "palaceoftruth-codex-memory",
    }
    assert targets["openclaw-plugin"]["status"] == "not-present"


def test_fixture_reports_codex_claude_and_clawhub_compatibility(tmp_path: Path) -> None:
    plugin_root = _plugin_fixture(tmp_path)

    report = checker.build_compatibility_report(plugin_root)

    assert report["status"] == "ok"
    targets = {target["target"]: target for target in report["targets"]}
    assert targets["codex-plugin"]["bin_declarations"] == ["uv"]
    assert targets["claude-style-plugin"]["relative_paths"] == {
        "skills": ["./skills/palaceoftruth-memory"]
    }
    assert targets["clawhub-skill"]["skills"][0]["source_metadata"] == {
        "repository": "https://example.test/repo"
    }


def test_checker_preserves_source_metadata_and_redacts_secret_like_fields(tmp_path: Path) -> None:
    plugin_root = _plugin_fixture(tmp_path)
    codex_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    payload = json.loads(codex_manifest.read_text(encoding="utf-8"))
    payload["source"] = {"url": "https://example.test/plugin.tgz", "api_key": "secret-value"}
    payload["digests"] = {"manifest": "sha256:abc"}
    codex_manifest.write_text(json.dumps(payload), encoding="utf-8")

    report = checker.build_compatibility_report(plugin_root)

    encoded = json.dumps(report)
    assert "secret-value" not in encoded
    source = {target["target"]: target for target in report["targets"]}["codex-plugin"][
        "source_metadata"
    ]
    assert source["source"]["url"] == "https://example.test/plugin.tgz"
    assert source["source"]["api_key"] == "<redacted>"
    assert source["digests"] == {"manifest": "sha256:abc"}


def test_checker_reports_invalid_mixed_manifest_without_dropping_fields(tmp_path: Path) -> None:
    plugin_root = _plugin_fixture(tmp_path)
    codex_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    payload = json.loads(codex_manifest.read_text(encoding="utf-8"))
    payload["version"] = "latest"
    payload["skills"] = "/absolute/skills"
    payload["clawhubOnly"] = {"exports": True}
    codex_manifest.write_text(json.dumps(payload), encoding="utf-8")
    skill_path = plugin_root / "skills" / "palaceoftruth-memory" / "SKILL.md"
    skill_path.write_text(
        "---\nname: palaceoftruth-memory\ndescription: skill\nopenclawCapability: runtime\n---\n# Skill\n",
        encoding="utf-8",
    )

    report = checker.build_compatibility_report(plugin_root)

    assert report["status"] == "error"
    targets = {target["target"]: target for target in report["targets"]}
    assert "Codex manifest version must be strict semver" in targets["codex-plugin"]["errors"]
    assert "Codex field skills must be a relative path" in targets["codex-plugin"]["errors"]
    assert targets["codex-plugin"]["unsupported_fields"] == ["clawhubOnly"]
    assert targets["clawhub-skill"]["skills"][0]["unsupported_fields"] == ["openclawCapability"]


def test_checker_reports_future_openclaw_fields_as_metadata_only(tmp_path: Path) -> None:
    plugin_root = _plugin_fixture(tmp_path)
    _write_json(
        plugin_root / "openclaw.plugin.json",
        {
            "name": "palaceoftruth-memory",
            "version": "1.2.3",
            "capabilities": ["memory.read"],
            "nativeRuntimeRegistration": {"entrypoint": "plugin.py"},
        },
    )

    report = checker.build_compatibility_report(plugin_root)

    targets = {target["target"]: target for target in report["targets"]}
    openclaw = targets["openclaw-plugin"]
    assert openclaw["present"] is True
    assert openclaw["status"] == "experimental-metadata-only"
    assert openclaw["unsupported_fields"] == ["nativeRuntimeRegistration"]
    assert "no native runtime capability registration is performed" in openclaw["warnings"][0]


def test_cli_returns_nonzero_for_invalid_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_root = _plugin_fixture(tmp_path)
    codex_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    payload = json.loads(codex_manifest.read_text(encoding="utf-8"))
    payload["version"] = "1"
    codex_manifest.write_text(json.dumps(payload), encoding="utf-8")

    status = checker.main(["--plugin-root", str(plugin_root)])

    captured = capsys.readouterr()
    assert status == 1
    report = json.loads(captured.out)
    assert report["status"] == "error"


def test_cli_text_output_summarizes_supported_targets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_root = _plugin_fixture(tmp_path)

    status = checker.main(["--plugin-root", str(plugin_root), "--format", "text"])

    captured = capsys.readouterr()
    assert status == 0
    assert "Palace agent plugin compatibility: ok" in captured.out
    assert "codex-plugin: present, supported" in captured.out
