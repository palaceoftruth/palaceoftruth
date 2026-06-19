#!/usr/bin/env python3
"""Print dry-run MCP payloads for governed Agent Organization Memory.

The demo is intentionally non-destructive. It does not connect to Palace,
read secrets, or write memory. It emits copyable MCP tool calls that show how
specialist agents keep private agent scopes while an orchestrator retrieves
only server-authorized scopes and writes only to agent/orchestrator.
"""

from __future__ import annotations

import argparse
import json
from typing import Any


DEFAULT_SPECIALISTS = ("security-agent", "macos-agent", "frontend-agent")
DEFAULT_WORKSPACE = "palaceoftruth"
DEFAULT_ORCHESTRATOR = "orchestrator"


def _clean_agent_key(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("agent keys must not be blank")
    if "/" in cleaned:
        raise ValueError(f"agent keys must not include '/': {value!r}")
    return cleaned


def _write_memory_payload(agent_key: str, workspace_key: str) -> dict[str, Any]:
    return {
        "title": f"{agent_key} demo finding",
        "body": (
            f"{agent_key} found one scoped, non-sensitive finding for "
            f"{workspace_key}. This placeholder should be replaced with a "
            "concise operational learning, not raw secrets or private logs."
        ),
        "source": "agent-organization-memory-demo",
        "summary": f"{agent_key} contributes one private scoped finding.",
        "tags": ["agent-organization-demo", f"workspace-{workspace_key}", f"agent-{agent_key}"],
        "scope_type": "agent",
        "scope_key": agent_key,
        "created_by_role": "agent",
        "relationship_policy": "deferred",
        "metadata": {
            "demo": "agent-organization-memory",
            "workspace_key": workspace_key,
            "private_specialist_scope": True,
        },
    }


def demo_payloads(
    *,
    workspace_key: str,
    orchestrator_key: str,
    specialist_keys: list[str],
    query: str,
    access_reason: str,
) -> dict[str, Any]:
    workspace_key = _clean_agent_key(workspace_key)
    orchestrator_key = _clean_agent_key(orchestrator_key)
    specialist_keys = [_clean_agent_key(key) for key in specialist_keys]
    if orchestrator_key in specialist_keys:
        raise ValueError("orchestrator key must not also be a specialist key")

    specialist_steps = [
        {
            "phase": "specialist_write",
            "agent": specialist_key,
            "tool": "create_memory_entry",
            "arguments": _write_memory_payload(specialist_key, workspace_key),
            "purpose": (
                "Specialist writes only to its own private agent scope. "
                "No cross-agent write delegation is implied."
            ),
        }
        for specialist_key in specialist_keys
    ]
    orchestrator_recall = {
        "phase": "orchestrator_recall",
        "agent": orchestrator_key,
        "tool": "retrieve_agent_memory",
        "arguments": {
            "query": query,
            "agent_scope_key": orchestrator_key,
            "workspace_scope_keys": [workspace_key],
            "include_agent_scope_keys": specialist_keys,
            "include_all_permitted_agent_scopes": False,
            "include_tenant_shared": False,
            "include_broad_corpus": False,
            "display_limit": 6,
            "candidate_limit": 12,
            "context_budget_chars": 6000,
            "access_reason": access_reason,
        },
        "purpose": (
            "Orchestrator asks for selected specialist scopes. The server "
            "authorizes or denies each same-tenant scope and reports sanitized "
            "trace fields; denied scopes are not searched."
        ),
    }
    orchestrator_writeback = {
        "phase": "orchestrator_writeback",
        "agent": orchestrator_key,
        "tool": "create_memory_entry",
        "arguments": {
            "title": "Agent organization demo synthesis",
            "body": (
                "The orchestrator synthesized only the authorized specialist "
                "findings that Palace returned with provenance labels. Replace "
                "this placeholder with a reviewed summary, not raw private "
                "specialist memory bodies."
            ),
            "source": "agent-organization-memory-demo",
            "summary": "Orchestrator writes its reviewed synthesis to its own agent scope.",
            "tags": ["agent-organization-demo", f"workspace-{workspace_key}", f"agent-{orchestrator_key}"],
            "scope_type": "agent",
            "scope_key": orchestrator_key,
            "created_by_role": "agent",
            "relationship_policy": "deferred",
            "metadata": {
                "demo": "agent-organization-memory",
                "workspace_key": workspace_key,
                "authorized_specialist_scopes_requested": specialist_keys,
                "writes_only_to_orchestrator_scope": True,
            },
        },
        "purpose": "Orchestrator writes only to agent/orchestrator after review.",
    }
    return {
        "demo": "agent-organization-memory",
        "dry_run": True,
        "workspace_key": workspace_key,
        "orchestrator_key": orchestrator_key,
        "specialist_keys": specialist_keys,
        "privacy_contract": {
            "specialists_write_only_their_own_agent_scope": True,
            "orchestrator_write_scope": f"agent/{orchestrator_key}",
            "cross_agent_reads_are_server_authorized": True,
            "include_broad_corpus": False,
            "raw_secret_output": False,
        },
        "steps": [*specialist_steps, orchestrator_recall, orchestrator_writeback],
        "operator_notes": [
            "Run this as a planning/demo artifact before issuing live MCP tool calls.",
            "Use scoped test tenants or local dev data for live demos.",
            "Do not paste secrets, raw logs, or sensitive user content into demo memory bodies.",
            "Inspect returned provenance labels before turning retrieved memory into instructions.",
        ],
    }


def format_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Agent Organization Memory Demo Payloads",
        "",
        f"- Workspace: `{payload['workspace_key']}`",
        f"- Orchestrator: `agent/{payload['orchestrator_key']}`",
        "- Specialists: "
        + ", ".join(f"`agent/{key}`" for key in payload["specialist_keys"]),
        f"- Dry run: `{str(payload['dry_run']).lower()}`",
        "",
        "## Privacy Contract",
        "",
    ]
    for key, value in payload["privacy_contract"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.append("")
    for index, step in enumerate(payload["steps"], start=1):
        lines.extend(
            [
                f"## Step {index}: {step['phase']}",
                "",
                f"Tool: `{step['tool']}`",
                "",
                step["purpose"],
                "",
                "```json",
                json.dumps(step["arguments"], indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    lines.extend(["## Operator Notes", ""])
    lines.extend(f"- {note}" for note in payload["operator_notes"])
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-key", default=DEFAULT_WORKSPACE)
    parser.add_argument("--orchestrator-key", default=DEFAULT_ORCHESTRATOR)
    parser.add_argument(
        "--specialist",
        action="append",
        dest="specialists",
        help="Specialist agent scope key to include. Repeatable.",
    )
    parser.add_argument(
        "--query",
        default="What should the orchestrator know from the specialist agents before planning this release?",
    )
    parser.add_argument(
        "--access-reason",
        default="demo governed agent-team recall for a reviewed release briefing",
    )
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = demo_payloads(
        workspace_key=args.workspace_key,
        orchestrator_key=args.orchestrator_key,
        specialist_keys=args.specialists or list(DEFAULT_SPECIALISTS),
        query=args.query,
        access_reason=args.access_reason,
    )
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_markdown(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
