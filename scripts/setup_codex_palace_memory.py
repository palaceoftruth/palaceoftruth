#!/usr/bin/env python3
"""Prepare and verify Codex Palace MCP configuration.

The default path is a non-mutating dry run: validate local repo shape, print a
redacted Codex MCP config snippet, and show the exact live-smoke command. Pass
--live-smoke to launch the stdio adapter and write exactly one scoped memory
through the existing compatibility smoke.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
MCP_ADAPTER = BACKEND_ROOT / "scripts" / "palaceoftruth_mcp.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_agent_memory_compatibility.py"
SKILLPACK_ROOT = REPO_ROOT / "plugins" / "palaceoftruth-memory"
CODEX_SKILL = SKILLPACK_ROOT / "skills" / "palaceoftruth-codex-memory" / "SKILL.md"
DEFAULT_API_BASE_URL = "https://api.palaceoftruth.test"
DEFAULT_SCOPE_TYPE = "agent"
DEFAULT_SCOPE_KEY = "codex"
REDACTED_SECRET = "<redacted: set PALACEOFTRUTH_API_KEY in your Codex runtime environment>"


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
        report = build_report(args)
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
