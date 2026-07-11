from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


packager = _load("package_agent_memory_plugin", REPO_ROOT / "scripts/package_agent_memory_plugin.py")
detector = _load("detect_agent_plugin_release", REPO_ROOT / "scripts/detect_agent_plugin_release.py")


def test_agent_package_changes_trigger_release() -> None:
    assert detector.should_release_for_changed_paths(
        ["third_party_plugins/agent_clients/palaceoftruth-memory/skills/palaceoftruth-memory/SKILL.md"]
    )
    assert detector.should_release_for_changed_paths(["scripts/package_agent_memory_plugin.py"])
    assert not detector.should_release_for_changed_paths(["chart/Chart.yaml"])


def test_agent_release_assets_are_deterministic_and_safe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1782540000")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    first = packager.package_plugin(tmp_path / "first")
    second = packager.package_plugin(tmp_path / "second")

    assert {key: packager._sha256(first[key]) for key in first} == {
        key: packager._sha256(second[key]) for key in second
    }
    manifest = json.loads(first["metadata"].read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "palace.agent-client.update-manifest.v1"
    assert manifest["version"] == "0.2.0"
    assert manifest["release_tag"] == "agent-memory-plugin-v0.2.0"
    assert manifest["source_revision"] == "abc123"
    assert manifest["install"]["ref"] == manifest["release_tag"]
    assert "do not delete" in manifest["rollback"]

    expected_prefix = "palaceoftruth-agent-memory-plugin-v0.2.0/"
    with tarfile.open(first["tar"]) as archive:
        tar_names = archive.getnames()
    with zipfile.ZipFile(first["zip"]) as archive:
        zip_names = archive.namelist()
    for name in tar_names + zip_names:
        assert name.startswith(expected_prefix)
        assert ".." not in Path(name).parts

    checksums = first["sha256"].read_text(encoding="utf-8")
    for key in ("tar", "zip", "metadata"):
        assert f"{packager._sha256(first[key])}  {first[key].name}" in checksums
