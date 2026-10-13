"""Regression: the published Hermes plugin release package must be complete.

The 1.0.41 published asset shipped only 4 members and failed to import
(``ModuleNotFoundError: No module named 'plugin.checkpoint'``). This test runs
the REAL packaging script and asserts the shipped archive is membership-complete,
deterministic, importable, and free of raw secret receipts.

It does not touch the network, a cluster, or any secret.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGER = REPO_ROOT / "scripts" / "package_hermes_memory_plugin.py"
PLUGIN_DIR = REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth"
VERSION = (PLUGIN_DIR / "plugin.yaml").read_text().split("version:", 1)[1].split("\n", 1)[0].strip()
ASSET_BASE = f"palaceoftruth-hermes-memory-plugin-v{VERSION}"
ARCHIVE_ROOT = ASSET_BASE
EXPECTED_ARCHIVE_PATHS = {
    f"{ARCHIVE_ROOT}/plugin/__init__.py",
    f"{ARCHIVE_ROOT}/plugin/checkpoint.py",
    f"{ARCHIVE_ROOT}/plugin/outbox.py",
    f"{ARCHIVE_ROOT}/plugin/credentials.py",
    f"{ARCHIVE_ROOT}/plugin/cli.py",
    f"{ARCHIVE_ROOT}/plugin/plugin.yaml",
    f"{ARCHIVE_ROOT}/README.md",
    f"{ARCHIVE_ROOT}/Dockerfile",
}
REQUIRED_MODULE_SOURCES = ("__init__.py", "checkpoint.py", "outbox.py", "credentials.py", "cli.py")


def _build(output_dir: Path, epoch: str = "1700000000") -> dict[str, Path]:
    env = dict(os.environ)
    env["SOURCE_DATE_EPOCH"] = epoch
    result = subprocess.run(
        [sys.executable, str(PACKAGER), "--output-dir", str(output_dir)],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        "tar": output_dir / f"{ASSET_BASE}.tar.gz",
        "zip": output_dir / f"{ASSET_BASE}.zip",
        "json": output_dir / f"{ASSET_BASE}.json",
        "sha256": output_dir / f"{ASSET_BASE}.sha256",
    }


def test_published_archive_is_membership_complete(tmp_path):
    assets = _build(tmp_path)
    with tarfile.open(assets["tar"]) as tf:
        tar_names = {m.name for m in tf.getmembers()}
    with zipfile.ZipFile(assets["zip"]) as zf:
        zip_names = set(zf.namelist())
    assert tar_names == EXPECTED_ARCHIVE_PATHS, sorted(EXPECTED_ARCHIVE_PATHS ^ tar_names)
    assert zip_names == EXPECTED_ARCHIVE_PATHS, sorted(EXPECTED_ARCHIVE_PATHS ^ zip_names)


def test_archive_members_are_byte_equal_to_source(tmp_path):
    assets = _build(tmp_path)
    source = {
        f"{ARCHIVE_ROOT}/plugin/{name}": (PLUGIN_DIR / name).read_bytes()
        for name in ("__init__.py", "checkpoint.py", "outbox.py", "credentials.py", "cli.py", "plugin.yaml")
    }
    source[f"{ARCHIVE_ROOT}/README.md"] = (PLUGIN_DIR / "README.md").read_bytes()
    source[f"{ARCHIVE_ROOT}/Dockerfile"] = (PLUGIN_DIR / "Dockerfile").read_bytes()
    with tarfile.open(assets["tar"]) as tf:
        for name, data in source.items():
            member = tf.extractfile(name)
            assert member is not None and member.read() == data, name


def test_inventory_lists_every_shipped_file(tmp_path):
    assets = _build(tmp_path)
    inventory = json.loads(assets["json"].read_text())
    assert inventory["version"] == VERSION
    assert len(inventory["files"]) == len(EXPECTED_ARCHIVE_PATHS)
    archive_paths = {row["archive_path"] for row in inventory["files"]}
    assert archive_paths == {p[len(ARCHIVE_ROOT) + 1:] for p in EXPECTED_ARCHIVE_PATHS}
    for row in inventory["files"]:
        digest = hashlib.sha256((REPO_ROOT / row["source_path"]).read_bytes()).hexdigest()
        assert digest == row["sha256"], row["source_path"]


def test_archives_are_deterministic(tmp_path):
    first = _build(tmp_path / "a")
    second = _build(tmp_path / "b")
    for key in ("tar", "zip"):
        assert first[key].read_bytes() == second[key].read_bytes(), key


def test_no_raw_secret_receipts_are_shipped(tmp_path):
    assets = _build(tmp_path)
    with tarfile.open(assets["tar"]) as tf:
        names = [m.name for m in tf.getmembers()]
    for name in names:
        lowered = name.lower()
        assert "receipt" not in lowered
        assert not lowered.endswith((".xml", ".json"))
        assert "all-green" not in lowered


def test_extracted_package_imports_with_all_modules(tmp_path):
    assets = _build(tmp_path)
    extract = tmp_path / "extract"
    with tarfile.open(assets["tar"]) as tf:
        tf.extractall(extract, filter="data")
    pkg_dir = extract / ARCHIVE_ROOT
    # Minimal host stub: the only top-level host import is agent.memory_provider.
    host = tmp_path / "host"
    (host / "agent").mkdir(parents=True)
    (host / "agent" / "__init__.py").write_text("")
    (host / "agent" / "memory_provider.py").write_text(
        "class MemoryProvider:\n    pass\n"
    )
    (pkg_dir / "plugin").mkdir(exist_ok=True)
    # The archive ships modules under plugin/; make the package importable as "plugin".
    code = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(host)!r})\n"
        f"sys.path.insert(0, {str(extract / ARCHIVE_ROOT)!r})\n"
        "import plugin\n"
        "importlib.import_module('plugin.checkpoint')\n"
        "importlib.import_module('plugin.outbox')\n"
        "importlib.import_module('plugin.credentials')\n"
        "importlib.import_module('plugin.cli')\n"
        "assert hasattr(plugin, 'PalaceOfTruthMemoryProvider')\n"
        "print('OK')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_missing_module_file_is_detected():
    """A package that omits a required module must fail the completeness check."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("packager", PACKAGER)
    packager = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(packager)
    for module in REQUIRED_MODULE_SOURCES:
        assert module in packager.PLUGIN_FILES, module
        assert (PLUGIN_DIR / module).exists(), module
