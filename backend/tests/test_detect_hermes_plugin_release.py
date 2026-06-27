from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "detect_hermes_plugin_release.py"
SPEC = importlib.util.spec_from_file_location("detect_hermes_plugin_release", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
detect_module = importlib.util.module_from_spec(SPEC)
sys.modules["detect_hermes_plugin_release"] = detect_module
SPEC.loader.exec_module(detect_module)

PACKAGE_SCRIPT_PATH = REPO_ROOT / "scripts" / "package_hermes_memory_plugin.py"
PACKAGE_SPEC = importlib.util.spec_from_file_location("package_hermes_memory_plugin", PACKAGE_SCRIPT_PATH)
assert PACKAGE_SPEC is not None
assert PACKAGE_SPEC.loader is not None
package_module = importlib.util.module_from_spec(PACKAGE_SPEC)
sys.modules["package_hermes_memory_plugin"] = package_module
PACKAGE_SPEC.loader.exec_module(package_module)


def test_readme_only_plugin_change_triggers_release() -> None:
    assert detect_module.should_release_for_changed_paths(
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


def test_packaging_script_change_triggers_release() -> None:
    assert detect_module.should_release_for_changed_paths(["scripts/package_hermes_memory_plugin.py"])


def test_packaged_release_writes_canonical_update_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1782540000")
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    monkeypatch.setenv("HERMES_PLUGIN_IMAGE_TAG", "abc12345")

    metadata = package_module._read_plugin_metadata()
    created = package_module._package_plugin(tmp_path, metadata)
    manifest = json.loads(created["metadata"].read_text(encoding="utf-8"))

    assert set(created) == {"tar", "zip", "metadata", "sha256"}
    assert manifest["schema_version"] == "palace.plugin.update-manifest.v1"
    assert manifest["plugin_id"] == "hermes.memory.palaceoftruth"
    assert manifest["package_surface"] == "hermes-memory-plugin"
    assert manifest["version"] == metadata["version"]
    assert manifest["release_tag"] == f"hermes-memory-plugin-v{metadata['version']}"
    assert manifest["release"]["assets"] == {
        "tar": f"palaceoftruth-hermes-memory-plugin-v{metadata['version']}.tar.gz",
        "zip": f"palaceoftruth-hermes-memory-plugin-v{metadata['version']}.zip",
        "manifest": f"palaceoftruth-hermes-memory-plugin-v{metadata['version']}.json",
        "checksums": f"palaceoftruth-hermes-memory-plugin-v{metadata['version']}.sha256",
    }
    assert manifest["source_directory"] == "third_party_plugins/hermes/memory/palaceoftruth"
    assert manifest["generated_at"] == "2026-06-27T06:00:00Z"
    assert manifest["compatibility"]["host"]["plugin_api"] == "memory"
    assert manifest["compatibility"]["runtime"]["python"] == ">=3.9"
    assert "/api/v1/memory/retrieve-agent" in manifest["compatibility"]["client"]["required_routes"]
    assert manifest["signature"] == {"status": "reserved", "entries": []}
    assert manifest["provenance"]["source_revision"] == "abc123"
    assert manifest["provenance"]["container_tag"] == "abc12345"
    assert manifest["rollback"]["previous_version"] is None

    artifacts = {artifact["name"]: artifact for artifact in manifest["artifacts"]}
    for label in ("tar", "zip"):
        path = created[label]
        artifact = artifacts[path.name]
        assert artifact["size_bytes"] == path.stat().st_size
        assert artifact["sha256"] == package_module._sha256(path)

    declared_files = {entry["archive_path"]: entry for entry in manifest["files"]}
    assert set(declared_files) == set(package_module.PLUGIN_FILES.values())
    for entry in declared_files.values():
        source_path = REPO_ROOT / entry["source_path"]
        assert entry["size_bytes"] == source_path.stat().st_size
        assert entry["sha256"] == package_module._sha256(source_path)

    checksum_text = created["sha256"].read_text(encoding="utf-8")
    assert f"{package_module._sha256(created['metadata'])}  {created['metadata'].name}" in checksum_text


def test_packaged_archives_keep_members_under_release_root(tmp_path: Path) -> None:
    metadata = package_module._read_plugin_metadata()
    created = package_module._package_plugin(tmp_path, metadata)
    expected_prefix = f"palaceoftruth-hermes-memory-plugin-v{metadata['version']}/"

    with tarfile.open(created["tar"]) as tar_handle:
        tar_names = tar_handle.getnames()
    with zipfile.ZipFile(created["zip"]) as zip_handle:
        zip_names = zip_handle.namelist()

    for archive_names in (tar_names, zip_names):
        assert archive_names
        for archive_name in archive_names:
            assert archive_name.startswith(expected_prefix)
            assert not archive_name.startswith("/")
            assert ".." not in Path(archive_name).parts
