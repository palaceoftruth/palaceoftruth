#!/usr/bin/env python3
"""Validate Palace agent plugin package compatibility without mutation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGIN_ROOT = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
MAX_BUNDLE_BYTES = 5 * 1024 * 1024

CODEX_FIELDS = {
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "skills",
    "mcpServers",
    "interface",
    "compatibility",
    "source",
    "digests",
}
CLAUDE_FIELDS = {
    "name",
    "description",
    "version",
    "author",
    "repository",
    "license",
    "keywords",
    "skills",
    "compatibility",
    "source",
    "digests",
}
OPENCLAW_FIELDS = {
    "name",
    "version",
    "description",
    "capabilities",
    "permissions",
    "entrypoints",
    "skills",
    "source",
    "digests",
    "compatibility",
}
SKILL_FRONTMATTER_FIELDS = {
    "name",
    "description",
    "version",
    "license",
    "source",
    "homepage",
    "repository",
    "tags",
    "compatibility",
    "digest",
}
SECRET_FIELD_RE = re.compile(r"(api[_-]?key|token|secret|password)", re.IGNORECASE)


class CompatibilityError(RuntimeError):
    """Raised when the compatibility report cannot be built."""


def _load_json_file(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise CompatibilityError(f"{label} not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompatibilityError(f"{label} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CompatibilityError(f"{label} at {path} must be a JSON object")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_path(value: str) -> bool:
    return bool(value) and not value.startswith("/") and "://" not in value


def _relative_path_exists(root: Path, value: str) -> bool:
    return (root / value).exists()


def _field_names(payload: Any, prefix: str = "") -> set[str]:
    names: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            names.add(path)
            names.update(_field_names(value, path))
    elif isinstance(payload, list):
        for item in payload:
            names.update(_field_names(item, prefix))
    return names


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            redacted[key] = "<redacted>" if SECRET_FIELD_RE.search(key) else _redact(nested)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _source_metadata(payload: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    selected: dict[str, Any] = {
        "manifest_path": str(manifest_path),
        "manifest_digest": f"sha256:{_sha256_file(manifest_path)}",
    }
    for key in ("name", "version", "repository", "homepage", "source", "digests"):
        if key in payload:
            selected[key] = _redact(payload[key])
    return selected


def _unsupported_fields(payload: dict[str, Any], supported: set[str]) -> list[str]:
    return sorted(key for key in payload if key not in supported)


def _parse_skill_frontmatter(path: Path) -> tuple[dict[str, Any], list[str]]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, ["missing YAML frontmatter"]
    end = text.find("\n---", 4)
    if end == -1:
        return {}, ["unterminated YAML frontmatter"]
    frontmatter: dict[str, Any] = {}
    warnings: list[str] = []
    for raw_line in text[4:end].splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            warnings.append(f"unsupported frontmatter line: {line}")
            continue
        key, value = line.split(":", 1)
        frontmatter[key.strip()] = value.strip().strip("\"'")
    return frontmatter, warnings


def _validate_codex(plugin_root: Path) -> dict[str, Any]:
    path = plugin_root / ".codex-plugin" / "plugin.json"
    manifest = _load_json_file(path, "Codex plugin manifest")
    errors: list[str] = []
    warnings: list[str] = []
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        errors.append("Codex manifest version must be strict semver")
    for field in ("skills", "mcpServers"):
        value = manifest.get(field)
        if not isinstance(value, str) or not _is_relative_path(value):
            errors.append(f"Codex field {field} must be a relative path")
        elif not _relative_path_exists(plugin_root, value):
            errors.append(f"Codex field {field} points to missing path {value}")
    mcp_path = plugin_root / str(manifest.get("mcpServers", ".mcp.json"))
    mcp = _load_json_file(mcp_path, "Codex MCP config") if mcp_path.is_file() else {}
    mcp_servers = mcp.get("mcpServers") if isinstance(mcp.get("mcpServers"), dict) else {}
    env_fields = sorted(
        {
            key
            for server in mcp_servers.values()
            if isinstance(server, dict)
            for key in (server.get("env") or {})
            if isinstance(key, str)
        }
    )
    if "PALACEOFTRUTH_API_KEY" not in env_fields:
        warnings.append("PALACEOFTRUTH_API_KEY must be supplied by the agent runtime environment")
    bin_fields = sorted(
        str(server.get("command"))
        for server in mcp_servers.values()
        if isinstance(server, dict) and server.get("command")
    )
    return {
        "target": "codex-plugin",
        "present": True,
        "supported": not errors,
        "errors": errors,
        "warnings": warnings,
        "unsupported_fields": _unsupported_fields(manifest, CODEX_FIELDS),
        "relative_paths": {"skills": manifest.get("skills"), "mcpServers": manifest.get("mcpServers")},
        "env_declarations": env_fields,
        "bin_declarations": bin_fields,
        "source_metadata": _source_metadata(manifest, path),
    }


def _validate_claude(plugin_root: Path) -> dict[str, Any]:
    path = plugin_root / ".claude-plugin" / "plugin.json"
    manifest = _load_json_file(path, "Claude-style plugin manifest")
    errors: list[str] = []
    warnings: list[str] = []
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        errors.append("Claude-style manifest version must be strict semver")
    skills = manifest.get("skills")
    if not isinstance(skills, list) or not skills:
        errors.append("Claude-style manifest skills must be a non-empty list")
        skill_paths: list[str] = []
    else:
        skill_paths = [item for item in skills if isinstance(item, str)]
        for value in skill_paths:
            if not _is_relative_path(value):
                errors.append(f"Claude-style skill path must be relative: {value}")
            elif not _relative_path_exists(plugin_root, value):
                errors.append(f"Claude-style skill path points to missing path {value}")
    marketplace_path = plugin_root / ".claude-plugin" / "marketplace.json"
    marketplace = _load_json_file(marketplace_path, "Claude-style marketplace")
    plugins = marketplace.get("plugins") if isinstance(marketplace.get("plugins"), list) else []
    if not plugins:
        warnings.append("Claude-style marketplace has no plugin entries")
    return {
        "target": "claude-style-plugin",
        "present": True,
        "supported": not errors,
        "errors": errors,
        "warnings": warnings,
        "unsupported_fields": _unsupported_fields(manifest, CLAUDE_FIELDS),
        "relative_paths": {"skills": skill_paths},
        "marketplace_plugins": plugins,
        "source_metadata": _source_metadata(manifest, path),
    }


def _validate_clawhub_skills(plugin_root: Path) -> dict[str, Any]:
    skills_root = plugin_root / "skills"
    errors: list[str] = []
    warnings: list[str] = []
    skills: list[dict[str, Any]] = []
    for path in sorted(skills_root.glob("*/SKILL.md")):
        metadata, skill_warnings = _parse_skill_frontmatter(path)
        unsupported = _unsupported_fields(metadata, SKILL_FRONTMATTER_FIELDS)
        if not isinstance(metadata.get("name"), str) or not metadata.get("name"):
            errors.append(f"{path.relative_to(plugin_root)} missing frontmatter name")
        if not isinstance(metadata.get("description"), str) or not metadata.get("description"):
            errors.append(f"{path.relative_to(plugin_root)} missing frontmatter description")
        skills.append(
            {
                "path": str(path.relative_to(plugin_root)),
                "name": metadata.get("name"),
                "description": metadata.get("description"),
                "unsupported_fields": unsupported,
                "warnings": skill_warnings,
                "source_metadata": _redact(
                    {
                        key: metadata[key]
                        for key in ("version", "repository", "homepage", "source", "digest")
                        if key in metadata
                    }
                ),
            }
        )
        warnings.extend(f"{path.relative_to(plugin_root)}: {item}" for item in skill_warnings)
        if unsupported:
            warnings.append(
                f"{path.relative_to(plugin_root)} has unsupported ClawHub metadata fields: "
                + ", ".join(unsupported)
            )
    if not skills:
        errors.append("no ClawHub-compatible skills found under skills/*/SKILL.md")
    return {
        "target": "clawhub-skill",
        "present": bool(skills),
        "supported": not errors,
        "errors": errors,
        "warnings": warnings,
        "skills": skills,
    }


def _validate_openclaw(plugin_root: Path) -> dict[str, Any]:
    path = plugin_root / "openclaw.plugin.json"
    if not path.exists():
        return {
            "target": "openclaw-plugin",
            "present": False,
            "supported": False,
            "status": "not-present",
            "errors": [],
            "warnings": [
                "native OpenClaw plugin registration is intentionally deferred until runtime capability APIs are required"
            ],
            "unsupported_fields": [],
        }
    manifest = _load_json_file(path, "OpenClaw plugin manifest")
    errors: list[str] = []
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        errors.append("OpenClaw manifest version must be strict semver")
    unsupported = _unsupported_fields(manifest, OPENCLAW_FIELDS)
    return {
        "target": "openclaw-plugin",
        "present": True,
        "supported": not errors,
        "status": "experimental-metadata-only",
        "errors": errors,
        "warnings": ["OpenClaw metadata is reported only; no native runtime capability registration is performed"],
        "unsupported_fields": unsupported,
        "source_metadata": _source_metadata(manifest, path),
    }


def _bundle_size(plugin_root: Path, max_bundle_bytes: int) -> dict[str, Any]:
    files = [path for path in plugin_root.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    largest = sorted(
        (
            {"path": str(path.relative_to(plugin_root)), "bytes": path.stat().st_size}
            for path in files
        ),
        key=lambda item: int(item["bytes"]),
        reverse=True,
    )[:5]
    return {
        "total_bytes": total_bytes,
        "file_count": len(files),
        "max_bundle_bytes": max_bundle_bytes,
        "concerns": (
            [f"bundle size {total_bytes} exceeds {max_bundle_bytes} bytes"]
            if total_bytes > max_bundle_bytes
            else []
        ),
        "largest_files": largest,
    }


def build_compatibility_report(plugin_root: Path, max_bundle_bytes: int = MAX_BUNDLE_BYTES) -> dict[str, Any]:
    plugin_root = plugin_root.resolve()
    if not plugin_root.is_dir():
        raise CompatibilityError(f"plugin root not found at {plugin_root}")
    targets = [
        _validate_codex(plugin_root),
        _validate_claude(plugin_root),
        _validate_clawhub_skills(plugin_root),
        _validate_openclaw(plugin_root),
    ]
    all_errors = [error for target in targets for error in target.get("errors", [])]
    unsupported = {
        str(target["target"]): target.get("unsupported_fields", [])
        for target in targets
        if target.get("unsupported_fields")
    }
    return _redact(
        {
            "report": "palace-agent-plugin-compatibility",
            "dry_run": True,
            "mutating": False,
            "plugin_root": str(plugin_root),
            "compatibility_target": {
                "primary": ["codex-plugin", "clawhub-skill"],
                "secondary": ["claude-style-plugin"],
                "deferred": ["openclaw-plugin-native-runtime"],
                "rationale": (
                    "Codex bundle plus ClawHub skill compatibility comes first; native "
                    "OpenClaw plugin support is deferred until runtime capability registration is required."
                ),
            },
            "targets": targets,
            "bundle": _bundle_size(plugin_root, max_bundle_bytes),
            "unsupported_fields": unsupported,
            "status": "ok" if not all_errors else "error",
        }
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Palace agent plugin compatibility for Codex, Claude-style, ClawHub, and future OpenClaw manifests."
    )
    parser.add_argument("--plugin-root", default=str(DEFAULT_PLUGIN_ROOT))
    parser.add_argument("--max-bundle-bytes", type=int, default=MAX_BUNDLE_BYTES)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Palace agent plugin compatibility: {report['status']}",
        f"Plugin root: {report['plugin_root']}",
        f"Primary targets: {', '.join(report['compatibility_target']['primary'])}",
    ]
    for target in report["targets"]:
        status = "present" if target.get("present") else "missing"
        supported = "supported" if target.get("supported") else "not-supported"
        lines.append(f"- {target['target']}: {status}, {supported}")
        for error in target.get("errors", []):
            lines.append(f"  error: {error}")
        for warning in target.get("warnings", []):
            lines.append(f"  warning: {warning}")
    for concern in report["bundle"]["concerns"]:
        lines.append(f"bundle: {concern}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        report = build_compatibility_report(
            Path(args.plugin_root).expanduser(),
            max_bundle_bytes=args.max_bundle_bytes,
        )
    except CompatibilityError as exc:
        parser.error(str(exc))
    if args.format == "text":
        print(render_text(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
