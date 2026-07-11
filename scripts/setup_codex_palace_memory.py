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
REDACTED_SECRET = "<redacted: set this value in your Codex runtime environment>"
PALACE_PLUGIN_NAME = "palaceoftruth-memory"
PALACE_MCP_SERVER_NAME = "palaceoftruth-memory"
OAUTH_CLIENT_SECRET_ENV = "PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET"
OAUTH_TOKEN_URL_ENV = "PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL"
OAUTH_RESOURCE_ENV = "PALACEOFTRUTH_MCP_OAUTH_RESOURCE"
OAUTH_CLIENT_KEY_ENV = "PALACEOFTRUTH_MCP_CLIENT_KEY"
OAUTH_CLIENT_SCOPES_ENV = "PALACEOFTRUTH_MCP_CLIENT_SCOPES"
BEARER_TOKEN_ENV = "PALACEOFTRUTH_MCP_BEARER_TOKEN"
DEFAULT_SCOPE_TYPE_ENV = "PALACEOFTRUTH_DEFAULT_SCOPE_TYPE"
DEFAULT_SCOPE_KEY_ENV = "PALACEOFTRUTH_DEFAULT_SCOPE_KEY"
AUTH_ENV_ALIASES = {
    "bearer_token": (BEARER_TOKEN_ENV, "SECONDBRAIN_MCP_BEARER_TOKEN"),
    "oauth_client_secret": (OAUTH_CLIENT_SECRET_ENV, "SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET"),
    "token_url": (OAUTH_TOKEN_URL_ENV, "SECONDBRAIN_MCP_OAUTH_TOKEN_URL"),
    "resource": (OAUTH_RESOURCE_ENV, "SECONDBRAIN_MCP_OAUTH_RESOURCE"),
    "client_key": (OAUTH_CLIENT_KEY_ENV, "SECONDBRAIN_MCP_CLIENT_KEY"),
    "client_scopes": (OAUTH_CLIENT_SCOPES_ENV, "SECONDBRAIN_MCP_CLIENT_SCOPES"),
    "default_scope_type": (DEFAULT_SCOPE_TYPE_ENV, "SECONDBRAIN_DEFAULT_SCOPE_TYPE"),
    "default_scope_key": (DEFAULT_SCOPE_KEY_ENV, "SECONDBRAIN_DEFAULT_SCOPE_KEY"),
}


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


def codex_runtime_mcp_settings(codex_home: Path) -> dict[str, Any]:
    """Read non-secret runtime MCP settings to make local-profile drift visible."""
    config = read_codex_config(codex_home)
    servers = config.get("mcp_servers")
    server = servers.get(PALACE_MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return {"configured": False, "api_base_url": None, "default_scope": None}
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    return {
        "configured": True,
        "api_base_url": env.get("PALACEOFTRUTH_API_BASE_URL"),
        "default_scope": {
            "type": env.get(DEFAULT_SCOPE_TYPE_ENV),
            "key": env.get(DEFAULT_SCOPE_KEY_ENV),
        },
        "client_key": env.get(OAUTH_CLIENT_KEY_ENV),
    }


def auth_configuration(args: argparse.Namespace) -> dict[str, Any]:
    """Report auth precedence without exposing runtime credential values."""
    def configured_value(names: tuple[str, ...]) -> str | None:
        for name in names:
            value = os.getenv(name)
            if value and value.strip():
                return value.strip()
        return None

    configured = {
        "bearer_token": bool(configured_value(AUTH_ENV_ALIASES["bearer_token"])),
        "oauth_client_credentials": bool(configured_value(AUTH_ENV_ALIASES["oauth_client_secret"])),
        "legacy_api_key": bool(configured_value((args.api_key_env, "SECONDBRAIN_API_KEY", "API_KEY"))),
    }
    if configured["bearer_token"]:
        mode = "static_bearer"
    elif configured["oauth_client_credentials"]:
        mode = "oauth_client_credentials"
    elif configured["legacy_api_key"]:
        mode = "legacy_api_key"
    else:
        mode = "missing"
    return {
        "mode": mode,
        "configured": configured,
        "oauth_preferred": True,
        "legacy_fallback_retained": configured["legacy_api_key"],
        "token_url": configured_value(AUTH_ENV_ALIASES["token_url"]) or f"{args.api_base_url}/api/v1/memory/mcp/oauth/token",
        "resource": configured_value(AUTH_ENV_ALIASES["resource"]) or f"{args.api_base_url}/api/v1",
        "client_key": configured_value(AUTH_ENV_ALIASES["client_key"]) or "default",
        "client_scopes": configured_value(AUTH_ENV_ALIASES["client_scopes"]) or "read,write",
        "default_scope": {
            "type": configured_value(AUTH_ENV_ALIASES["default_scope_type"]) or args.scope_type,
            "key": configured_value(AUTH_ENV_ALIASES["default_scope_key"]) or args.scope_key,
        },
    }


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
    auth = auth_configuration(args)
    runtime_settings = codex_runtime_mcp_settings(codex_home)
    runtime_drift: list[str] = []
    runtime_api_base = runtime_settings["api_base_url"]
    if isinstance(runtime_api_base, str) and runtime_api_base.rstrip("/") != args.api_base_url:
        runtime_drift.append("Codex runtime PALACEOFTRUTH_API_BASE_URL differs from requested setup API base")
    version_drift = installed_version is not None and installed_version != desired_version
    installed = installed_root is not None and installed_manifest is not None
    restart_required = bool(installed and (version_drift or mcp_drift or skillpack_drift["drifted"] or runtime_drift))
    enabled = codex_enabled_state(codex_home)

    if not installed:
        codex_status = "missing"
        update_state = "install-available"
        next_action = "Install the repo Codex plugin package, then rerun this read-only check."
    elif version_drift or mcp_drift or skillpack_drift["drifted"]:
        codex_status = "drifted"
        update_state = "update-available"
        next_action = "Refresh the installed Codex plugin from the repo package and restart Codex."
    elif auth["mode"] == "missing":
        codex_status = "auth-missing"
        update_state = "current"
        next_action = (
            f"Set {OAUTH_CLIENT_SECRET_ENV} for OAuth client credentials, or explicitly set "
            f"{args.api_key_env} for staged legacy fallback, before live MCP use."
        )
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
            "auth": auth,
            "runtime_settings": runtime_settings,
            "runtime_drift": runtime_drift,
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
        OAUTH_CLIENT_SECRET_ENV: REDACTED_SECRET,
        OAUTH_TOKEN_URL_ENV: f"{api_base_url}/api/v1/memory/mcp/oauth/token",
        OAUTH_RESOURCE_ENV: f"{api_base_url}/api/v1",
        OAUTH_CLIENT_KEY_ENV: "default",
        OAUTH_CLIENT_SCOPES_ENV: "read,write",
        DEFAULT_SCOPE_TYPE_ENV: DEFAULT_SCOPE_TYPE,
        DEFAULT_SCOPE_KEY_ENV: DEFAULT_SCOPE_KEY,
        "PALACEOFTRUTH_API_KEY": "<redacted: optional legacy fallback>",
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
            f'{OAUTH_CLIENT_SECRET_ENV} = "{REDACTED_SECRET}"',
            f'{OAUTH_TOKEN_URL_ENV} = "{api_base_url}/api/v1/memory/mcp/oauth/token"',
            f'{OAUTH_RESOURCE_ENV} = "{api_base_url}/api/v1"',
            f'{OAUTH_CLIENT_KEY_ENV} = "default"',
            f'{OAUTH_CLIENT_SCOPES_ENV} = "read,write"',
            f'{DEFAULT_SCOPE_TYPE_ENV} = "{DEFAULT_SCOPE_TYPE}"',
            f'{DEFAULT_SCOPE_KEY_ENV} = "{DEFAULT_SCOPE_KEY}"',
            "# Optional staged compatibility fallback; OAuth remains preferred.",
            f'# PALACEOFTRUTH_API_KEY = "{REDACTED_SECRET}"',
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
        "--verify-no-scope-fail-closed",
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
        command.append(f"--stdio-arg={item}")
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
        "adapter_revision": sha256_file(MCP_ADAPTER),
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
        "auth": auth_configuration(args),
        "next_step": (
            f"Dry run complete. Prefer {OAUTH_CLIENT_SECRET_ENV} with the shown token URL/resource "
            f"and rerun with --live-smoke; use {args.api_key_env} only as explicit staged fallback."
        ),
    }


def format_text(report: dict[str, Any]) -> str:
    if report.get("report") == "palace-plugin-install-check":
        codex = report["codex"]
        hermes = report["hermes"]
        drift = codex["mcp_command_drift"] or ["none"]
        runtime_drift = codex["runtime_drift"] or ["none"]
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
            f"Auth mode: {codex['auth']['mode']}",
            f"Restart required: {str(codex['restart_required']).lower()}",
            "MCP drift:",
            *[f"- {item}" for item in drift],
            "Runtime drift:",
            *[f"- {item}" for item in runtime_drift],
            f"Configured runtime API: {codex['runtime_settings']['api_base_url'] or 'n/a'}",
            f"Configured default scope: {codex['runtime_settings']['default_scope'] or 'n/a'}",
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
        f"Adapter revision: {report['adapter_revision']}",
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
    auth = auth_configuration(args)
    if args.api_key:
        auth["mode"] = "legacy_api_key"
    if auth["mode"] == "missing":
        raise SetupError(
            f"{OAUTH_CLIENT_SECRET_ENV} (preferred), {BEARER_TOKEN_ENV}, or --api-key/{args.api_key_env} "
            "is required with --live-smoke"
        )
    env = dict(os.environ)
    if args.api_key:
        env["PALACEOFTRUTH_API_KEY"] = args.api_key
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
