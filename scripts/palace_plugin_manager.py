#!/usr/bin/env python3
"""Plan Palace plugin updates without mutating installed profiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
CODEX_PLUGIN_ROOT = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"
CODEX_PLUGIN_MANIFEST = CODEX_PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
LOCKFILE_SCHEMA_VERSION = 1
MANAGER_VERSION = "0.1.0"


class PluginPlanError(RuntimeError):
    """Raised when a plugin plan cannot be built from caller input."""


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PluginPlanError(f"{label} not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PluginPlanError(f"{label} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PluginPlanError(f"{label} at {path} must be a JSON object")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _directory_digest(root: Path) -> str:
    if not root.is_dir():
        raise PluginPlanError(f"plugin root not found at {root}")
    file_entries: list[dict[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = str(path.relative_to(root))
        file_entries.append({"path": relative_path, "sha256": _sha256_file(path)})
    return _sha256_text(json.dumps(file_entries, sort_keys=True, separators=(",", ":")))


def _parse_version(value: str) -> tuple[int, ...] | None:
    parts: list[int] = []
    for chunk in value.split("."):
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return tuple(parts)


def _compare_versions(desired: str, installed: str | None) -> str:
    if installed is None:
        return "missing"
    desired_parts = _parse_version(desired)
    installed_parts = _parse_version(installed)
    if desired_parts is None or installed_parts is None:
        return "unknown"
    if desired_parts > installed_parts:
        return "newer"
    if desired_parts < installed_parts:
        return "older"
    return "same"


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            lowered = key.lower()
            if "key" in lowered or "secret" in lowered or "token" in lowered or "password" in lowered:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact(nested)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def build_desired_manifest(
    *,
    package_surface: str,
    plugin_root: Path,
    manifest_path: Path,
    artifact_url: str | None,
    source: str,
) -> dict[str, Any]:
    manifest = _load_json_file(manifest_path, "desired plugin manifest")
    plugin_id = manifest.get("name")
    version = manifest.get("version")
    if not isinstance(plugin_id, str) or not plugin_id:
        raise PluginPlanError("desired plugin manifest is missing string field name")
    if not isinstance(version, str) or not version:
        raise PluginPlanError("desired plugin manifest is missing string field version")
    compatibility = (
        manifest.get("compatibility") if isinstance(manifest.get("compatibility"), dict) else {}
    )
    return {
        "plugin_id": plugin_id,
        "package_surface": package_surface,
        "source": source,
        "marketplace": manifest.get("repository") or manifest.get("homepage"),
        "artifact_url": artifact_url,
        "resolved_version": version,
        "manifest_digest": f"sha256:{_sha256_file(manifest_path)}",
        "artifact_digest": f"sha256:{_directory_digest(plugin_root)}",
        "manifest_path": str(manifest_path),
        "plugin_root": str(plugin_root),
        "compatibility": compatibility,
    }


def build_lockfile_entry(
    *,
    desired: dict[str, Any],
    installed_path: Path | None,
    previous_version: str | None = None,
    enabled: bool = True,
    pinned: bool = False,
    skipped: bool = False,
    restart_required: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": LOCKFILE_SCHEMA_VERSION,
        "plugin_id": desired["plugin_id"],
        "package_surface": desired["package_surface"],
        "source": desired["source"],
        "marketplace": desired["marketplace"],
        "artifact_url": desired["artifact_url"],
        "resolved_version": desired["resolved_version"],
        "manifest_digest": desired["manifest_digest"],
        "artifact_digest": desired["artifact_digest"],
        "installed_path": str(installed_path) if installed_path else None,
        "installed_at": _iso_now(),
        "previous_version": previous_version,
        "enabled": enabled,
        "pinned": pinned,
        "skipped": skipped,
        "restart_required": restart_required,
    }


def _load_lockfile(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None or not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"installed lockfile is corrupt JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "installed lockfile must be a JSON object"
    entry = payload.get("plugin") if isinstance(payload.get("plugin"), dict) else payload
    return entry, None


def _compatibility_reasons(desired: dict[str, Any], installed: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    compatibility = (
        desired.get("compatibility") if isinstance(desired.get("compatibility"), dict) else {}
    )
    min_manager = compatibility.get("min_palace_plugin_manager")
    if isinstance(min_manager, str):
        relation = _compare_versions(MANAGER_VERSION, min_manager)
        if relation == "older":
            reasons.append(
                f"manager {MANAGER_VERSION} is older than required {min_manager}"
            )
    if installed is None:
        return reasons
    if installed.get("schema_version") not in {None, LOCKFILE_SCHEMA_VERSION}:
        reasons.append(f"unsupported lockfile schema_version {installed.get('schema_version')}")
    if installed.get("plugin_id") and installed.get("plugin_id") != desired["plugin_id"]:
        reasons.append("installed lockfile plugin_id differs from desired manifest")
    if installed.get("package_surface") and installed.get("package_surface") != desired["package_surface"]:
        reasons.append("installed lockfile package_surface differs from desired manifest")
    return reasons


def build_update_plan(
    *,
    desired: dict[str, Any],
    installed_lockfile: dict[str, Any] | None,
    lockfile_error: str | None,
    installed_path: Path | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if lockfile_error:
        reasons.append(lockfile_error)
    reasons.extend(_compatibility_reasons(desired, installed_lockfile))
    if reasons:
        update_state = "incompatible"
        operation = "none"
        restart_required = False
    elif installed_lockfile is None:
        update_state = "install"
        operation = "install"
        restart_required = True
        reasons.append("no installed lockfile found")
    else:
        installed_version = (
            str(installed_lockfile["resolved_version"])
            if installed_lockfile.get("resolved_version") is not None
            else None
        )
        version_relation = _compare_versions(desired["resolved_version"], installed_version)
        digest_changed = (
            installed_lockfile.get("manifest_digest") != desired["manifest_digest"]
            or installed_lockfile.get("artifact_digest") != desired["artifact_digest"]
        )
        pinned = bool(installed_lockfile.get("pinned"))
        skipped = bool(installed_lockfile.get("skipped"))
        if pinned:
            update_state = "no-op"
            operation = "none"
            restart_required = False
            reasons.append("installed lockfile is pinned")
        elif skipped:
            update_state = "no-op"
            operation = "none"
            restart_required = False
            reasons.append("installed lockfile is marked skipped")
        elif version_relation == "newer":
            update_state = "update"
            operation = "update"
            restart_required = True
            reasons.append(
                f"desired version {desired['resolved_version']} is newer than installed "
                f"{installed_version}"
            )
        elif version_relation == "older":
            update_state = "downgrade"
            operation = "downgrade"
            restart_required = True
            reasons.append(
                f"desired version {desired['resolved_version']} is older than installed "
                f"{installed_version}"
            )
        elif digest_changed:
            update_state = "update"
            operation = "update"
            restart_required = True
            reasons.append("manifest or artifact digest changed at the same version")
        else:
            update_state = "no-op"
            operation = "none"
            restart_required = False
            reasons.append("installed lockfile matches desired manifest and artifact")

    next_lockfile = build_lockfile_entry(
        desired=desired,
        installed_path=installed_path,
        previous_version=(
            str(installed_lockfile.get("resolved_version"))
            if installed_lockfile and installed_lockfile.get("resolved_version") is not None
            else None
        ),
        enabled=bool(installed_lockfile.get("enabled", True)) if installed_lockfile else True,
        pinned=bool(installed_lockfile.get("pinned", False)) if installed_lockfile else False,
        skipped=bool(installed_lockfile.get("skipped", False)) if installed_lockfile else False,
        restart_required=restart_required,
    )
    receipt = {
        "receipt_type": "palace-plugin-update-plan",
        "manager_version": MANAGER_VERSION,
        "mutating": False,
        "operation": operation,
        "plugin_id": desired["plugin_id"],
        "package_surface": desired["package_surface"],
        "from_version": (
            str(installed_lockfile.get("resolved_version"))
            if installed_lockfile and installed_lockfile.get("resolved_version") is not None
            else None
        ),
        "to_version": desired["resolved_version"],
        "restart_required": restart_required,
        "previous_version": next_lockfile["previous_version"],
        "lockfile_after": next_lockfile,
    }
    return _redact(
        {
            "report": "palace-plugin-update-plan",
            "dry_run": True,
            "mutating": False,
            "manager_version": MANAGER_VERSION,
            "desired": desired,
            "installed_lockfile": installed_lockfile,
            "plan": {
                "update_state": update_state,
                "operation": operation,
                "restart_required": restart_required,
                "reasons": reasons,
                "dry_run_diff": {
                    "resolved_version": {
                        "from": (
                            str(installed_lockfile.get("resolved_version"))
                            if installed_lockfile and installed_lockfile.get("resolved_version") is not None
                            else None
                        ),
                        "to": desired["resolved_version"],
                    },
                    "manifest_digest": {
                        "from": installed_lockfile.get("manifest_digest") if installed_lockfile else None,
                        "to": desired["manifest_digest"],
                    },
                    "artifact_digest": {
                        "from": installed_lockfile.get("artifact_digest") if installed_lockfile else None,
                        "to": desired["artifact_digest"],
                    },
                },
                "receipt": receipt,
            },
        }
    )


def build_plan_from_args(args: argparse.Namespace) -> dict[str, Any]:
    plugin_root = Path(args.plugin_root).expanduser()
    manifest_path = Path(args.manifest_path).expanduser()
    installed_path = Path(args.installed_plugin_path).expanduser() if args.installed_plugin_path else None
    desired = build_desired_manifest(
        package_surface=args.package_surface,
        plugin_root=plugin_root,
        manifest_path=manifest_path,
        artifact_url=args.artifact_url,
        source=args.source,
    )
    installed_lockfile, lockfile_error = _load_lockfile(
        Path(args.installed_lockfile).expanduser() if args.installed_lockfile else None
    )
    return build_update_plan(
        desired=desired,
        installed_lockfile=installed_lockfile,
        lockfile_error=lockfile_error,
        installed_path=installed_path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        choices=["plan"],
        default="plan",
        help="Plan an update. The command is read-only and does not apply changes.",
    )
    parser.add_argument("--package-surface", choices=["codex", "hermes"], default="codex")
    parser.add_argument("--plugin-root", default=str(CODEX_PLUGIN_ROOT))
    parser.add_argument("--manifest-path", default=str(CODEX_PLUGIN_MANIFEST))
    parser.add_argument("--installed-lockfile", default=None)
    parser.add_argument("--installed-plugin-path", default=None)
    parser.add_argument("--source", default="repo-package")
    parser.add_argument("--artifact-url", default=None)
    parser.add_argument("--format", choices=["json"], default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = build_plan_from_args(args)
    except PluginPlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True) + "\n", end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
