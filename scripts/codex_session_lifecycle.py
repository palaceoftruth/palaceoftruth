#!/usr/bin/env python3
"""Print dry-run Palace MCP payloads for a Codex session lifecycle.

The script does not connect to Palace. It emits copyable tool payloads for
session startup recall, pre-compaction checkpoint preview, and post-run
write-back using safe placeholders instead of raw transcript text or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_AGENT_SCOPE_KEY = "codex"
DEFAULT_QUERY = "What durable context should Codex know before working in this repository?"


def derive_workspace_key(cwd: str) -> str:
    name = Path(cwd).resolve().name.lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    return normalized or "workspace"


def lifecycle_payloads(
    *,
    cwd: str,
    workspace_key: str | None,
    session_key: str | None,
    agent_scope_key: str,
    query: str,
) -> dict[str, Any]:
    resolved_workspace_key = workspace_key or derive_workspace_key(cwd)
    checkpoint_scope_type = "session" if session_key else "workspace"
    checkpoint_scope_key = session_key or resolved_workspace_key
    return {
        "contract": "codex-palace-session-lifecycle",
        "dry_run": True,
        "workspace_key": resolved_workspace_key,
        "privacy": {
            "raw_secret_output": False,
            "raw_transcript_output": False,
            "write_tools_need_operator_review": True,
        },
        "steps": [
            {
                "phase": "start",
                "tool": "whoami",
                "arguments": {},
                "purpose": "Confirm the MCP server is authenticated to the intended tenant.",
            },
            {
                "phase": "start",
                "tool": "get_wakeup_context",
                "arguments": {
                    "agent_scope_key": agent_scope_key,
                    "workspace_scope_keys": [resolved_workspace_key],
                    "session_scope_key": session_key,
                    "include_tenant_shared": True,
                    "memory_limit_per_scope": 5,
                    "checkpoint_limit_per_scope": 3,
                },
                "purpose": "Load compact session-start context, recent memory pointers, checkpoint pointers, and readiness warnings.",
            },
            {
                "phase": "start",
                "tool": "retrieve_agent_memory",
                "arguments": {
                    "query": query,
                    "agent_scope_key": agent_scope_key,
                    "workspace_scope_keys": [resolved_workspace_key],
                    "session_scope_key": session_key,
                    "include_tenant_shared": True,
                    "include_broad_corpus": False,
                    "display_limit": 5,
                    "context_budget_chars": 6000,
                },
                "purpose": "Recall durable agent and workspace context without broad corpus fallback.",
            },
            {
                "phase": "handoff_or_precompact",
                "tool": "palace_checkpoint",
                "arguments": {
                    "title": "Codex session checkpoint",
                    "summary": "<concise state, decisions, and next steps>",
                    "evidence_snippets": [
                        "<changed files or validated command names only>",
                        "<known caveat or blocker, if any>",
                    ],
                    "scope_type": checkpoint_scope_type,
                    "scope_key": checkpoint_scope_key,
                    "checkpoint_kind": "precompact",
                    "tags": ["codex-lifecycle", resolved_workspace_key],
                    "relationship_policy": "immediate",
                    "idempotency_key": "codex-checkpoint:<stable-thread-or-run-id>",
                    "dry_run": True,
                },
                "purpose": "Preview the resumable checkpoint envelope before writing handoff or compaction state.",
            },
            {
                "phase": "post_run",
                "tool": "palace_remember",
                "arguments": {
                    "title": "Codex durable project learning",
                    "body": "<non-sensitive operational learning for future runs>",
                    "source": "codex",
                    "summary": "<one-sentence learning summary>",
                    "tags": ["codex", "codex-lifecycle", resolved_workspace_key],
                    "scope_type": "workspace",
                    "scope_key": resolved_workspace_key,
                    "idempotency_key": "codex-learning:<stable-run-or-commit-id>",
                    "created_by_role": "agent",
                    "metadata": {
                        "codex_lifecycle": {
                            "schema_version": 1,
                            "session_key": session_key,
                            "raw_secret_output": False,
                            "raw_transcript_output": False,
                        }
                    },
                    "relationship_policy": "immediate",
                },
                "purpose": "Write concise durable learning after substantial work; omit for trivial runs and defer locally on MCP failure.",
            },
        ],
        "local_fallback": {
            "when": [
                "Palace MCP is unavailable or unauthenticated.",
                "Scoped retrieval drifts outside the requested agent/workspace/session scope.",
                "Exact local source-line citations are required.",
            ],
            "path": "$CODEX_HOME/memories",
        },
    }


def format_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Codex Palace Session Lifecycle Payloads",
        "",
        f"- Workspace key: `{payload['workspace_key']}`",
        f"- Dry run: `{str(payload['dry_run']).lower()}`",
        "- No raw secrets or transcript bodies are included.",
        "",
    ]
    for step in payload["steps"]:
        lines.extend(
            [
                f"## {step['phase']}: {step['tool']}",
                "",
                step["purpose"],
                "",
                "```json",
                json.dumps(step["arguments"], indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## Local Fallback",
            "",
            "Use local Codex memory files only when Palace MCP is unavailable, "
            "scope routing is suspect, or exact source-line citations are required.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--workspace-key", default=None)
    parser.add_argument("--session-key", default=None)
    parser.add_argument("--agent-scope-key", default=DEFAULT_AGENT_SCOPE_KEY)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = lifecycle_payloads(
        cwd=args.cwd,
        workspace_key=args.workspace_key,
        session_key=args.session_key,
        agent_scope_key=args.agent_scope_key,
        query=args.query,
    )
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
