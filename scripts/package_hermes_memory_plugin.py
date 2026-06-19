#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth"
PLUGIN_FILES = {
    "__init__.py": "plugin/__init__.py",
    "plugin.yaml": "plugin/plugin.yaml",
    "README.md": "README.md",
    "Dockerfile": "Dockerfile",
}


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
                arcname=f"{archive_root}/{archive_name}",
            )

    zip_path = output_dir / f"{asset_base}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zip_handle:
        for source_name, archive_name in PLUGIN_FILES.items():
            zip_handle.write(
                PLUGIN_DIR / source_name,
                arcname=f"{archive_root}/{archive_name}",
            )

    metadata_path = output_dir / f"{asset_base}.json"
    metadata_path.write_text(
        json.dumps(
            {
                "name": metadata["name"],
                "version": metadata["version"],
                "description": metadata["description"],
                "release_tag": f"hermes-memory-plugin-v{metadata['version']}",
                "source_directory": str(PLUGIN_DIR.relative_to(REPO_ROOT)),
                "files": list(PLUGIN_FILES.values()),
            },
            indent=2,
        )
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
