"""Harness regression tests; real-host cases opt in via HERMES_COMPAT_TEST_ROOT."""
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_hermes_host_compatibility.py"


def run_gate(tmp_path, *args):
    output = tmp_path / "report.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), *args],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PALACE_API_KEY": "must-not-inherit", "HERMES_ENABLE_PROJECT_PLUGINS": "1"},
    )
    return result, json.loads(output.read_text()) if output.exists() else {}


def test_missing_host_is_machine_readable_failure(tmp_path):
    result, report = run_gate(tmp_path, "--hermes-root", str(tmp_path / "absent"))
    assert report.get("success") is False, result.stderr
    assert report["gates"]["setup"]["status"] == "failed"
    assert result.returncode == 1


@pytest.fixture
def real_host():
    root = os.environ.get("HERMES_COMPAT_TEST_ROOT")
    if not root:
        pytest.skip("Set HERMES_COMPAT_TEST_ROOT to a real, trusted Hermes checkout")
    return root


def test_real_host_legacy_contract_and_isolation(tmp_path, real_host):
    result, report = run_gate(tmp_path, "--hermes-root", real_host)
    assert result.returncode == 0, (result.stderr, report)
    assert report["success"] is True
    assert report["host_sha"]
    for name in ("isolation", "discovery", "registration", "lifecycle", "write_completion", "tool_dispatch"):
        assert report["gates"][name]["status"] == "passed", report
    assert report["gates"]["checkpoint"]["status"] == "supported_limitation"
    assert report["gates"]["context_helper"]["status"] == "not_requested"
    assert "on_delegation" in report["gates"]["lifecycle"]["observed_hooks"]
    assert report["gates"]["lifecycle"]["shutdown_drain"]["status"] == "drained"
    completion = report["gates"]["write_completion"]
    assert completion["released"]["flush_while_blocked"] is False
    assert completion["released"]["writes"][0]["thread"].startswith("mem-sync")
    assert completion["blocked_shutdown"]["shutdown"]["status"] == "timed_out"
    assert completion["blocked_shutdown"]["write_completed_at_shutdown"] is False


def test_strict_checkpoint_is_an_explicit_future_gate(tmp_path, real_host):
    result, report = run_gate(tmp_path, "--hermes-root", real_host, "--require-gate", "checkpoint")
    assert result.returncode == 1
    assert report["success"] is False
    assert report["gates"]["checkpoint"]["status"] == "supported_limitation"
    assert report["required_gates"] == ["checkpoint"]


def test_strict_global_drain_rejects_pinned_host_limitations(tmp_path, real_host):
    result, report = run_gate(tmp_path, "--hermes-root", real_host, "--require-gate", "global_drain")
    assert result.returncode == 1
    assert report["success"] is False
    assert report["gates"]["write_completion"]["status"] == "passed"
    assert report["gates"]["global_drain"]["status"] == "supported_limitation"
    assert report["gates"]["global_drain"]["limitations"]
    assert report["required_gates"] == ["global_drain"]


@pytest.mark.parametrize("injected,gate", [
    ("import socket\ntry:\n    socket.create_connection(('127.0.0.1', 9))\nexcept Exception:\n    pass\n", "isolation"),
    ("def broken_sync(self, *args, **kwargs):\n    raise TypeError('regression fixture')\nPalaceOfTruthMemoryProvider.sync_turn = broken_sync\n", "lifecycle"),
])
def test_swallowed_regressions_fail_the_gate(tmp_path, real_host, injected, gate):
    package = tmp_path / "fixture-plugin"
    shutil.copytree(ROOT / "third_party_plugins/hermes/memory/palaceoftruth", package)
    init = package / "__init__.py"
    init.write_text(init.read_text() + "\n" + injected)
    result, report = run_gate(tmp_path, "--hermes-root", real_host, "--plugin-root", str(package))
    assert report.get("gates", {}).get(gate, {}).get("status") == "failed", (result.stderr, report)
    assert result.returncode == 1
