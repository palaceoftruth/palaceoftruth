from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "palace_plugin_manager.py"
SPEC = importlib.util.spec_from_file_location("palace_plugin_manager", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
manager = importlib.util.module_from_spec(SPEC)
sys.modules["palace_plugin_manager"] = manager
SPEC.loader.exec_module(manager)


def parse_args(values: list[str]) -> Any:
    return manager.build_parser().parse_args(values)


def _manifest(root: Path, version: str = "1.2.3", extra: dict[str, Any] | None = None) -> Path:
    manifest_dir = root / ".codex-plugin"
    manifest_dir.mkdir(parents=True)
    payload: dict[str, Any] = {
        "name": "palaceoftruth-memory",
        "version": version,
        "repository": "https://github.com/palaceoftruth/palaceoftruth",
    }
    if extra:
        payload.update(extra)
    manifest_path = manifest_dir / "plugin.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"palaceoftruth-memory": {"command": "uv"}}}),
        encoding="utf-8",
    )
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text("skill\n", encoding="utf-8")
    return manifest_path


def _desired(root: Path, version: str = "1.2.3") -> dict[str, Any]:
    manifest_path = _manifest(root, version)
    return manager.build_desired_manifest(
        package_surface="codex",
        plugin_root=root,
        manifest_path=manifest_path,
        artifact_url="https://example.test/palaceoftruth-memory.tgz",
        source="repo-package",
    )


def _lockfile(desired: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    entry = manager.build_lockfile_entry(
        desired=desired,
        installed_path=Path("/tmp/palaceoftruth-memory"),
    )
    entry.update(overrides)
    return entry


def _plan(
    desired: dict[str, Any],
    installed: dict[str, Any] | None,
    *,
    lockfile_error: str | None = None,
) -> dict[str, Any]:
    return manager.build_update_plan(
        desired=desired,
        installed_lockfile=installed,
        lockfile_error=lockfile_error,
        installed_path=Path("/tmp/palaceoftruth-memory"),
    )


def test_lockfile_schema_records_update_planning_fields(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin")

    lockfile = manager.build_lockfile_entry(
        desired=desired,
        installed_path=Path("/tmp/palaceoftruth-memory"),
        previous_version="1.2.2",
        enabled=False,
        pinned=True,
        skipped=True,
        restart_required=True,
    )

    assert lockfile["schema_version"] == manager.LOCKFILE_SCHEMA_VERSION
    assert lockfile["plugin_id"] == "palaceoftruth-memory"
    assert lockfile["package_surface"] == "codex"
    assert lockfile["source"] == "repo-package"
    assert lockfile["marketplace"] == "https://github.com/palaceoftruth/palaceoftruth"
    assert lockfile["artifact_url"] == "https://example.test/palaceoftruth-memory.tgz"
    assert lockfile["resolved_version"] == "1.2.3"
    assert lockfile["manifest_digest"].startswith("sha256:")
    assert lockfile["artifact_digest"].startswith("sha256:")
    assert lockfile["installed_path"] == "/tmp/palaceoftruth-memory"
    assert lockfile["installed_at"].endswith("Z")
    assert lockfile["previous_version"] == "1.2.2"
    assert lockfile["enabled"] is False
    assert lockfile["pinned"] is True
    assert lockfile["skipped"] is True
    assert lockfile["restart_required"] is True


def test_planner_reports_fresh_install(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin")

    report = _plan(desired, None)

    assert report["dry_run"] is True
    assert report["mutating"] is False
    assert report["plan"]["update_state"] == "install"
    assert report["plan"]["operation"] == "install"
    assert report["plan"]["restart_required"] is True
    assert report["plan"]["receipt"]["from_version"] is None
    assert report["plan"]["receipt"]["to_version"] == "1.2.3"


def test_planner_reports_up_to_date_no_op(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin")
    installed = _lockfile(desired)

    report = _plan(desired, installed)

    assert report["plan"]["update_state"] == "no-op"
    assert report["plan"]["operation"] == "none"
    assert report["plan"]["restart_required"] is False
    assert "matches desired" in report["plan"]["reasons"][0]


def test_planner_reports_outdated_update(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin", "1.2.3")
    installed = _lockfile(desired, resolved_version="1.2.2")

    report = _plan(desired, installed)

    assert report["plan"]["update_state"] == "update"
    assert report["plan"]["operation"] == "update"
    assert report["plan"]["receipt"]["previous_version"] == "1.2.2"
    assert report["plan"]["dry_run_diff"]["resolved_version"] == {
        "from": "1.2.2",
        "to": "1.2.3",
    }


def test_planner_reports_downgrade(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin", "1.2.3")
    installed = _lockfile(desired, resolved_version="1.2.4")

    report = _plan(desired, installed)

    assert report["plan"]["update_state"] == "downgrade"
    assert report["plan"]["operation"] == "downgrade"
    assert "older than installed 1.2.4" in report["plan"]["reasons"][0]


def test_planner_keeps_pinned_install_no_op(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin", "1.2.3")
    installed = _lockfile(desired, resolved_version="1.2.2", pinned=True)

    report = _plan(desired, installed)

    assert report["plan"]["update_state"] == "no-op"
    assert report["plan"]["operation"] == "none"
    assert report["plan"]["restart_required"] is False
    assert report["plan"]["receipt"]["lockfile_after"]["pinned"] is True
    assert "pinned" in report["plan"]["reasons"][0]


def test_planner_reports_incompatible_for_plugin_mismatch(tmp_path: Path) -> None:
    desired = _desired(tmp_path / "plugin")
    installed = _lockfile(desired, plugin_id="other-plugin")

    report = _plan(desired, installed)

    assert report["plan"]["update_state"] == "incompatible"
    assert report["plan"]["operation"] == "none"
    assert report["plan"]["restart_required"] is False
    assert "plugin_id differs" in report["plan"]["reasons"][0]


def test_planner_reports_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(manager.PluginPlanError, match="not found"):
        manager.build_desired_manifest(
            package_surface="codex",
            plugin_root=tmp_path,
            manifest_path=tmp_path / ".codex-plugin" / "plugin.json",
            artifact_url=None,
            source="repo-package",
        )


def test_cli_reports_corrupt_lockfile_without_mutation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plugin_root = tmp_path / "plugin"
    manifest_path = _manifest(plugin_root)
    lockfile = tmp_path / "palace.lock.json"
    lockfile.write_text("{not-json", encoding="utf-8")

    status = manager.main(
        [
            "--plugin-root",
            str(plugin_root),
            "--manifest-path",
            str(manifest_path),
            "--installed-lockfile",
            str(lockfile),
            "--installed-plugin-path",
            str(tmp_path / "installed"),
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    report = json.loads(captured.out)
    assert report["mutating"] is False
    assert report["plan"]["update_state"] == "incompatible"
    assert "corrupt JSON" in report["plan"]["reasons"][0]


def test_plan_output_redacts_secret_like_metadata(tmp_path: Path) -> None:
    manifest_path = _manifest(
        tmp_path / "plugin",
        extra={"api_token": "secret-value", "nested": {"password": "secret-password"}},
    )
    args = parse_args(
        [
            "--plugin-root",
            str(tmp_path / "plugin"),
            "--manifest-path",
            str(manifest_path),
        ]
    )

    report = manager.build_plan_from_args(args)

    encoded = json.dumps(report)
    assert "secret-value" not in encoded
    assert "secret-password" not in encoded
    assert report["desired"]["resolved_version"] == "1.2.3"
