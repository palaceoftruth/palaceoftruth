#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth"
PLUGIN_FILES = {
    "__init__.py": "plugin/__init__.py",
    "plugin.yaml": "plugin/plugin.yaml",
    "README.md": "README.md",
    "Dockerfile": "Dockerfile",
}
MANIFEST_SCHEMA_VERSION = "palace.plugin.update-manifest.v1"
PLUGIN_ID = "hermes.memory.palaceoftruth"
PACKAGE_SURFACE = "hermes-memory-plugin"
ARTIFACT_REPOSITORY = "ghcr.io/palaceoftruth/palaceoftruth/hermes-memory-plugin"


def _read_plugin_metadata() -> dict[str, str]:
    plugin_yaml = PLUGIN_DIR / "plugin.yaml"
    metadata: dict[str, str] = {}
    for line in plugin_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        metadata[key.strip()] = raw_value.strip().strip('"').strip("'")
    required = {"name", "version", "description"}
    missing = sorted(required - metadata.keys())
    if missing:
        raise SystemExit(f"plugin.yaml is missing required fields: {', '.join(missing)}")
    return metadata


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_name(archive_root: str, archive_name: str) -> str:
    candidate = Path(archive_root) / archive_name
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SystemExit(f"unsafe archive path: {candidate}")
    return candidate.as_posix()


def _file_manifest() -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for source_name, archive_name in PLUGIN_FILES.items():
        source_path = PLUGIN_DIR / source_name
        files.append(
            {
                "source_path": str(source_path.relative_to(REPO_ROOT)),
                "archive_path": archive_name,
                "size_bytes": source_path.stat().st_size,
                "sha256": _sha256(source_path),
            }
        )
    return files


def _artifact_entry(path: Path, kind: str, media_type: str) -> dict[str, Any]:
    return {
        "name": path.name,
        "kind": kind,
        "media_type": media_type,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _build_update_manifest(
    *,
    metadata: dict[str, str],
    tar_path: Path,
    zip_path: Path,
) -> dict[str, Any]:
    version = metadata["version"]
    release_tag = f"hermes-memory-plugin-v{version}"
    asset_base = f"palaceoftruth-hermes-memory-plugin-v{version}"
    generated_at = os.environ.get("SOURCE_DATE_EPOCH")
    if generated_at:
        try:
            generated = datetime.fromtimestamp(int(generated_at), tz=timezone.utc)
        except ValueError as exc:
            raise SystemExit("SOURCE_DATE_EPOCH must be an integer Unix timestamp") from exc
    else:
        generated = datetime.now(tz=timezone.utc)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "plugin_id": PLUGIN_ID,
        "package_surface": PACKAGE_SURFACE,
        "name": metadata["name"],
        "version": version,
        "description": metadata["description"],
        "release_tag": release_tag,
        "release": {
            "tag": release_tag,
            "assets": {
                "tar": f"{asset_base}.tar.gz",
                "zip": f"{asset_base}.zip",
                "manifest": f"{asset_base}.json",
                "checksums": f"{asset_base}.sha256",
            },
        },
        "source_directory": str(PLUGIN_DIR.relative_to(REPO_ROOT)),
        "repository": metadata.get("repository"),
        "license": metadata.get("license"),
        "owner": metadata.get("owner"),
        "generated_at": generated.isoformat().replace("+00:00", "Z"),
        "artifacts": [
            _artifact_entry(tar_path, "tarball", "application/gzip"),
            _artifact_entry(zip_path, "zip", "application/zip"),
        ],
        "files": _file_manifest(),
        "compatibility": {
            "host": {
                "name": "Hermes",
                "plugin_api": "memory",
                "minimum_version": None,
            },
            "client": {
                "name": "Palace of Truth API",
                "required_routes": [
                    "/api/v1/memory/whoami",
                    "/api/v1/memory/scopes",
                    "/api/v1/memory/retrieve-agent",
                    "/api/v1/memory/entries",
                    "/api/v1/memory/entries:batch",
                ],
            },
            "runtime": {
                "language": "python",
                "python": ">=3.9",
                "container_platforms": ["linux/amd64"],
            },
        },
        "provenance": {
            "source_repository": metadata.get("repository"),
            "source_directory": str(PLUGIN_DIR.relative_to(REPO_ROOT)),
            "source_revision": os.environ.get("GITHUB_SHA") or None,
            "release_tag": release_tag,
            "container_repository": ARTIFACT_REPOSITORY,
            "container_tag": os.environ.get("HERMES_PLUGIN_IMAGE_TAG") or None,
        },
        "rollback": {
            "previous_version": None,
            "previous_release_tag": None,
            "strategy": "Install a prior pinned release asset and verify its checksum before replacing the plugin directory.",
        },
        "signature": {
            "status": "reserved",
            "entries": [],
        },
    }


def _package_plugin(output_dir: Path, metadata: dict[str, str]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    version = metadata["version"]
    asset_base = f"palaceoftruth-hermes-memory-plugin-v{version}"
    archive_root = f"{asset_base}"

    tar_path = output_dir / f"{asset_base}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar_handle:
        for source_name, archive_name in PLUGIN_FILES.items():
            tar_handle.add(
                PLUGIN_DIR / source_name,
                arcname=_safe_archive_name(archive_root, archive_name),
            )

    zip_path = output_dir / f"{asset_base}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_handle:
        for source_name, archive_name in PLUGIN_FILES.items():
            zip_handle.write(
                PLUGIN_DIR / source_name,
                arcname=_safe_archive_name(archive_root, archive_name),
            )

    metadata_path = output_dir / f"{asset_base}.json"
    update_manifest = _build_update_manifest(
        metadata=metadata,
        tar_path=tar_path,
        zip_path=zip_path,
    )
    metadata_path.write_text(
        json.dumps(update_manifest, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    checksum_path = output_dir / f"{asset_base}.sha256"
    checksum_path.write_text(
        "\n".join(
            f"{_sha256(path)}  {path.name}"
            for path in (tar_path, zip_path, metadata_path)
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "tar": tar_path,
        "zip": zip_path,
        "metadata": metadata_path,
        "sha256": checksum_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Package the Hermes Palace of Truth memory plugin.")
    parser.add_argument("--output-dir", type=Path, help="Directory to write release assets into.")
    parser.add_argument(
        "--print-version",
        action="store_true",
        help="Print the plugin version from plugin.yaml and exit.",
    )
    args = parser.parse_args()

    metadata = _read_plugin_metadata()
    if args.print_version:
        print(metadata["version"])
        return 0

    if args.output_dir is None:
        raise SystemExit("--output-dir is required unless --print-version is used")

    created = _package_plugin(args.output_dir, metadata)
    for label, path in created.items():
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
