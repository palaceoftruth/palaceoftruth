from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "detect_hermes_plugin_release.py"
SPEC = importlib.util.spec_from_file_location("detect_hermes_plugin_release", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
detect_module = importlib.util.module_from_spec(SPEC)
sys.modules["detect_hermes_plugin_release"] = detect_module
SPEC.loader.exec_module(detect_module)


def test_readme_only_plugin_change_does_not_trigger_release() -> None:
    assert not detect_module.should_release_for_changed_paths(
        ["third_party_plugins/hermes/memory/palaceoftruth/README.md"]
    )


def test_plugin_contract_change_triggers_release() -> None:
    assert detect_module.should_release_for_changed_paths(
        ["third_party_plugins/hermes/memory/palaceoftruth/plugin.yaml"]
    )


def test_plugin_runtime_change_triggers_release() -> None:
    assert detect_module.should_release_for_changed_paths(
        ["third_party_plugins/hermes/memory/palaceoftruth/__init__.py"]
    )


def test_plugin_dockerfile_change_triggers_release() -> None:
    assert detect_module.should_release_for_changed_paths(
        ["third_party_plugins/hermes/memory/palaceoftruth/Dockerfile"]
    )


def test_unrelated_changes_do_not_trigger_release() -> None:
    assert not detect_module.should_release_for_changed_paths(
        [
            ".github/workflows/build-push.yml",
            "scripts/smoke_agent_memory_compatibility.py",
            "backend/tests/test_agent_memory_compatibility_smoke.py",
        ]
    )
