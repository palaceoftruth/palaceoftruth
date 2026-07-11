import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "setup_codex_palace_memory.py"
SPEC = importlib.util.spec_from_file_location("setup_codex_palace_memory", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
setup_script = importlib.util.module_from_spec(SPEC)
sys.modules["setup_codex_palace_memory"] = setup_script
SPEC.loader.exec_module(setup_script)

SMOKE_SPEC = importlib.util.spec_from_file_location("smoke_agent_memory_compatibility", setup_script.SMOKE_SCRIPT)
assert SMOKE_SPEC is not None
assert SMOKE_SPEC.loader is not None
smoke_script = importlib.util.module_from_spec(SMOKE_SPEC)
sys.modules["smoke_agent_memory_compatibility"] = smoke_script
SMOKE_SPEC.loader.exec_module(smoke_script)


def parse_args(values: list[str]) -> Any:
    return setup_script.build_parser().parse_args(values)


def clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for names in setup_script.AUTH_ENV_ALIASES.values():
        for name in names:
            monkeypatch.delenv(name, raising=False)
    for name in ("PALACEOFTRUTH_API_KEY", "SECONDBRAIN_API_KEY", "API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_setup_default_is_redacted_non_mutating_dry_run() -> None:
    args = parse_args(["--api-base-url", "https://api.palaceoftruth.test"])

    report = setup_script.build_report(args)

    assert report["dry_run"] is True
    assert report["mutating"] is False
    assert report["scope"] == {"type": "agent", "key": "codex"}
    assert "palaceoftruth-codex-memory" in report["skillpack"]
    assert setup_script.OAUTH_CLIENT_SECRET_ENV in report["codex_config_toml"]
    assert "tenant-api-key" not in report["codex_config_toml"]
    assert report["redacted_env"][setup_script.OAUTH_CLIENT_SECRET_ENV].startswith("<redacted:")
    assert report["auth"]["oauth_preferred"] is True
    assert report["live_smoke_contract"] == {
        "writes_scoped_memories": 1,
        "relationship_policy": "immediate",
        "backfill": "disabled",
        "delete_retry_admin_operations": False,
        "raw_secret_output": False,
    }


def test_plugin_check_reports_missing_install_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_auth_env(monkeypatch)
    args = parse_args(
        [
            "--check",
            "--codex-home",
            str(tmp_path / "codex-home"),
            "--format",
            "json",
        ]
    )

    report = setup_script.build_plugin_check_report(args)

    assert report["report"] == "palace-plugin-install-check"
    assert report["dry_run"] is True
    assert report["mutating"] is False
    assert report["codex"]["installed"] is False
    assert report["codex"]["status"] == "missing"
    assert report["codex"]["update_state"] == "install-available"
    assert report["codex"]["auth"]["mode"] == "missing"
    assert report["codex"]["auth"]["configured"] == {
        "bearer_token": False,
        "oauth_client_credentials": False,
        "legacy_api_key": False,
    }
    assert report["codex"]["mcp_command_drift"] == ["installed Codex plugin is missing"]
    assert report["codex"]["marketplace"]["registered"] is True
    assert report["codex"]["marketplace"]["path"] == (
        "./third_party_plugins/agent_clients/palaceoftruth-memory"
    )
    assert report["hermes"]["package_surface"] == "hermes"
    assert report["hermes"]["status"] == "separate-package-surface"
    assert "Install the repo Codex plugin package" in report["codex"]["next_action"]


def test_plugin_check_detects_version_mcp_and_skillpack_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = tmp_path / "installed-palace-plugin"
    shutil.copytree(setup_script.SKILLPACK_ROOT, installed)
    manifest_path = installed / ".codex-plugin" / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.0.1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    mcp_path = installed / ".mcp.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    mcp["mcpServers"]["palaceoftruth-memory"]["command"] = "python"
    mcp["mcpServers"]["palaceoftruth-memory"]["env"]["PALACEOFTRUTH_API_KEY"] = "secret"
    mcp_path.write_text(json.dumps(mcp), encoding="utf-8")
    (installed / "skills" / "palaceoftruth-codex-memory" / "SKILL.md").write_text(
        "changed skill\n", encoding="utf-8"
    )
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret-value")
    args = parse_args(
        [
            "--check",
            "--installed-plugin-path",
            str(installed),
            "--codex-home",
            str(tmp_path / "codex-home"),
        ]
    )

    report = setup_script.build_plugin_check_report(args)

    assert report["codex"]["installed"] is True
    assert report["codex"]["status"] == "drifted"
    assert report["codex"]["update_state"] == "update-available"
    assert report["codex"]["installed_version"] == "0.0.1"
    assert report["codex"]["restart_required"] is True
    assert "MCP command differs from repo manifest" in report["codex"]["mcp_command_drift"]
    assert any("PALACEOFTRUTH_API_KEY" in item for item in report["codex"]["mcp_command_drift"])
    assert "palaceoftruth-codex-memory/SKILL.md" in report["codex"]["skillpack_drift"]["changed"]
    assert "secret-value" not in json.dumps(report)
    assert "secret\"" not in json.dumps(report)


def test_plugin_check_discovers_cached_codex_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_auth_env(monkeypatch)
    installed = (
        tmp_path
        / "codex-home"
        / "plugins"
        / "cache"
        / "palaceoftruth"
        / "palaceoftruth-memory"
    )
    shutil.copytree(setup_script.SKILLPACK_ROOT, installed)
    args = parse_args(["--check", "--codex-home", str(tmp_path / "codex-home")])

    report = setup_script.build_plugin_check_report(args)

    assert report["codex"]["installed"] is True
    assert report["codex"]["source"] == "codex-cache"
    assert report["codex"]["status"] == "auth-missing"
    assert report["codex"]["update_state"] == "current"
    assert report["codex"]["mcp_command_drift"] == []
    assert report["codex"]["skillpack_drift"]["drifted"] is False


def test_setup_live_smoke_command_uses_stdio_adapter_without_backfill() -> None:
    args = parse_args(
        [
            "--api-base-url",
            "https://api.palaceoftruth.test",
            "--run-id",
            "setup-live",
            "--scope-type",
            "agent",
            "--scope-key",
            "codex",
        ]
    )

    command = setup_script.smoke_command(args, include_secret=False)

    assert str(setup_script.SMOKE_SCRIPT) in command
    assert "mcp-stdio" in command
    assert command[command.index("--relationship-policy") + 1] == "immediate"
    assert "--skip-backfill" in command
    assert "backfill_deferred_relationships" not in " ".join(command)
    assert "--stdio-arg=scripts/palaceoftruth_mcp.py" in command
    assert "--stdio-arg=--directory" in command
    assert "--stdio-arg" not in command
    assert "--api-key" not in command
    assert not any(item.startswith("<redacted:") for item in command)

    smoke_args = command[command.index(str(setup_script.SMOKE_SCRIPT)) + 1 :]
    parsed = smoke_script.build_parser().parse_args(smoke_args)
    assert parsed.stdio_arg == [
        "--directory",
        str(setup_script.BACKEND_ROOT),
        "run",
        "python",
        "scripts/palaceoftruth_mcp.py",
    ]


def test_setup_live_smoke_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_auth_env(monkeypatch)
    args = parse_args(["--live-smoke"])

    with pytest.raises(setup_script.SetupError, match="OAUTH_CLIENT_SECRET"):
        setup_script.run_live_smoke(args)


def test_setup_oauth_configuration_is_secret_safe_and_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_auth_env(monkeypatch)
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "legacy-secret")
    monkeypatch.setenv(setup_script.OAUTH_CLIENT_SECRET_ENV, "oauth-secret")
    monkeypatch.setenv(setup_script.OAUTH_CLIENT_KEY_ENV, "codex-remote")
    args = parse_args(["--api-base-url", "https://api.palaceoftruth.test"])

    auth = setup_script.auth_configuration(args)

    assert auth["mode"] == "oauth_client_credentials"
    assert auth["oauth_preferred"] is True
    assert auth["legacy_fallback_retained"] is True
    assert auth["client_key"] == "codex-remote"
    assert "oauth-secret" not in json.dumps(auth)
    assert "legacy-secret" not in json.dumps(auth)


def test_setup_oauth_configuration_accepts_secondbrain_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_auth_env(monkeypatch)
    monkeypatch.setenv("SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET", "oauth-secret")
    monkeypatch.setenv("SECONDBRAIN_MCP_CLIENT_KEY", "codex-compat")
    args = parse_args([])

    auth = setup_script.auth_configuration(args)

    assert auth["mode"] == "oauth_client_credentials"
    assert auth["client_key"] == "codex-compat"


def test_setup_live_smoke_runs_exact_previewed_command(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: dict[str, Any] = {}

    def fake_run(command: list[str], check: bool, env: dict[str, str]) -> Any:
        launched["command"] = command
        launched["check"] = check
        launched["env"] = env
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret-value")
    monkeypatch.setattr(setup_script.subprocess, "run", fake_run)
    args = parse_args(["--live-smoke", "--run-id", "setup-live"])

    status = setup_script.run_live_smoke(args)

    assert status == 0
    assert launched["check"] is False
    command = launched["command"]
    assert "--api-key" not in command
    assert launched["env"]["PALACEOFTRUTH_API_KEY"] == "secret-value"
    assert command[command.index("--relationship-policy") + 1] == "immediate"
    assert "--skip-backfill" in command
    assert "--stdio-arg=--directory" in command


def test_setup_rejects_invalid_config_shape() -> None:
    with pytest.raises(setup_script.SetupError, match="http"):
        setup_script.build_report(parse_args(["--api-base-url", "palace.local"]))

    with pytest.raises(setup_script.SetupError, match="must be omitted"):
        setup_script.build_report(
            parse_args(["--scope-type", "tenant_shared", "--scope-key", "codex"])
        )

    with pytest.raises(setup_script.SetupError, match="is required"):
        setup_script.build_report(parse_args(["--scope-type", "agent", "--scope-key", ""]))


def test_plugin_check_text_is_concise_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PALACEOFTRUTH_API_KEY", "secret-value")
    args = parse_args(["--check", "--codex-home", str(tmp_path)])

    report = setup_script.build_plugin_check_report(args)
    text = setup_script.format_text(report)

    assert "Palace plugin install check" in text
    assert "Mode: read-only dry-run" in text
    assert "Auth mode: legacy_api_key" in text
    assert "secret-value" not in text


def test_plugin_check_cannot_run_live_smoke(capsys: pytest.CaptureFixture[str]) -> None:
    status = setup_script.main(["--check", "--live-smoke"])

    captured = capsys.readouterr()
    assert status == 2
    assert "--check cannot be combined with --live-smoke" in captured.err
