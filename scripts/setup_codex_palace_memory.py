#!/usr/bin/env python3
"""Prepare and verify Codex Palace MCP configuration.

The default path is a non-mutating dry run: validate local repo shape, print a
redacted Codex MCP config snippet, and show the exact live-smoke command. Pass
--live-smoke to launch the stdio adapter and write exactly one scoped memory
through the existing compatibility smoke.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
MCP_ADAPTER = BACKEND_ROOT / "scripts" / "palaceoftruth_mcp.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_agent_memory_compatibility.py"
SKILLPACK_ROOT = REPO_ROOT / "third_party_plugins" / "agent_clients" / "palaceoftruth-memory"
CODEX_PLUGIN_MANIFEST = SKILLPACK_ROOT / ".codex-plugin" / "plugin.json"
CODEX_MCP_CONFIG = SKILLPACK_ROOT / ".mcp.json"
CODEX_SKILL = SKILLPACK_ROOT / "skills" / "palaceoftruth-codex-memory" / "SKILL.md"
HERMES_PLUGIN_ROOT = REPO_ROOT / "third_party_plugins" / "hermes" / "memory" / "palaceoftruth"
HERMES_PLUGIN_MANIFEST = HERMES_PLUGIN_ROOT / "plugin.yaml"
DEFAULT_API_BASE_URL = "https://api.palaceoftruth.test"
DEFAULT_SCOPE_TYPE = "agent"
DEFAULT_SCOPE_KEY = "codex"
REDACTED_SECRET = "<redacted: set PALACEOFTRUTH_API_KEY in your Codex runtime environment>"
PALACE_PLUGIN_NAME = "palaceoftruth-memory"
PALACE_MCP_SERVER_NAME = "palaceoftruth-memory"


class SetupError(RuntimeError):
    """Raised when the setup contract is invalid before any live smoke starts."""


def validate_api_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SetupError("--api-base-url must be an http(s) URL")
    return value.rstrip("/")


def validate_scope(scope_type: str, scope_key: str | None) -> None:
    if scope_type == "tenant_shared":
        if scope_key:
            raise SetupError("--scope-key must be omitted when --scope-type is tenant_shared")
        return
    if not scope_key:
        raise SetupError(f"--scope-key is required when --scope-type is {scope_type}")


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise SetupError(f"{label} not found at {path}")


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    require_file(path, label)
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{label} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SetupError(f"{label} at {path} must be a JSON object")
    return payload


def load_simple_yaml_values(path: Path, label: str) -> dict[str, str]:
    require_file(path, label)
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_file_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    hashes: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        hashes[str(path.relative_to(root))] = sha256_file(path)
    return hashes


def discover_installed_codex_plugin(codex_home: Path) -> Path | None:
    search_roots = [
        codex_home / "plugins" / "cache",
        codex_home / "plugins" / "installed",
        codex_home / "plugins",
    ]
    seen: set[Path] = set()
    for root in search_roots:
        if not root.is_dir() or root in seen:
            continue
        seen.add(root)
        for manifest_path in sorted(root.rglob(".codex-plugin/plugin.json")):
            try:
                manifest = load_json_file(manifest_path, "installed Codex plugin manifest")
            except SetupError:
                continue
            if manifest.get("name") == PALACE_PLUGIN_NAME:
                return manifest_path.parents[1]
    return None


def normalize_mcp_server(config: dict[str, Any]) -> dict[str, Any] | None:
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None
    server = servers.get(PALACE_MCP_SERVER_NAME)
    return server if isinstance(server, dict) else None


def read_codex_config(codex_home: Path) -> dict[str, Any]:
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as handle:
            parsed = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"Codex config at {config_path} is not valid TOML: {exc}") from exc
    return parsed


def compare_mcp_config(desired_root: Path, installed_root: Path | None) -> list[str]:
    desired = normalize_mcp_server(load_json_file(desired_root / ".mcp.json", "Codex MCP config"))
    if desired is None:
        raise SetupError("repo Codex MCP config is missing the palaceoftruth-memory server")
    if installed_root is None:
        return ["installed Codex plugin is missing"]
    installed_config_path = installed_root / ".mcp.json"
    if not installed_config_path.is_file():
        return [f"installed MCP config is missing at {installed_config_path}"]
    installed = normalize_mcp_server(load_json_file(installed_config_path, "installed Codex MCP config"))
    if installed is None:
        return ["installed MCP config is missing the palaceoftruth-memory server"]

    drift: list[str] = []
    for key in ("command", "args", "cwd"):
        if installed.get(key) != desired.get(key):
            drift.append(f"MCP {key} differs from repo manifest")
    desired_env = desired.get("env") if isinstance(desired.get("env"), dict) else {}
    installed_env = installed.get("env") if isinstance(installed.get("env"), dict) else {}
    if installed_env.get("PALACEOFTRUTH_API_BASE_URL") != desired_env.get("PALACEOFTRUTH_API_BASE_URL"):
        drift.append("MCP PALACEOFTRUTH_API_BASE_URL differs from repo manifest")
    if "PALACEOFTRUTH_API_KEY" in installed_env:
        drift.append("installed MCP config contains PALACEOFTRUTH_API_KEY; use runtime env or secret store instead")
    return drift


def compare_skillpack(desired_root: Path, installed_root: Path | None) -> dict[str, Any]:
    desired = relative_file_hashes(desired_root / "skills")
    installed = relative_file_hashes(installed_root / "skills") if installed_root is not None else {}
    missing = sorted(set(desired) - set(installed))
    extra = sorted(set(installed) - set(desired))
    changed = sorted(path for path in set(desired) & set(installed) if desired[path] != installed[path])
    return {
        "missing": missing,
        "extra": extra,
        "changed": changed,
        "drifted": bool(missing or extra or changed),
    }


def marketplace_source(repo_root: Path) -> dict[str, Any]:
    marketplace_path = repo_root / ".agents" / "plugins" / "marketplace.json"
    marketplace = load_json_file(marketplace_path, "repo plugin marketplace")
    plugins = marketplace.get("plugins") if isinstance(marketplace.get("plugins"), list) else []
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("name") == PALACE_PLUGIN_NAME:
            source = plugin.get("source") if isinstance(plugin.get("source"), dict) else {}
            return {
                "registered": True,
                "path": source.get("path"),
                "source": source.get("source"),
                "policy": plugin.get("policy") if isinstance(plugin.get("policy"), dict) else {},
            }
    return {"registered": False, "path": None, "source": None, "policy": {}}


def codex_enabled_state(codex_home: Path) -> bool | None:
    config = read_codex_config(codex_home)
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    server = servers.get(PALACE_MCP_SERVER_NAME)
    if not isinstance(server, dict):
        return None
    enabled = server.get("enabled")
    if enabled is None:
        return True
    return bool(enabled)


def build_plugin_check_report(args: argparse.Namespace) -> dict[str, Any]:
    require_file(CODEX_PLUGIN_MANIFEST, "Codex plugin manifest")
    require_file(CODEX_MCP_CONFIG, "Codex MCP config")
    require_file(HERMES_PLUGIN_MANIFEST, "Hermes plugin manifest")
    require_file(CODEX_SKILL, "Codex Palace memory skillpack")

    codex_home = Path(args.codex_home).expanduser()
    desired_manifest = load_json_file(CODEX_PLUGIN_MANIFEST, "Codex plugin manifest")
    desired_version = str(desired_manifest.get("version", ""))
    installed_root = (
        Path(args.installed_plugin_path).expanduser()
        if args.installed_plugin_path
        else discover_installed_codex_plugin(codex_home)
    )
    installed_manifest_path = installed_root / ".codex-plugin" / "plugin.json" if installed_root else None
    installed_manifest = (
        load_json_file(installed_manifest_path, "installed Codex plugin manifest")
        if installed_manifest_path and installed_manifest_path.is_file()
        else None
    )
    installed_version = str(installed_manifest.get("version", "")) if installed_manifest else None
    mcp_drift = compare_mcp_config(SKILLPACK_ROOT, installed_root)
    skillpack_drift = compare_skillpack(SKILLPACK_ROOT, installed_root)
    marketplace = marketplace_source(REPO_ROOT)
    auth_env_present = bool(os.getenv(args.api_key_env))
    version_drift = installed_version is not None and installed_version != desired_version
    installed = installed_root is not None and installed_manifest is not None
    restart_required = bool(installed and (version_drift or mcp_drift or skillpack_drift["drifted"]))
    enabled = codex_enabled_state(codex_home)

    if not installed:
        codex_status = "missing"
        update_state = "install-available"
        next_action = "Install the repo Codex plugin package, then rerun this read-only check."
    elif version_drift or mcp_drift or skillpack_drift["drifted"]:
        codex_status = "drifted"
        update_state = "update-available"
        next_action = "Refresh the installed Codex plugin from the repo package and restart Codex."
    elif not auth_env_present:
        codex_status = "auth-missing"
        update_state = "current"
        next_action = f"Set {args.api_key_env} in the Codex runtime environment before live MCP use."
    else:
        codex_status = "ok"
        update_state = "current"
        next_action = "Installed Codex plugin matches the repo package; restart only if Codex was already running."

    hermes_manifest = load_simple_yaml_values(HERMES_PLUGIN_MANIFEST, "Hermes plugin manifest")
    return {
        "report": "palace-plugin-install-check",
        "dry_run": True,
        "mutating": False,
        "repo_root": str(REPO_ROOT),
        "codex_home": str(codex_home),
        "codex": {
            "package_surface": "codex",
            "manifest_path": str(CODEX_PLUGIN_MANIFEST),
            "manifest_version": desired_version,
            "installed": installed,
            "installed_path": str(installed_root) if installed_root else None,
            "installed_version": installed_version,
            "source": "explicit-path" if args.installed_plugin_path else ("codex-cache" if installed else None),
            "marketplace": marketplace,
            "enabled": enabled,
            "api_key_env": args.api_key_env,
            "auth_env_present": auth_env_present,
            "mcp_command_drift": mcp_drift,
            "skillpack_drift": skillpack_drift,
            "restart_required": restart_required,
            "status": codex_status,
            "update_state": update_state,
            "next_action": next_action,
        },
        "hermes": {
            "package_surface": "hermes",
            "manifest_path": str(HERMES_PLUGIN_MANIFEST),
            "manifest_version": hermes_manifest.get("version"),
            "source": "repo-package",
            "installed": False,
            "installed_path": None,
            "status": "separate-package-surface",
            "next_action": (
                "Treat Hermes as a separate release artifact; compare deployment pins "
                "against the Hermes plugin package metadata, not the Codex plugin version."
            ),
        },
    }


def redacted_env(api_base_url: str) -> dict[str, str]:
    return {
        "PALACEOFTRUTH_API_BASE_URL": api_base_url,
        "PALACEOFTRUTH_API_KEY": REDACTED_SECRET,
    }


def codex_config_snippet(api_base_url: str) -> str:
    backend = str(BACKEND_ROOT)
    return "\n".join(
        [
            "[mcp_servers.palaceoftruth-memory]",
            'command = "uv"',
            "args = [",
            '  "--directory",',
            f'  "{backend}",',
            '  "run",',
            '  "python",',
            '  "scripts/palaceoftruth_mcp.py",',
            "]",
            "enabled = true",
            "",
            "[mcp_servers.palaceoftruth-memory.env]",
            f'PALACEOFTRUTH_API_BASE_URL = "{api_base_url}"',
            f'PALACEOFTRUTH_API_KEY = "{REDACTED_SECRET}"',
        ]
    )


def smoke_command(args: argparse.Namespace, *, include_secret: bool) -> list[str]:
    command = [
        sys.executable,
        str(SMOKE_SCRIPT),
        "--api-base-url",
        args.api_base_url,
        "mcp-stdio",
        "--run-id",
        args.run_id,
        "--scope-type",
        args.scope_type,
        "--relationship-policy",
        "immediate",
        "--skip-backfill",
        "--active-skill",
        "palaceoftruth-codex-memory",
        "--stdio-command",
        "uv",
        "--stdio-cwd",
        str(REPO_ROOT),
    ]
    if args.scope_key is not None:
        command.extend(["--scope-key", args.scope_key])
    for item in (
        "--directory",
        str(BACKEND_ROOT),
        "run",
        "python",
        "scripts/palaceoftruth_mcp.py",
    ):
        command.extend(["--stdio-arg", item])
    return command


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    args.api_base_url = validate_api_base_url(args.api_base_url)
    validate_scope(args.scope_type, args.scope_key)
    require_file(MCP_ADAPTER, "MCP adapter")
    require_file(SMOKE_SCRIPT, "compatibility smoke script")
    require_file(CODEX_SKILL, "Codex Palace memory skillpack")

    return {
        "report": "codex-palace-mcp-setup",
        "dry_run": not args.live_smoke,
        "mutating": bool(args.live_smoke),
        "repo_root": str(REPO_ROOT),
        "backend_root": str(BACKEND_ROOT),
        "adapter": str(MCP_ADAPTER),
        "skillpack": str(CODEX_SKILL),
        "scope": {"type": args.scope_type, **({"key": args.scope_key} if args.scope_key else {})},
        "api_base_url": args.api_base_url,
        "uv_available": shutil.which("uv") is not None,
        "codex_config_toml": codex_config_snippet(args.api_base_url),
        "redacted_env": redacted_env(args.api_base_url),
        "live_smoke_command": smoke_command(args, include_secret=False),
        "live_smoke_contract": {
            "writes_scoped_memories": 1,
            "relationship_policy": "immediate",
            "backfill": "disabled",
            "delete_retry_admin_operations": False,
            "raw_secret_output": False,
        },
        "next_step": (
            "Dry run complete. Set PALACEOFTRUTH_API_KEY in the runtime environment "
            "and rerun with --live-smoke to verify the adapter end to end."
        ),
    }


def format_text(report: dict[str, Any]) -> str:
    if report.get("report") == "palace-plugin-install-check":
        codex = report["codex"]
        hermes = report["hermes"]
        drift = codex["mcp_command_drift"] or ["none"]
        skillpack = codex["skillpack_drift"]
        lines = [
            "Palace plugin install check",
            "Mode: read-only dry-run",
            f"Codex status: {codex['status']}",
            f"Codex installed: {str(codex['installed']).lower()}",
            f"Codex manifest version: {codex['manifest_version']}",
            f"Codex installed version: {codex['installed_version'] or 'n/a'}",
            f"Codex installed path: {codex['installed_path'] or 'n/a'}",
            f"Codex marketplace registered: {str(codex['marketplace']['registered']).lower()}",
            f"Codex enabled: {'unknown' if codex['enabled'] is None else str(codex['enabled']).lower()}",
            f"{codex['api_key_env']} present: {str(codex['auth_env_present']).lower()}",
            f"Restart required: {str(codex['restart_required']).lower()}",
            "MCP drift:",
            *[f"- {item}" for item in drift],
            "Skillpack drift:",
            f"- missing: {len(skillpack['missing'])}",
            f"- changed: {len(skillpack['changed'])}",
            f"- extra: {len(skillpack['extra'])}",
            f"Hermes status: {hermes['status']} version {hermes['manifest_version']}",
            "",
            f"Next action: {codex['next_action']}",
            f"Hermes note: {hermes['next_action']}",
        ]
        return "\n".join(lines) + "\n"

    lines = [
        "Codex Palace MCP setup",
        f"Mode: {'live-smoke' if report['mutating'] else 'dry-run'}",
        f"API: {report['api_base_url']}",
        f"Scope: {report['scope']}",
        f"Adapter: {report['adapter']}",
        f"Skillpack: {report['skillpack']}",
        f"uv available: {str(report['uv_available']).lower()}",
        "",
        "Codex config snippet:",
        report["codex_config_toml"],
        "",
        "Live smoke command preview:",
        " ".join(report["live_smoke_command"]),
        "",
        "Live smoke contract:",
        json.dumps(report["live_smoke_contract"], indent=2, sort_keys=True),
        "",
        report["next_step"],
    ]
    return "\n".join(lines) + "\n"


def run_live_smoke(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.getenv(args.api_key_env) or ""
    if not api_key:
        raise SetupError(f"--api-key or {args.api_key_env} is required with --live-smoke")
    env = dict(os.environ)
    env["PALACEOFTRUTH_API_KEY"] = api_key
    completed = subprocess.run(smoke_command(args, include_secret=True), check=False, env=env)
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("PALACEOFTRUTH_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    parser.add_argument("--api-key", default=None, help="Tenant API key. Prefer the env var instead.")
    parser.add_argument("--api-key-env", default="PALACEOFTRUTH_API_KEY")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report installed Codex/Hermes Palace plugin state without mutating local profiles.",
    )
    parser.add_argument("--codex-home", default=os.getenv("CODEX_HOME", "~/.codex"))
    parser.add_argument(
        "--installed-plugin-path",
        default=None,
        help="Optional installed Codex plugin root to compare instead of auto-discovering from CODEX_HOME.",
    )
    parser.add_argument("--run-id", default="codex-palace-setup")
    parser.add_argument(
        "--scope-type",
        choices=["session", "agent", "workspace", "tenant_shared"],
        default=DEFAULT_SCOPE_TYPE,
    )
    parser.add_argument("--scope-key", default=DEFAULT_SCOPE_KEY)
    parser.add_argument("--live-smoke", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.check and args.live_smoke:
            raise SetupError("--check cannot be combined with --live-smoke")
        report = build_plugin_check_report(args) if args.check else build_report(args)
        output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.format == "json" else format_text(report)
        print(output, end="")
        if args.live_smoke:
            return run_live_smoke(args)
        return 0
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
