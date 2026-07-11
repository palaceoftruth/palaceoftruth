#!/usr/bin/env python3
"""Build deterministic release assets for the Palace agent-client plugin."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"
MANIFEST_PATH = PLUGIN_DIR / ".codex-plugin" / "plugin.json"
SCHEMA_VERSION = "palace.agent-client.update-manifest.v1"
PACKAGE_SURFACE = "agent-client-plugin"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata() -> dict[str, Any]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for field in ("name", "version", "description"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise SystemExit(f"Codex plugin manifest is missing string field {field}")
    return payload


def _source_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "0")
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit("SOURCE_DATE_EPOCH must be an integer Unix timestamp") from exc


def _files() -> list[Path]:
    return sorted(path for path in PLUGIN_DIR.rglob("*") if path.is_file())


def _tar_bytes(root_name: str, epoch: int) -> bytes:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w") as archive:
        for path in _files():
            info = archive.gettarinfo(str(path), arcname=f"{root_name}/{path.relative_to(PLUGIN_DIR)}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            with path.open("rb") as handle:
                archive.addfile(info, handle)
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=epoch) as handle:
        handle.write(tar_buffer.getvalue())
    return compressed.getvalue()


def _write_zip(path: Path, root_name: str, epoch: int) -> None:
    timestamp = datetime.fromtimestamp(max(epoch, 315532800), tz=timezone.utc)
    date_time = (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in _files():
            info = zipfile.ZipInfo(f"{root_name}/{source.relative_to(PLUGIN_DIR)}", date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())


def package_plugin(output_dir: Path) -> dict[str, Path]:
    metadata = _metadata()
    version = metadata["version"]
    root_name = f"palaceoftruth-agent-memory-plugin-v{version}"
    output_dir.mkdir(parents=True, exist_ok=True)
    epoch = _source_epoch()
    tar_path = output_dir / f"{root_name}.tar.gz"
    tar_path.write_bytes(_tar_bytes(root_name, epoch))
    zip_path = output_dir / f"{root_name}.zip"
    _write_zip(zip_path, root_name, epoch)
    file_manifest = [
        {
            "path": str(path.relative_to(PLUGIN_DIR)),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in _files()
    ]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "plugin_id": metadata["name"],
        "package_surface": PACKAGE_SURFACE,
        "version": version,
        "release_tag": f"agent-memory-plugin-v{version}",
        "source_revision": os.environ.get("GITHUB_SHA") or None,
        "source_directory": str(PLUGIN_DIR.relative_to(REPO_ROOT)),
        "generated_at": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "artifacts": [
            {"name": tar_path.name, "sha256": _sha256(tar_path), "size_bytes": tar_path.stat().st_size},
            {"name": zip_path.name, "sha256": _sha256(zip_path), "size_bytes": zip_path.stat().st_size},
        ],
        "files": file_manifest,
        "install": {
            "marketplace": "palaceoftruth/palaceoftruth",
            "ref": f"agent-memory-plugin-v{version}",
            "plugin": "palaceoftruth-memory",
            "restart_required": True,
        },
        "rollback": "Reinstall a prior verified release tag; do not delete or rewrite Palace memory.",
    }
    manifest_path = output_dir / f"{root_name}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksum_path = output_dir / f"{root_name}.sha256"
    checksum_path.write_text(
        "".join(f"{_sha256(item)}  {item.name}\n" for item in (tar_path, zip_path, manifest_path)),
        encoding="utf-8",
    )
    return {"tar": tar_path, "zip": zip_path, "metadata": manifest_path, "sha256": checksum_path}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--print-version", action="store_true")
    args = parser.parse_args()
    if args.print_version:
        print(_metadata()["version"])
        return 0
    if args.output_dir is None:
        raise SystemExit("--output-dir is required unless --print-version is used")
    for label, path in package_plugin(args.output_dir).items():
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
