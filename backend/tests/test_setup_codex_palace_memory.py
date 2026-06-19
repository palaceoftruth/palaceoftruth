import importlib.util
import os
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


def parse_args(values: list[str]) -> Any:
    return setup_script.build_parser().parse_args(values)


def test_setup_default_is_redacted_non_mutating_dry_run() -> None:
    args = parse_args(["--api-base-url", "https://api.palaceoftruth.test"])

    report = setup_script.build_report(args)

    assert report["dry_run"] is True
    assert report["mutating"] is False
    assert report["scope"] == {"type": "agent", "key": "codex"}
    assert "palaceoftruth-codex-memory" in report["skillpack"]
    assert "PALACEOFTRUTH_API_KEY" in report["codex_config_toml"]
    assert "tenant-api-key" not in report["codex_config_toml"]
    assert report["redacted_env"]["PALACEOFTRUTH_API_KEY"].startswith("<redacted:")
    assert report["live_smoke_contract"] == {
        "writes_scoped_memories": 1,
        "relationship_policy": "immediate",
        "backfill": "disabled",
        "delete_retry_admin_operations": False,
        "raw_secret_output": False,
    }


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
    assert "scripts/palaceoftruth_mcp.py" in command
    assert "--api-key" not in command
    assert not any(item.startswith("<redacted:") for item in command)


def test_setup_live_smoke_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALACEOFTRUTH_API_KEY", raising=False)
    args = parse_args(["--live-smoke"])

    with pytest.raises(setup_script.SetupError, match="PALACEOFTRUTH_API_KEY is required"):
        setup_script.run_live_smoke(args)


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


def test_setup_rejects_invalid_config_shape() -> None:
    with pytest.raises(setup_script.SetupError, match="http"):
        setup_script.build_report(parse_args(["--api-base-url", "palace.local"]))

    with pytest.raises(setup_script.SetupError, match="must be omitted"):
        setup_script.build_report(
            parse_args(["--scope-type", "tenant_shared", "--scope-key", "codex"])
        )

    with pytest.raises(setup_script.SetupError, match="is required"):
        setup_script.build_report(parse_args(["--scope-type", "agent", "--scope-key", ""]))
