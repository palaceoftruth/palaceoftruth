#!/usr/bin/env python3
"""Smoke the canonical agent-memory contract for generic REST and MCP clients.

This helper is intentionally small and non-destructive: it writes one scoped
memory, polls its job, retrieves it, lists scoped memory metadata, checks
session-start context when available, and lists recent memory jobs. The REST
smoke avoids relationship backfill; the MCP smoke can queue a bounded deferred
backfill. Neither smoke retries jobs, uses admin operations, or deletes data.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_API_BASE_URL = "http://localhost:8000"
DEFAULT_MCP_URL = "http://localhost:8765/mcp"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKEND_ROOT = DEFAULT_REPO_ROOT / "backend"
DEFAULT_STDIO_ARGS = [
    "--directory",
    str(DEFAULT_BACKEND_ROOT),
    "run",
    "python",
    "scripts/palaceoftruth_mcp.py",
]
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,48}$")
TERMINAL_SUCCESS = {"complete", "completed", "duplicate"}
TERMINAL_FAILURE = {"failed", "cancelled"}
SENSITIVE_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
SCORECARD_TRANSPORTS = ("rest", "mcp-http", "mcp-stdio")
REQUIRED_SCORE_STEPS = {
    "rest": ("whoami", "write", "poll", "retrieve", "list_entries", "jobs"),
    "mcp-http": ("whoami", "write", "poll", "backfill", "retrieve", "list_entries", "jobs"),
    "mcp-stdio": ("whoami", "write", "poll", "backfill", "retrieve", "list_entries", "jobs"),
}
OPTIONAL_SCORE_STEPS = ("context",)
CODEX_BRIDGE_REQUIRED_SKILLS = (
    "palaceoftruth-memory",
    "palaceoftruth-codex-memory",
)
CODEX_BRIDGE_REQUIRED_TOOLS = {
    "whoami",
    "get_wakeup_context",
    "palace_context",
    "palace_search",
    "retrieve_agent_memory",
    "create_memory_entry",
    "capture_checkpoint",
    "list_memory_entries",
    "list_memory_jobs",
}
CODEX_BRIDGE_PROHIBITED_TOOL_FRAGMENTS = (
    "admin",
    "cleanup",
    "delete",
    "purge",
    "restore",
    "retry",
    "revoke",
    "rotate",
)
ACTIVATION_CATEGORY_NAMES = (
    "tenant_health",
    "scoped_memory_coverage",
    "retrieval_lens_availability",
    "conversation_trajectory_readiness",
    "live_graph_signal_readiness",
    "benchmark_artifact_freshness",
)
FEEDVALUE_INCIDENT_ITEM_ID = "ae129f46-ddb8-45ac-9487-777cc911e558"
RECEIPT_SHELF_INCIDENT_ITEM_ID = "3eaf5fe0-1855-4e19-8898-d110ebecf3ae"
DEFAULT_TASK_POOL_STATUSES = ("ready", "backlog", "in_progress", "review", "blocked")


SMOKE_MATRIX = (
    {
        "step": "whoami",
        "rest": "GET /api/v1/memory/whoami",
        "mcp": "whoami tool or palaceoftruth://connection resource over stdio or streamable HTTP",
        "proves": "the configured tenant API key is valid before writing memory",
    },
    {
        "step": "write",
        "rest": "POST /api/v1/memory/entries",
        "mcp": "create_memory_entry tool",
        "proves": "canonical scoped memory writes do not require client-specific glue",
    },
    {
        "step": "poll",
        "rest": "GET /api/v1/memory/jobs/{job_id}",
        "mcp": "get_memory_job tool",
        "proves": "callers can wait for queued memory ingestion to finish",
    },
    {
        "step": "backfill",
        "rest": "operator-only outside the REST smoke",
        "mcp": "backfill_deferred_relationships tool",
        "proves": "deferred MCP memory imports can request relationship extraction without retry or delete rights",
    },
    {
        "step": "retrieve",
        "rest": "POST /api/v1/memory/retrieve",
        "mcp": "retrieve_memory tool",
        "proves": "written memory can be recalled through the canonical retrieval path",
    },
    {
        "step": "list_entries",
        "rest": "GET /api/v1/memory/entries",
        "mcp": "list_memory_entries tool",
        "proves": "agents can enumerate recent scoped memory metadata without inventing a query",
    },
    {
        "step": "context",
        "rest": "GET /api/v1/memory/wakeup-brief",
        "mcp": "get_wakeup_brief tool",
        "proves": "session-start Palace context is available when the tenant has briefs",
    },
    {
        "step": "jobs",
        "rest": "GET /api/v1/memory/jobs",
        "mcp": "list_memory_jobs tool",
        "proves": "generic agents get read-only operational visibility without mutating recovery rights",
    },
)


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} failed with HTTP {status}: {body[:500]}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


@dataclass(frozen=True)
class Client:
    base_url: str
    api_key: str

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        qs = ""
        if query:
            qs = "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None}, doseq=True)
        url = f"{self.base_url.rstrip('/')}{path}{qs}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8")
                if not payload:
                    return None
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(method, path, exc.code, error_body) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc


@dataclass
class McpClient:
    url: str
    headers: dict[str, str]
    timeout_seconds: float
    sse_read_timeout_seconds: float

    async def __aenter__(self) -> "McpClient":
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:
            raise RuntimeError(
                "MCP smoke requires the Python 'mcp' package. Run from backend with "
                "`uv run python ../scripts/smoke_agent_memory_compatibility.py ...` "
                "or install backend dependencies."
            ) from exc

        self._stack = AsyncExitStack()
        read_stream, write_stream, _ = await self._stack.enter_async_context(
            streamablehttp_client(
                self.url,
                headers=self.headers or None,
                timeout=self.timeout_seconds,
                sse_read_timeout=self.sse_read_timeout_seconds,
            )
        )
        session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self._stack.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._session.call_tool(name, arguments or {})
        return mcp_result_to_payload(name, result)


@dataclass
class StdioMcpClient:
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str | None

    async def __aenter__(self) -> "StdioMcpClient":
        try:
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as exc:
            raise RuntimeError(
                "MCP stdio smoke requires the Python 'mcp' package. Run from backend with "
                "`uv run python ../scripts/smoke_agent_memory_compatibility.py mcp-stdio ...` "
                "or install backend dependencies."
            ) from exc

        self._stack = AsyncExitStack()
        server = StdioServerParameters(command=self.command, args=self.args, env=self.env, cwd=self.cwd)
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(server))
        session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self._stack.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = await self._session.call_tool(name, arguments or {})
        return mcp_result_to_payload(name, result)


def mcp_result_to_payload(name: str, result: Any) -> Any:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool {name} failed: {mcp_result_text(result)}")

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured

    text = mcp_result_text(result)
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return text


def mcp_result_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_run_id() -> str:
    return utc_now().strftime("%Y%m%d-%H%M%S")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise SystemExit("run id must be 3-49 chars and contain only letters, numbers, dot, dash, underscore")
    return run_id


def transport_run_id(base_run_id: str, transport: str) -> str:
    normalized = transport.replace("-", "_")
    candidate = f"{base_run_id}-{normalized}"
    if len(candidate) > 49:
        raise SystemExit(
            "--run-id is too long for scorecard transport suffixes; use 39 chars or fewer"
        )
    return validate_run_id(candidate)


def smoke_tag(run_id: str) -> str:
    return f"agent-memory-smoke-{run_id}"


def normalize_skill_name(value: str) -> str:
    normalized_chars: list[str] = []
    previous_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            normalized_chars.append(char)
            previous_was_separator = False
            continue
        if not previous_was_separator:
            normalized_chars.append("-")
            previous_was_separator = True
    return "".join(normalized_chars).strip("-")


def active_skill_metadata(values: list[str]) -> tuple[list[str], list[str]]:
    names: list[str] = []
    tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        skill = normalize_skill_name(value)
        if not skill or skill in seen:
            continue
        seen.add(skill)
        names.append(value.strip())
        tags.append(f"skill-{skill}")
    return names, tags


def memory_filter_tags(entry: dict[str, Any], run_id: str) -> list[str]:
    metadata = entry.get("metadata")
    skill_tags = metadata.get("skill_tags") if isinstance(metadata, dict) else None
    tags = [smoke_tag(run_id)]
    if isinstance(skill_tags, list):
        tags.extend(tag for tag in skill_tags if isinstance(tag, str) and tag)
    return tags


def make_memory_entry(
    *,
    tenant_id: str,
    run_id: str,
    scope_type: str,
    scope_key: str | None,
    relationship_policy: str,
    active_skills: list[str] | None = None,
) -> dict[str, Any]:
    sentinel = f"AGENT-MEMORY-SMOKE-{run_id}"
    skill_names, skill_tags = active_skill_metadata(active_skills or [])
    tags = ["agent-memory-smoke", smoke_tag(run_id), *skill_tags]
    scope: dict[str, str] = {"type": scope_type}
    if scope_type != "tenant_shared":
        if not scope_key:
            raise SystemExit(f"--scope-key is required when --scope-type is {scope_type}")
        scope["key"] = scope_key
    elif scope_key:
        raise SystemExit("--scope-key must be omitted when --scope-type is tenant_shared")
    return {
        "tenant_id": tenant_id,
        "title": f"Agent memory compatibility smoke {run_id}",
        "body": (
            f"{sentinel}\n\n"
            "This memory verifies the canonical Palace of Truth agent-memory flow for "
            "generic REST and MCP clients. The smoke should write, poll, retrieve, "
            "list scoped memory metadata, load wake-up context when available, and "
            "list recent memory jobs without using retry or deletion endpoints."
        ),
        "source": "agent-memory-smoke",
        "created_at": utc_now().isoformat().replace("+00:00", "Z"),
        "summary": "Agent memory compatibility smoke.",
        "tags": tags,
        "scope": scope,
        "created_by_role": "automation",
        "metadata": {
            "run_id": run_id,
            "smoke": "agent-memory-compatibility",
            **({"active_skills": skill_names, "skill_tags": skill_tags} if skill_tags else {}),
        },
        "idempotency_key": f"agent-memory-smoke:{run_id}",
        "enable_ai_enrichment": False,
        "relationship_policy": relationship_policy,
    }


def mcp_create_memory_arguments(entry: dict[str, Any]) -> dict[str, Any]:
    scope = entry.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("type"), str):
        raise RuntimeError(f"memory entry did not include a valid scope: {entry}")

    arguments = {
        "title": entry["title"],
        "body": entry["body"],
        "source": entry["source"],
        "created_at": entry["created_at"],
        "summary": entry.get("summary"),
        "tags": entry.get("tags") or [],
        "scope_type": scope["type"],
        "scope_key": scope.get("key"),
        "source_url": entry.get("source_url"),
        "created_by_role": entry.get("created_by_role"),
        "metadata": entry.get("metadata"),
        "idempotency_key": entry.get("idempotency_key"),
        "webhook_url": entry.get("webhook_url"),
        "enable_ai_enrichment": bool(entry.get("enable_ai_enrichment")),
        "relationship_policy": entry.get("relationship_policy", "immediate"),
    }
    return {key: value for key, value in arguments.items() if value is not None}


def memory_entry_listing_query(entry: dict[str, Any], run_id: str, *, limit: int = 10) -> dict[str, Any]:
    scope = entry.get("scope")
    if not isinstance(scope, dict) or not isinstance(scope.get("type"), str):
        raise RuntimeError(f"memory entry did not include a valid scope: {entry}")
    query: dict[str, Any] = {
        "scope_type": scope["type"],
        "tags": memory_filter_tags(entry, run_id),
        "tags_mode": "all",
        "limit": limit,
    }
    if scope.get("key") is not None:
        query["scope_key"] = scope["key"]
    return query


def mcp_list_memory_entries_arguments(entry: dict[str, Any], run_id: str, *, limit: int = 10) -> dict[str, Any]:
    return memory_entry_listing_query(entry, run_id, limit=limit)


def retrieve_trace_summary(retrieve: dict[str, Any]) -> dict[str, Any]:
    trace = retrieve.get("trace")
    if not isinstance(trace, dict):
        return {}
    steps = trace.get("steps") if isinstance(trace.get("steps"), list) else []
    tenant_shared_results_merged = any(
        isinstance(step, dict)
        and "tenant_shared" in f"{step.get('title', '')} {step.get('detail', '')}"
        and "Merged" in f"{step.get('title', '')} {step.get('detail', '')}"
        for step in steps
    )
    return {
        "fallback_used": trace.get("fallback_used"),
        "requested_scope_type": trace.get("requested_scope_type"),
        "requested_scope_key": trace.get("requested_scope_key"),
        "tenant_shared_results_merged": tenant_shared_results_merged,
    }


def parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for value in values:
        separator = "=" if "=" in value else ":"
        name, sep, raw_header_value = value.partition(separator)
        if not sep or not name.strip() or not raw_header_value.strip():
            raise SystemExit("--header values must use NAME=VALUE or NAME:VALUE")
        headers[name.strip()] = raw_header_value.strip()
    return headers


def parse_env_overrides(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for value in values:
        name, sep, raw_env_value = value.partition("=")
        if not sep or not name.strip():
            raise SystemExit("--env values must use NAME=VALUE")
        env[name.strip()] = raw_env_value
    return env


def mcp_api_key_from_env(env: dict[str, str]) -> str:
    return (
        env.get("PALACEOFTRUTH_API_KEY")
        or env.get("SECONDBRAIN_API_KEY")
        or env.get("API_KEY")
        or ""
    ).strip()


def build_stdio_env(args: argparse.Namespace) -> dict[str, str]:
    env = dict(os.environ)
    env.update(parse_env_overrides(args.env))
    env.setdefault("PALACEOFTRUTH_API_BASE_URL", args.api_base_url)
    if args.api_key:
        env["PALACEOFTRUTH_API_KEY"] = args.api_key
    if not mcp_api_key_from_env(env):
        raise SystemExit(
            "--api-key, PALACEOFTRUTH_API_KEY, SECONDBRAIN_API_KEY, or API_KEY is required for mcp-stdio"
        )
    return env


def stdio_args_for(args: argparse.Namespace) -> list[str]:
    return list(args.stdio_arg) if args.stdio_arg else list(DEFAULT_STDIO_ARGS)


def _status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if not isinstance(status, str):
        raise RuntimeError(f"memory job response did not include a string status: {payload}")
    return status


def poll_job(
    client: Client,
    *,
    job_id: str,
    interval_seconds: float,
    timeout_seconds: float,
    request_timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        payload = client.request("GET", f"/api/v1/memory/jobs/{job_id}", timeout=request_timeout)
        if not isinstance(payload, dict):
            raise RuntimeError(f"memory job response was not an object: {payload}")
        last_payload = payload
        status = _status(payload)
        if status in TERMINAL_SUCCESS:
            return payload
        if status in TERMINAL_FAILURE:
            raise RuntimeError(f"memory job {job_id} ended with {status}: {payload.get('error_message')}")
        time.sleep(interval_seconds)
    raise RuntimeError(f"memory job {job_id} did not finish before timeout; last payload: {last_payload}")


def run_rest_smoke(args: argparse.Namespace) -> dict[str, Any]:
    api_key = (args.api_key or os.getenv("PALACEOFTRUTH_API_KEY") or os.getenv("SECONDBRAIN_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("--api-key, PALACEOFTRUTH_API_KEY, or SECONDBRAIN_API_KEY is required")

    run_id = validate_run_id(args.run_id or default_run_id())
    client = Client(base_url=args.api_base_url, api_key=api_key)
    result: dict[str, Any] = {"run_id": run_id, "base_url": args.api_base_url, "steps": {}}

    whoami = client.request("GET", "/api/v1/memory/whoami", timeout=args.request_timeout)
    if not isinstance(whoami, dict) or not whoami.get("tenant_id"):
        raise RuntimeError(f"whoami did not return tenant_id: {whoami}")
    tenant_id = str(whoami["tenant_id"])
    result["tenant_id"] = tenant_id
    result["steps"]["whoami"] = {"status": "ok"}

    entry = make_memory_entry(
        tenant_id=tenant_id,
        run_id=run_id,
        scope_type=args.scope_type,
        scope_key=args.scope_key,
        relationship_policy=args.relationship_policy,
        active_skills=args.active_skill,
    )
    accepted = client.request("POST", "/api/v1/memory/entries", body=entry, timeout=args.request_timeout)
    if not isinstance(accepted, dict) or not accepted.get("job_id"):
        raise RuntimeError(f"memory write did not return job_id: {accepted}")
    job_id = str(accepted["job_id"])
    result["job_id"] = job_id
    result["steps"]["write"] = {"status": accepted.get("status"), "accepted_as": accepted.get("accepted_as")}

    job = poll_job(
        client,
        job_id=job_id,
        interval_seconds=args.job_interval_seconds,
        timeout_seconds=args.job_timeout_seconds,
        request_timeout=args.request_timeout,
    )
    result["steps"]["poll"] = {"status": job.get("status")}

    retrieve = client.request(
        "POST",
        "/api/v1/memory/retrieve",
        body={
            "query": f"agent memory compatibility smoke {run_id}",
            "limit": 5,
            "tags": memory_filter_tags(entry, run_id),
            "tags_mode": "all",
            "scope": entry["scope"],
        },
        timeout=args.request_timeout,
    )
    if not isinstance(retrieve, dict):
        raise RuntimeError(f"memory retrieve response was not an object: {retrieve}")
    results = retrieve.get("results")
    hit_count = len(results) if isinstance(results, list) else 0
    if hit_count < 1:
        raise RuntimeError(f"memory retrieve did not return the smoke memory: {retrieve}")
    result["steps"]["retrieve"] = {"status": "ok", "hit_count": hit_count, **retrieve_trace_summary(retrieve)}

    entries = client.request(
        "GET",
        "/api/v1/memory/entries",
        query=memory_entry_listing_query(entry, run_id),
        timeout=args.request_timeout,
    )
    if not isinstance(entries, dict) or "entries" not in entries:
        raise RuntimeError(f"memory entries listing response was not valid: {entries}")
    entry_count = len(entries.get("entries") or [])
    if entry_count < 1:
        raise RuntimeError(f"memory entries listing did not return the smoke memory: {entries}")
    result["steps"]["list_entries"] = {"status": "ok", "returned": entry_count}

    if not args.skip_wakeup_brief:
        try:
            wakeup = client.request("GET", "/api/v1/memory/wakeup-brief", timeout=args.request_timeout)
            result["steps"]["context"] = {
                "status": "ok",
                "freshness": wakeup.get("freshness") if isinstance(wakeup, dict) else None,
            }
        except ApiError as exc:
            if args.fail_missing_wakeup_brief:
                raise
            result["steps"]["context"] = {"status": "skipped", "reason": f"HTTP {exc.status}"}

    jobs = client.request(
        "GET",
        "/api/v1/memory/jobs",
        query={"page": 1, "per_page": 10},
        timeout=args.request_timeout,
    )
    if not isinstance(jobs, dict) or "jobs" not in jobs:
        raise RuntimeError(f"memory jobs listing response was not valid: {jobs}")
    result["steps"]["jobs"] = {"status": "ok", "returned": len(jobs.get("jobs") or [])}
    return result


def build_incident_retrieval_doctor_request(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "agent_scope_key": args.agent_scope_key,
        "workspace_scope_keys": ["feedvalue", "receipt-shelf"],
        "include_tenant_shared": True,
        "include_broad_corpus": False,
        "candidate_limit": args.candidate_limit,
        "display_limit": args.display_limit,
        "sample_probes": [
            {
                "query": "FeedValue current implementation memory",
                "scope": {"type": "workspace", "key": "feedvalue"},
                "expected_item_ids": [FEEDVALUE_INCIDENT_ITEM_ID],
                "limit": args.probe_limit,
            },
            {
                "query": "Receipt Shelf current implementation memory",
                "scope": {"type": "workspace", "key": "receipt-shelf"},
                "expected_item_ids": [RECEIPT_SHELF_INCIDENT_ITEM_ID],
                "limit": args.probe_limit,
            },
        ],
    }


def run_incident_retrieval_doctor(args: argparse.Namespace) -> dict[str, Any]:
    api_key = (args.api_key or os.getenv("PALACEOFTRUTH_API_KEY") or os.getenv("SECONDBRAIN_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("--api-key, PALACEOFTRUTH_API_KEY, or SECONDBRAIN_API_KEY is required")

    client = Client(base_url=args.api_base_url, api_key=api_key)
    request_body = build_incident_retrieval_doctor_request(args)
    report = client.request(
        "POST",
        "/api/v1/memory/retrieval-doctor",
        body=request_body,
        timeout=args.request_timeout,
    )
    if not isinstance(report, dict):
        raise RuntimeError(f"retrieval doctor response was not an object: {report}")

    probes = report.get("probes") if isinstance(report.get("probes"), list) else []
    return {
        "status": report.get("status"),
        "tenant_id": report.get("tenant_id"),
        "selected_scopes": report.get("selected_scopes", []),
        "probes": [
            {
                "probe_index": probe.get("probe_index"),
                "scope": probe.get("scope"),
                "status": probe.get("status"),
                "expected_top_rank": probe.get("expected_top_rank"),
                "selected_scope_result_count": probe.get("selected_scope_result_count"),
                "top_result_ids": [
                    result.get("item_id")
                    for result in probe.get("top_results", [])
                    if isinstance(result, dict)
                ],
                "reasons": probe.get("reasons", []),
            }
            for probe in probes
            if isinstance(probe, dict)
        ],
        "checks": report.get("checks", []),
    }


def cmd_incident_retrieval_doctor(args: argparse.Namespace) -> int:
    report = run_incident_retrieval_doctor(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


def _health_elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 2)


def _health_step_ok(name: str, started: float, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": "ok",
        "duration_ms": _health_elapsed_ms(started),
        **{key: value for key, value in details.items() if value is not None},
    }


def _health_step_error(name: str, started: float, exc: BaseException) -> dict[str, Any]:
    step: dict[str, Any] = {
        "name": name,
        "status": "failed",
        "duration_ms": _health_elapsed_ms(started),
        "error_class": exc.__class__.__name__,
    }
    if isinstance(exc, ApiError):
        step.update({"http_status": exc.status, "method": exc.method, "path": exc.path})
    return step


def _scope_present(scopes: dict[str, Any], *, scope_type: str, scope_key: str | None) -> bool:
    for row in scopes.get("scopes") or []:
        scope = row.get("scope") if isinstance(row, dict) else None
        if not isinstance(scope, dict):
            continue
        if scope.get("type") == scope_type and scope.get("key") == scope_key:
            return True
    return False


def _result_count(payload: dict[str, Any], key: str) -> int:
    values = payload.get(key)
    return len(values) if isinstance(values, list) else 0


def _health_trace_summary(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    return {
        "fallback_used": trace.get("fallback_used"),
        "context_budget_truncated": trace.get("context_budget_truncated") or trace.get("budget_truncated"),
        "searched_scope_count": len(trace.get("searched_scopes") or []),
        "selected_scope_result_count": trace.get("selected_scope_result_count"),
        "broad_result_count": trace.get("broad_result_count"),
        "deduped_result_count": trace.get("deduped_result_count"),
    }


def _run_codex_health_step(
    report: dict[str, Any],
    name: str,
    failures: list[str],
    callback: Any,
) -> dict[str, Any] | None:
    started = time.monotonic()
    try:
        step = callback(started)
    except (ApiError, RuntimeError) as exc:
        step = _health_step_error(name, started, exc)
        reason = f"{name}: {step['error_class']}"
        if isinstance(exc, ApiError):
            reason = f"{name}: HTTP {exc.status} {exc.method} {exc.path}"
        failures.append(reason)
    report["steps"].append(step)
    return step if step.get("status") == "ok" else None


def run_codex_memory_health(args: argparse.Namespace) -> dict[str, Any]:
    api_key = (args.api_key or os.getenv("PALACEOFTRUTH_API_KEY") or os.getenv("SECONDBRAIN_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("--api-key, PALACEOFTRUTH_API_KEY, or SECONDBRAIN_API_KEY is required")

    client = Client(base_url=args.api_base_url, api_key=api_key)
    workspace_scope_keys = args.workspace_scope_key or ["palaceoftruth"]
    failures: list[str] = []
    report: dict[str, Any] = {
        "report": "codex-memory-health",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "api_base_url": args.api_base_url,
        "read_only": True,
        "agent_scope": {"type": "agent", "key": args.agent_scope_key},
        "workspace_scopes": [{"type": "workspace", "key": key} for key in workspace_scope_keys],
        "steps": [],
        "privacy": {
            "writes_memory": False,
            "queues_backfill": False,
            "raw_memory_content_reported": False,
        },
    }

    def whoami_step(started: float) -> dict[str, Any]:
        payload = client.request("GET", "/api/v1/memory/whoami", timeout=args.request_timeout)
        if not isinstance(payload, dict) or not payload.get("tenant_id"):
            raise RuntimeError(f"whoami did not return tenant_id: {payload}")
        report["tenant_id"] = payload["tenant_id"]
        return _health_step_ok("whoami", started, tenant_id=payload.get("tenant_id"))

    _run_codex_health_step(report, "whoami", failures, whoami_step)

    def scopes_step(started: float) -> dict[str, Any]:
        payload = client.request(
            "GET",
            "/api/v1/memory/scopes",
            query={"limit": args.scope_limit, "sample_limit": 0},
            timeout=args.request_timeout,
        )
        if not isinstance(payload, dict) or "scopes" not in payload:
            raise RuntimeError(f"memory scopes response was not valid: {payload}")
        agent_present = _scope_present(payload, scope_type="agent", scope_key=args.agent_scope_key)
        if not agent_present:
            raise RuntimeError(f"agent/{args.agent_scope_key} scope was not listed")
        workspace_present = [
            key
            for key in workspace_scope_keys
            if _scope_present(payload, scope_type="workspace", scope_key=key)
        ]
        if len(workspace_present) != len(workspace_scope_keys):
            missing = sorted(set(workspace_scope_keys) - set(workspace_present))
            raise RuntimeError(f"workspace scope(s) not listed: {', '.join(missing)}")
        return _health_step_ok(
            "list_memory_scopes",
            started,
            returned=_result_count(payload, "scopes"),
            agent_scope_present=agent_present,
            workspace_scope_count=len(workspace_present),
        )

    _run_codex_health_step(report, "list_memory_scopes", failures, scopes_step)

    def entries_step(started: float) -> dict[str, Any]:
        payload = client.request(
            "GET",
            "/api/v1/memory/entries",
            query={
                "scope_type": "agent",
                "scope_key": args.agent_scope_key,
                "limit": args.entry_limit,
            },
            timeout=args.request_timeout,
        )
        if not isinstance(payload, dict) or "entries" not in payload:
            raise RuntimeError(f"memory entries listing response was not valid: {payload}")
        returned = _result_count(payload, "entries")
        if returned < 1:
            raise RuntimeError(f"agent/{args.agent_scope_key} entries listing returned no records")
        return _health_step_ok("list_memory_entries", started, returned=returned, total=payload.get("total"))

    _run_codex_health_step(report, "list_memory_entries", failures, entries_step)

    def retrieve_step(started: float) -> dict[str, Any]:
        payload = client.request(
            "POST",
            "/api/v1/memory/retrieve",
            body={
                "query": args.query,
                "limit": args.limit,
                "context_budget_chars": args.context_budget_chars,
                "scope": {"type": "agent", "key": args.agent_scope_key},
            },
            timeout=args.request_timeout,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"memory retrieve response was not an object: {payload}")
        returned = _result_count(payload, "results")
        if returned < 1:
            raise RuntimeError("scoped retrieve returned no results")
        return _health_step_ok(
            "retrieve_memory",
            started,
            returned=returned,
            **retrieve_trace_summary(payload),
        )

    _run_codex_health_step(report, "retrieve_memory", failures, retrieve_step)

    def route_aware_step(started: float) -> dict[str, Any]:
        payload = client.request(
            "POST",
            "/api/v1/memory/retrieve-agent",
            body={
                "query": args.query,
                "agent_scope_key": args.agent_scope_key,
                "workspace_scope_keys": workspace_scope_keys,
                "include_tenant_shared": False,
                "include_broad_corpus": False,
                "limit": args.limit,
                "display_limit": args.display_limit,
                "context_budget_chars": args.context_budget_chars,
            },
            timeout=args.request_timeout,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"retrieve-agent response was not an object: {payload}")
        returned = _result_count(payload, "results")
        if returned < 1:
            raise RuntimeError("route-aware retrieve-agent returned no results")
        trace = _health_trace_summary(payload)
        if trace.get("fallback_used"):
            raise RuntimeError("route-aware retrieve-agent used fallback")
        if trace.get("broad_result_count"):
            raise RuntimeError("route-aware retrieve-agent returned broad corpus results")
        return _health_step_ok(
            "retrieve_agent_memory",
            started,
            returned=returned,
            include_broad_corpus=False,
            include_tenant_shared=False,
            **trace,
        )

    _run_codex_health_step(report, "retrieve_agent_memory", failures, route_aware_step)

    report["failure_count"] = len(failures)
    report["failures"] = failures
    report["status"] = "passed" if not failures else "failed"
    return report


async def poll_mcp_job(
    client: Any,
    *,
    job_id: str,
    interval_seconds: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        payload = await client.call_tool("get_memory_job", {"job_id": job_id})
        if not isinstance(payload, dict):
            raise RuntimeError(f"MCP get_memory_job response was not an object: {payload}")
        last_payload = payload
        status = _status(payload)
        if status in TERMINAL_SUCCESS:
            return payload
        if status in TERMINAL_FAILURE:
            raise RuntimeError(f"MCP memory job {job_id} ended with {status}: {payload.get('error_message')}")
        await asyncio.sleep(interval_seconds)
    raise RuntimeError(f"MCP memory job {job_id} did not finish before timeout; last payload: {last_payload}")


async def run_mcp_smoke_with_client_async(
    args: argparse.Namespace,
    client: Any,
    *,
    transport: str,
    endpoint: dict[str, Any],
) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id or default_run_id())
    result: dict[str, Any] = {"run_id": run_id, "transport": transport, "steps": {}}
    result.update(endpoint)

    async with client:
        whoami = await client.call_tool("whoami")
        if not isinstance(whoami, dict) or not whoami.get("tenant_id"):
            raise RuntimeError(f"MCP whoami did not return tenant_id: {whoami}")
        tenant_id = str(whoami["tenant_id"])
        result["tenant_id"] = tenant_id
        result["steps"]["whoami"] = {"status": "ok"}

        entry = make_memory_entry(
            tenant_id=tenant_id,
            run_id=run_id,
            scope_type=args.scope_type,
            scope_key=args.scope_key,
            relationship_policy=args.relationship_policy,
            active_skills=args.active_skill,
        )
        accepted = await client.call_tool("create_memory_entry", mcp_create_memory_arguments(entry))
        if not isinstance(accepted, dict) or not accepted.get("job_id"):
            raise RuntimeError(f"MCP create_memory_entry did not return job_id: {accepted}")
        job_id = str(accepted["job_id"])
        result["job_id"] = job_id
        result["steps"]["write"] = {"status": accepted.get("status"), "accepted_as": accepted.get("accepted_as")}

        job = await poll_mcp_job(
            client,
            job_id=job_id,
            interval_seconds=args.job_interval_seconds,
            timeout_seconds=args.job_timeout_seconds,
        )
        result["steps"]["poll"] = {"status": job.get("status")}

        if args.skip_backfill:
            result["steps"]["backfill"] = {"status": "skipped", "reason": "--skip-backfill"}
        elif args.relationship_policy != "deferred":
            result["steps"]["backfill"] = {
                "status": "skipped",
                "reason": "relationship_policy is not deferred",
            }
        else:
            backfill = await client.call_tool(
                "backfill_deferred_relationships",
                {
                    "limit": args.backfill_limit,
                    "defer_seconds": args.backfill_defer_seconds,
                },
            )
            if not isinstance(backfill, dict):
                raise RuntimeError(f"MCP backfill_deferred_relationships response was not an object: {backfill}")
            backfill_step = {
                "status": backfill.get("status") or "ok",
                "limit": backfill.get("limit"),
                "defer_seconds": backfill.get("defer_seconds"),
            }
            if backfill.get("lease_key") is not None:
                backfill_step["lease_key"] = backfill.get("lease_key")
            if backfill.get("lease_holder") is not None:
                backfill_step["lease_holder"] = backfill.get("lease_holder")
            result["steps"]["backfill"] = backfill_step

        retrieve = await client.call_tool(
            "retrieve_memory",
            {
                "query": f"agent memory compatibility smoke {run_id}",
                "limit": 5,
                "tags": memory_filter_tags(entry, run_id),
                "tags_mode": "all",
                "scope_type": entry["scope"]["type"],
                "scope_key": entry["scope"].get("key"),
            },
        )
        if not isinstance(retrieve, dict):
            raise RuntimeError(f"MCP retrieve_memory response was not an object: {retrieve}")
        results = retrieve.get("results")
        hit_count = len(results) if isinstance(results, list) else 0
        if hit_count < 1:
            raise RuntimeError(f"MCP retrieve_memory did not return the smoke memory: {retrieve}")
        result["steps"]["retrieve"] = {"status": "ok", "hit_count": hit_count, **retrieve_trace_summary(retrieve)}

        entries = await client.call_tool("list_memory_entries", mcp_list_memory_entries_arguments(entry, run_id))
        if not isinstance(entries, dict) or "entries" not in entries:
            raise RuntimeError(f"MCP list_memory_entries response was not valid: {entries}")
        entry_count = len(entries.get("entries") or [])
        if entry_count < 1:
            raise RuntimeError(f"MCP list_memory_entries did not return the smoke memory: {entries}")
        result["steps"]["list_entries"] = {"status": "ok", "returned": entry_count}

        if args.skip_wakeup_brief:
            result["steps"]["context"] = {"status": "skipped", "reason": "--skip-wakeup-brief"}
        else:
            try:
                wakeup_args: dict[str, Any] = {"scope_type": args.wakeup_scope_type}
                if args.wakeup_scope_key is not None:
                    wakeup_args["scope_key"] = args.wakeup_scope_key
                wakeup = await client.call_tool("get_wakeup_brief", wakeup_args)
                result["steps"]["context"] = {
                    "status": "ok",
                    "freshness": wakeup.get("freshness") if isinstance(wakeup, dict) else None,
                }
            except RuntimeError as exc:
                if args.fail_missing_wakeup_brief:
                    raise
                result["steps"]["context"] = {"status": "skipped", "reason": str(exc)[:200]}

        jobs = await client.call_tool("list_memory_jobs", {"page": 1, "per_page": 10})
        if not isinstance(jobs, dict) or "jobs" not in jobs:
            raise RuntimeError(f"MCP list_memory_jobs response was not valid: {jobs}")
        result["steps"]["jobs"] = {"status": "ok", "returned": len(jobs.get("jobs") or [])}

    return result


async def run_mcp_smoke_async(args: argparse.Namespace) -> dict[str, Any]:
    headers = parse_headers(args.header)
    return await run_mcp_smoke_with_client_async(
        args,
        McpClient(
            url=args.mcp_url,
            headers=headers,
            timeout_seconds=args.request_timeout,
            sse_read_timeout_seconds=args.sse_read_timeout,
        ),
        transport="streamable-http",
        endpoint={"mcp_url": args.mcp_url},
    )


async def run_mcp_stdio_smoke_async(args: argparse.Namespace) -> dict[str, Any]:
    command_args = stdio_args_for(args)
    env = build_stdio_env(args)
    return await run_mcp_smoke_with_client_async(
        args,
        StdioMcpClient(
            command=args.stdio_command,
            args=command_args,
            env=env,
            cwd=args.stdio_cwd,
        ),
        transport="stdio",
        endpoint={
            "stdio_command": args.stdio_command,
            "stdio_args": command_args,
            "stdio_cwd": args.stdio_cwd,
        },
    )


def run_mcp_smoke(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_mcp_smoke_async(args))


def run_mcp_stdio_smoke(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_mcp_stdio_smoke_async(args))


def redact_value(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if SENSITIVE_ENV_RE.search(name):
        return "<redacted>"
    return value


def env_diagnostics(args: argparse.Namespace) -> dict[str, Any]:
    names = (
        "PALACEOFTRUTH_API_BASE_URL",
        "PALACEOFTRUTH_API_KEY",
        "PALACEOFTRUTH_MCP_URL",
        "PALACEOFTRUTH_MCP_TRANSPORT",
        "PALACEOFTRUTH_MCP_ALLOWED_HOSTS",
        "PALACEOFTRUTH_MCP_ALLOWED_ORIGINS",
        "SECONDBRAIN_API_BASE_URL",
        "SECONDBRAIN_API_KEY",
        "SECONDBRAIN_MCP_URL",
        "API_KEY",
    )
    values = {
        name: redact_value(name, os.getenv(name))
        for name in names
        if os.getenv(name) is not None
    }
    values["effective_api_base_url"] = args.api_base_url
    values["effective_mcp_url"] = getattr(args, "mcp_url", DEFAULT_MCP_URL)
    values["stdio_command"] = getattr(args, "stdio_command", "uv")
    values["stdio_args"] = stdio_args_for(args)
    values["stdio_cwd"] = getattr(args, "stdio_cwd", str(DEFAULT_REPO_ROOT))
    return values


def readiness_check(name: str, status: str, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        **{key: value for key, value in details.items() if value is not None},
    }


def _append_failure(failures: list[str], check: str, reason: str) -> None:
    failures.append(f"{check}: {reason}")


def analyze_stats(stats: dict[str, Any], failures: list[str]) -> dict[str, Any]:
    active_jobs = int(stats.get("active_jobs") or 0)
    active_memory_jobs = int(stats.get("active_memory_jobs") or 0)
    failed_memory_jobs = int(stats.get("failed_memory_jobs") or 0)
    if failed_memory_jobs:
        _append_failure(failures, "stats", f"{failed_memory_jobs} failed memory jobs")
    if active_memory_jobs:
        _append_failure(failures, "stats", f"{active_memory_jobs} active memory jobs")
    return {
        "total_items": stats.get("total_items"),
        "ready_items": stats.get("ready_items"),
        "indexed_items": stats.get("indexed_items"),
        "active_jobs": active_jobs,
        "active_memory_jobs": active_memory_jobs,
        "failed_memory_jobs": failed_memory_jobs,
        "orphaned_ready_items": stats.get("orphaned_ready_items"),
    }


def analyze_control_tower(control_tower: dict[str, Any], failures: list[str]) -> dict[str, Any]:
    room_artifacts = (
        control_tower.get("room_artifacts")
        if isinstance(control_tower.get("room_artifacts"), dict)
        else {}
    )
    wakeup_briefs = (
        control_tower.get("wakeup_briefs")
        if isinstance(control_tower.get("wakeup_briefs"), dict)
        else {}
    )
    worker_backpressure = (
        control_tower.get("worker_backpressure")
        if isinstance(control_tower.get("worker_backpressure"), dict)
        else {}
    )
    blocked_rooms = int(room_artifacts.get("blocked_rooms") or 0)
    stale_wakeup = int(wakeup_briefs.get("stale") or 0)
    backlog_generation = int(control_tower.get("backlog_generation") or 0)
    active_run = control_tower.get("active_palace_run")
    queues = worker_backpressure.get("queues") if isinstance(worker_backpressure, dict) else []
    active_queues = [
        {
            "queue_name": queue.get("queue_name"),
            "queued_depth": queue.get("queued_depth"),
            "worker_queue_depth": queue.get("worker_queue_depth"),
        }
        for queue in (queues or [])
        if isinstance(queue, dict)
        and ((queue.get("queued_depth") or 0) or (queue.get("worker_queue_depth") or 0))
    ]
    if backlog_generation:
        _append_failure(failures, "control_tower", f"Palace backlog generation is {backlog_generation}")
    if active_run:
        _append_failure(failures, "control_tower", "active Palace run is present")
    if blocked_rooms:
        _append_failure(failures, "control_tower", f"{blocked_rooms} blocked rooms")
    if stale_wakeup:
        _append_failure(failures, "control_tower", f"{stale_wakeup} stale wake-up briefs")
    if active_queues:
        _append_failure(failures, "control_tower", f"{len(active_queues)} worker queues have pending work")
    return {
        "dirty_generation": control_tower.get("dirty_generation"),
        "indexed_generation": control_tower.get("indexed_generation"),
        "backlog_generation": backlog_generation,
        "active_palace_run": bool(active_run),
        "blocked_rooms": blocked_rooms,
        "stale_wakeup_briefs": stale_wakeup,
        "active_worker_queues": active_queues,
    }


def import_nist_benchmark_module() -> Any:
    scripts_dir = Path(__file__).resolve().parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import benchmark_nist_sp800_staging as nist_benchmark

    return nist_benchmark


def summarize_retained_corpus(run_ids: list[str], failures: list[str]) -> list[dict[str, Any]]:
    nist_benchmark = import_nist_benchmark_module()
    summaries: list[dict[str, Any]] = []
    for run_id in run_ids:
        summary = nist_benchmark.summarize_nist_run_artifacts(run_id)
        warnings = summary.get("warnings") or []
        top_rank_failures = summary.get("top_rank_failures") or []
        dogfood_failures = summary.get("dogfood_failures") or []
        if warnings:
            _append_failure(
                failures,
                f"retained_corpus:{run_id}",
                f"{len(warnings)} artifact warnings",
            )
        if not summary.get("cleanup_plan_exists"):
            _append_failure(failures, f"retained_corpus:{run_id}", "cleanup plan is missing")
        if summary.get("relationship_queue_drained") is False:
            _append_failure(failures, f"retained_corpus:{run_id}", "relationship queue is not drained")
        if top_rank_failures:
            _append_failure(
                failures,
                f"retained_corpus:{run_id}",
                f"{len(top_rank_failures)} top-rank failures",
            )
        if dogfood_failures:
            _append_failure(failures, f"retained_corpus:{run_id}", f"{len(dogfood_failures)} dogfood failures")
        if summary.get("wakeup_briefs_stale"):
            _append_failure(failures, f"retained_corpus:{run_id}", "stale wake-up briefs in artifact report")
        if summary.get("blocked_rooms"):
            _append_failure(failures, f"retained_corpus:{run_id}", "blocked rooms in artifact report")
        summaries.append(summary)
    return summaries


def strict_retrieve_trace_failures(smoke_result: dict[str, Any]) -> list[str]:
    retrieve = smoke_result.get("steps", {}).get("retrieve")
    if not isinstance(retrieve, dict):
        return ["live smoke did not record retrieve step"]
    failures: list[str] = []
    if retrieve.get("fallback_used") is True:
        failures.append("retrieval used global fallback")
    if retrieve.get("tenant_shared_results_merged") is True:
        failures.append("retrieval merged tenant_shared results")
    return failures


def retrieval_lens_profiles() -> list[dict[str, Any]]:
    try:
        backend_root = DEFAULT_BACKEND_ROOT
        if str(backend_root) not in sys.path:
            sys.path.insert(0, str(backend_root))
        from app.services.retrieval_lenses import RETRIEVAL_LENS_PROFILES

        return [
            profile.as_trace()
            for _, profile in sorted(RETRIEVAL_LENS_PROFILES.items())
        ]
    except Exception as exc:
        return [
            {
                "name": "unknown",
                "description": "retrieval lens registry could not be loaded",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        ]


def activation_category(
    name: str,
    *,
    score: int,
    max_score: int,
    status: str,
    signals: dict[str, Any],
    remediations: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "score": score,
        "max_score": max_score,
        "status": status,
        "signals": signals,
        "remediations": remediations,
    }


def _check_status(report: dict[str, Any], check_name: str) -> str | None:
    for check in report.get("checks") or []:
        if isinstance(check, dict) and check.get("name") == check_name:
            return str(check.get("status"))
    return None


def _failures_with_prefix(report: dict[str, Any], prefix: str) -> list[str]:
    return [
        failure
        for failure in report.get("failures") or []
        if isinstance(failure, str) and failure.startswith(prefix)
    ]


def _scope_count(scopes: dict[str, Any], scope_type: str, scope_key: str | None = None) -> int:
    count = 0
    for row in scopes.get("scopes") or []:
        scope = row.get("scope") if isinstance(row, dict) else None
        if not isinstance(scope, dict) or scope.get("type") != scope_type:
            continue
        if scope_key is not None and scope.get("key") != scope_key:
            continue
        count += 1
    return count


def build_activation_dry_run_report(args: argparse.Namespace) -> dict[str, Any]:
    args.workspace_scope_key = args.workspace_scope_key or ["palaceoftruth"]
    lenses = retrieval_lens_profiles()
    categories = [
        activation_category(
            "tenant_health",
            score=0,
            max_score=3,
            status="not_checked",
            signals={"dry_run": True},
            remediations=["Run without --dry-run using a tenant API key to verify API health and tenant identity."],
        ),
        activation_category(
            "scoped_memory_coverage",
            score=0,
            max_score=3,
            status="not_checked",
            signals={
                "agent_scope": {"type": "agent", "key": args.agent_scope_key},
                "workspace_scopes": [{"type": "workspace", "key": key} for key in args.workspace_scope_key],
            },
            remediations=["Run the report against the target tenant to confirm agent/workspace memory coverage."],
        ),
        activation_category(
            "retrieval_lens_availability",
            score=2 if len(lenses) > 1 and lenses[0].get("name") != "unknown" else 0,
            max_score=2,
            status="ready" if len(lenses) > 1 and lenses[0].get("name") != "unknown" else "blocked",
            signals={"available_lenses": [lens.get("name") for lens in lenses]},
            remediations=[] if len(lenses) > 1 else ["Restore the retrieval lens registry before onboarding operators."],
        ),
        activation_category(
            "conversation_trajectory_readiness",
            score=0,
            max_score=2,
            status="not_checked",
            signals={"query": args.activation_query},
            remediations=["Run without --dry-run to call the read-only /api/v1/memory/trajectory endpoint."],
        ),
        activation_category(
            "live_graph_signal_readiness",
            score=0,
            max_score=2,
            status="not_checked",
            signals={"retrieval_doctor": "not run in dry-run"},
            remediations=["Run without --dry-run to call retrieval-doctor and inspect Control Tower graph signals."],
        ),
        activation_category(
            "benchmark_artifact_freshness",
            score=0,
            max_score=2,
            status="not_checked",
            signals={"nist_run_ids": args.nist_run_id},
            remediations=["Pass --nist-run-id for the retained benchmark artifacts this tenant should trust."],
        ),
    ]
    report = activation_report_envelope(args, categories, readiness=None, extra={"read_only": True, "dry_run": True})
    report["status"] = "dry-run"
    return report


def activation_report_envelope(
    args: argparse.Namespace,
    categories: list[dict[str, Any]],
    *,
    readiness: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score = sum(int(category.get("score") or 0) for category in categories)
    max_score = sum(int(category.get("max_score") or 0) for category in categories)
    score_percent = round((score / max_score) * 100, 2) if max_score else 0.0
    category_statuses = {str(category.get("status")) for category in categories}
    if "blocked" in category_statuses:
        status = "blocked"
    elif "needs_remediation" in category_statuses:
        status = "needs_remediation"
    elif category_statuses.intersection({"needs_data", "not_checked"}):
        status = "needs_data"
    elif score_percent >= args.target_score_percent:
        status = "activated"
    else:
        status = "needs_remediation"
    remediations: list[str] = []
    for category in categories:
        for remediation in category.get("remediations") or []:
            if remediation not in remediations:
                remediations.append(remediation)
    report = {
        "report": "palace-activation-onboarding",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "run_id": validate_run_id(args.run_id or default_run_id()),
        "api_base_url": args.api_base_url,
        "status": status,
        "score": score,
        "max_score": max_score,
        "score_percent": score_percent,
        "read_only": True,
        "privacy": {
            "writes_memory": False,
            "queues_backfill": False,
            "raw_memory_content_reported": False,
            "live_smoke_writes_memory": bool(args.live_smoke),
        },
        "activation_targets": {
            "agent_scope": {"type": "agent", "key": args.agent_scope_key},
            "workspace_scopes": [{"type": "workspace", "key": key} for key in args.workspace_scope_key],
            "query": args.activation_query,
            "target_score_percent": args.target_score_percent,
        },
        "categories": categories,
        "remediations": remediations,
    }
    if readiness is not None:
        report["operator_readiness_status"] = readiness.get("status")
        report["operator_readiness_failure_count"] = readiness.get("failure_count")
        report["operator_readiness_failures"] = readiness.get("failures", [])
    if extra:
        report.update(extra)
    return report


def build_activation_onboarding_report(args: argparse.Namespace) -> dict[str, Any]:
    args.workspace_scope_key = args.workspace_scope_key or ["palaceoftruth"]
    if not hasattr(args, "scope_type"):
        args.scope_type = "workspace"
    if not hasattr(args, "scope_key"):
        args.scope_key = args.workspace_scope_key[0]
    if args.dry_run:
        return build_activation_dry_run_report(args)

    readiness = build_operator_readiness_report(args)
    api_key = (args.api_key or os.getenv("PALACEOFTRUTH_API_KEY") or os.getenv("SECONDBRAIN_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("--api-key, PALACEOFTRUTH_API_KEY, or SECONDBRAIN_API_KEY is required")
    client = Client(base_url=args.api_base_url, api_key=api_key)
    categories: list[dict[str, Any]] = []

    stats_failures = _failures_with_prefix(readiness, "stats:")
    control_failures = _failures_with_prefix(readiness, "control_tower:")
    tenant_score = 0
    tenant_score += 1 if _check_status(readiness, "api_health") == "ok" else 0
    tenant_score += 1 if _check_status(readiness, "tenant_identity") == "ok" else 0
    tenant_score += 1 if not stats_failures else 0
    categories.append(
        activation_category(
            "tenant_health",
            score=tenant_score,
            max_score=3,
            status="ready" if tenant_score == 3 else "blocked",
            signals={
                "api_health": _check_status(readiness, "api_health"),
                "tenant_identity": _check_status(readiness, "tenant_identity"),
                "stats_failures": stats_failures,
            },
            remediations=[] if tenant_score == 3 else ["Resolve API, tenant identity, or memory job failures before trusting onboarding output."],
        )
    )

    scopes = client.request(
        "GET",
        "/api/v1/memory/scopes",
        query={"limit": args.scope_limit, "sample_limit": 0},
        timeout=args.request_timeout,
    )
    if not isinstance(scopes, dict) or "scopes" not in scopes:
        raise RuntimeError(f"memory scopes response was not valid: {scopes}")
    agent_scope_count = _scope_count(scopes, "agent", args.agent_scope_key)
    workspace_counts = {key: _scope_count(scopes, "workspace", key) for key in args.workspace_scope_key}
    total_scopes = _result_count(scopes, "scopes")
    scoped_score = 1 if total_scopes else 0
    scoped_score += 1 if agent_scope_count else 0
    scoped_score += 1 if all(count > 0 for count in workspace_counts.values()) else 0
    scoped_remediations = []
    if not agent_scope_count:
        scoped_remediations.append(f"Seed or import at least one memory in agent/{args.agent_scope_key}.")
    missing_workspaces = [key for key, count in workspace_counts.items() if not count]
    if missing_workspaces:
        scoped_remediations.append("Seed workspace memory for: " + ", ".join(sorted(missing_workspaces)) + ".")
    categories.append(
        activation_category(
            "scoped_memory_coverage",
            score=scoped_score,
            max_score=3,
            status="ready" if scoped_score == 3 else "needs_remediation",
            signals={
                "total_scope_rows": total_scopes,
                "agent_scope_count": agent_scope_count,
                "workspace_scope_counts": workspace_counts,
            },
            remediations=scoped_remediations,
        )
    )

    lenses = retrieval_lens_profiles()
    lens_names = [str(lens.get("name")) for lens in lenses]
    lens_score = 2 if "default" in lens_names and any(name != "default" for name in lens_names) else 1 if lens_names else 0
    categories.append(
        activation_category(
            "retrieval_lens_availability",
            score=lens_score,
            max_score=2,
            status="ready" if lens_score == 2 else "needs_remediation",
            signals={"available_lenses": lens_names},
            remediations=[] if lens_score == 2 else ["Restore default plus domain retrieval lenses before agent onboarding."],
        )
    )

    trajectory = client.request(
        "POST",
        "/api/v1/memory/trajectory",
        body={
            "query": args.activation_query,
            "agent_scope_key": args.agent_scope_key,
            "workspace_scope_keys": args.workspace_scope_key,
            "include_tenant_shared": False,
            "include_broad_corpus": False,
            "limit": args.display_limit,
            "display_limit": args.display_limit,
            "context_budget_chars": args.context_budget_chars,
        },
        timeout=args.request_timeout,
    )
    if not isinstance(trajectory, dict):
        raise RuntimeError(f"trajectory response was not valid: {trajectory}")
    trajectory_total = int(trajectory.get("total") or 0)
    current_entries = _result_count(trajectory, "current_entries")
    trajectory_score = 1 if "trace" in trajectory else 0
    trajectory_score += 1 if trajectory_total or current_entries else 0
    categories.append(
        activation_category(
            "conversation_trajectory_readiness",
            score=trajectory_score,
            max_score=2,
            status="ready" if trajectory_score == 2 else "needs_remediation",
            signals={
                "total": trajectory_total,
                "current_entries": current_entries,
                "searched_scope_count": len((trajectory.get("trace") or {}).get("searched_scopes") or []),
            },
            remediations=[] if trajectory_score == 2 else ["Backfill or capture conversation facts before relying on trajectory answers."],
        )
    )

    doctor = client.request(
        "POST",
        "/api/v1/memory/retrieval-doctor",
        body={
            "agent_scope_key": args.agent_scope_key,
            "workspace_scope_keys": args.workspace_scope_key,
            "include_tenant_shared": False,
            "include_broad_corpus": False,
            "candidate_limit": args.candidate_limit,
            "display_limit": args.display_limit,
            "context_budget_chars": args.context_budget_chars,
            "sample_probes": [
                {
                    "query": args.activation_query,
                    "scope": {"type": "agent", "key": args.agent_scope_key},
                    "limit": args.display_limit,
                }
            ],
        },
        timeout=args.request_timeout,
    )
    if not isinstance(doctor, dict):
        raise RuntimeError(f"retrieval doctor response was not valid: {doctor}")
    doctor_status = str(doctor.get("status") or "unknown")
    graph_score = 0
    graph_score += 1 if doctor_status in {"ok", "degraded"} else 0
    graph_score += 1 if not control_failures else 0
    categories.append(
        activation_category(
            "live_graph_signal_readiness",
            score=graph_score,
            max_score=2,
            status="ready" if graph_score == 2 else "needs_remediation",
            signals={
                "retrieval_doctor_status": doctor_status,
                "control_tower_failures": control_failures,
                "check_count": _result_count(doctor, "checks"),
            },
            remediations=[] if graph_score == 2 else ["Clear Control Tower graph backlog or inspect retrieval-doctor failures."],
        )
    )

    retained_failures = _failures_with_prefix(readiness, "retained_corpus:")
    retained_status = _check_status(readiness, "retained_corpus")
    retained_count = len(readiness.get("retained_corpus") or [])
    if retained_status == "ok" and not retained_failures:
        benchmark_score = 2
        benchmark_remediations: list[str] = []
    elif retained_status == "skipped":
        benchmark_score = 1
        benchmark_remediations = ["Pass --nist-run-id for the retained benchmark artifact operators should trust."]
    else:
        benchmark_score = 0
        benchmark_remediations = ["Regenerate or recover retained benchmark artifacts before making readiness claims."]
    categories.append(
        activation_category(
            "benchmark_artifact_freshness",
            score=benchmark_score,
            max_score=2,
            status="ready" if benchmark_score == 2 else "needs_data" if benchmark_score == 1 else "needs_remediation",
            signals={
                "retained_corpus_status": retained_status,
                "retained_corpus_count": retained_count,
                "retained_corpus_failures": retained_failures,
            },
            remediations=benchmark_remediations,
        )
    )

    report = activation_report_envelope(
        args,
        categories,
        readiness=readiness,
        extra={
            "tenant_id": readiness.get("tenant_id"),
            "retrieval_lenses": lenses,
        },
    )
    if report["score_percent"] < args.target_score_percent and report["status"] == "activated":
        report["status"] = "needs_remediation"
    return report


def score_step(step: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not isinstance(step, dict):
        return False, "missing"
    status = step.get("status")
    if status in {"ok", "queued", "complete", "completed", "duplicate"}:
        return True, None
    return False, str(step.get("reason") or status or "unknown")


def score_smoke_result(
    transport: str,
    smoke_result: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    required_steps = REQUIRED_SCORE_STEPS[transport]
    step_results: dict[str, Any] = {}
    passed = 0
    steps = smoke_result.get("steps", {}) if isinstance(smoke_result, dict) else {}
    for name in required_steps:
        ok, reason = score_step(steps.get(name) if isinstance(steps, dict) else None)
        if ok:
            passed += 1
        step_results[name] = {
            "status": "passed" if ok else "failed",
            **({"reason": reason} if reason else {}),
        }

    optional: dict[str, Any] = {}
    for name in OPTIONAL_SCORE_STEPS:
        if isinstance(steps, dict) and name in steps:
            ok, reason = score_step(steps.get(name))
            if ok:
                status = "passed"
            elif reason and reason.startswith("--"):
                status = "skipped"
            else:
                status = "warning"
            optional[name] = {"status": status, **({"reason": reason} if reason else {})}

    total = len(required_steps)
    return {
        "transport": transport,
        "status": "passed" if passed == total and not error else "failed",
        "score": passed,
        "max_score": total,
        "score_percent": round((passed / total) * 100, 2),
        "required_steps": step_results,
        "optional_steps": optional,
        **({"error": error} if error else {}),
    }


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def _import_setup_module() -> Any:
    scripts_dir = _scripts_dir()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import setup_codex_palace_memory

    return setup_codex_palace_memory


def _import_lifecycle_module() -> Any:
    scripts_dir = _scripts_dir()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import codex_session_lifecycle

    return codex_session_lifecycle


def _import_mcp_server_module() -> Any:
    backend_root = DEFAULT_BACKEND_ROOT
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    from app import mcp_server

    return mcp_server


def _import_benchmark_module() -> Any:
    script_path = _scripts_dir() / "benchmark_agent_memory_retrieval.py"
    spec = importlib.util.spec_from_file_location("benchmark_agent_memory_retrieval_for_startup", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bridge_check(name: str, status: str, **details: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        **{key: value for key, value in details.items() if value is not None},
    }


def _json_contains_raw_secret(payload: Any) -> bool:
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return any(
        token in serialized
        for token in (
            "tenant-api-key",
            "secret-value",
            "PALACEOFTRUTH_API_KEY=secret",
            "SECONDBRAIN_API_KEY=secret",
        )
    )


def _skillpack_files() -> list[Path]:
    root = DEFAULT_REPO_ROOT / "plugins" / "palaceoftruth-memory"
    return [
        root / ".codex-plugin" / "plugin.json",
        root / ".mcp.json",
        root / "README.md",
        *[
            root / "skills" / skill / "SKILL.md"
            for skill in CODEX_BRIDGE_REQUIRED_SKILLS
        ],
    ]


def codex_bridge_setup_report(args: argparse.Namespace) -> dict[str, Any]:
    setup_module = _import_setup_module()
    setup_args = setup_module.build_parser().parse_args(
        [
            "--api-base-url",
            args.api_base_url,
            "--run-id",
            args.run_id,
            "--scope-type",
            args.scope_type,
            *([] if args.scope_key is None else ["--scope-key", args.scope_key]),
            "--format",
            "json",
        ]
    )
    return setup_module.build_report(setup_args)


def codex_bridge_lifecycle_payload(args: argparse.Namespace) -> dict[str, Any]:
    lifecycle_module = _import_lifecycle_module()
    return lifecycle_module.lifecycle_payloads(
        cwd=str(DEFAULT_REPO_ROOT),
        workspace_key=args.workspace_key,
        session_key=args.session_key,
        agent_scope_key=args.scope_key or "codex",
        query="What Palace memory should Codex recall before working in this repository?",
    )


async def _mcp_tool_names_async() -> list[str]:
    mcp_server = _import_mcp_server_module()
    tools = await mcp_server.mcp.list_tools()
    return sorted(tool.name for tool in tools)


def codex_bridge_tool_names() -> list[str]:
    try:
        return asyncio.run(_mcp_tool_names_async())
    except ImportError:
        return codex_bridge_tool_names_from_source()


def codex_bridge_tool_names_from_source() -> list[str]:
    source = (DEFAULT_BACKEND_ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
            ):
                names.append(node.name)
                break
    return sorted(names)


def build_codex_bridge_report(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    failures: list[str] = []

    missing = [str(path.relative_to(DEFAULT_REPO_ROOT)) for path in _skillpack_files() if not path.is_file()]
    if missing:
        failures.append(f"skillpack missing files: {', '.join(missing)}")
        checks.append(bridge_check("skillpack_presence", "failed", missing=missing))
    else:
        checks.append(
            bridge_check(
                "skillpack_presence",
                "ok",
                skills=list(CODEX_BRIDGE_REQUIRED_SKILLS),
            )
        )

    try:
        setup_report = codex_bridge_setup_report(args)
        live_command = setup_report.get("live_smoke_command") or []
        setup_failures = []
        if setup_report.get("mutating") is not False or setup_report.get("dry_run") is not True:
            setup_failures.append("setup verifier is not dry-run by default")
        if "palaceoftruth-codex-memory" not in str(setup_report.get("skillpack")):
            setup_failures.append("setup verifier did not target the Codex skillpack")
        if "mcp-stdio" not in live_command:
            setup_failures.append("setup verifier does not preview mcp-stdio")
        if "--skip-backfill" not in live_command:
            setup_failures.append("setup live-smoke command does not skip backfill")
        if _json_contains_raw_secret(setup_report):
            setup_failures.append("setup verifier output included a raw secret-like token")
        if setup_failures:
            failures.extend(f"setup_verifier: {failure}" for failure in setup_failures)
            checks.append(bridge_check("setup_verifier", "failed", failures=setup_failures))
        else:
            checks.append(
                bridge_check(
                    "setup_verifier",
                    "ok",
                    mutating=False,
                    live_smoke_preview="mcp-stdio",
                )
            )
    except Exception as exc:
        failures.append(f"setup_verifier: {exc}")
        checks.append(bridge_check("setup_verifier", "failed", error=str(exc)))

    try:
        lifecycle = codex_bridge_lifecycle_payload(args)
        steps = {step["tool"]: step["arguments"] for step in lifecycle.get("steps", [])}
        retrieve = steps.get("retrieve_agent_memory") or {}
        checkpoint = steps.get("capture_checkpoint") or {}
        lifecycle_failures = []
        if retrieve.get("agent_scope_key") != (args.scope_key or "codex"):
            lifecycle_failures.append("retrieve_agent_memory does not default to agent/codex")
        if args.workspace_key not in (retrieve.get("workspace_scope_keys") or []):
            lifecycle_failures.append("retrieve_agent_memory does not include the workspace key")
        if retrieve.get("include_broad_corpus") is not False:
            lifecycle_failures.append("retrieve_agent_memory allows broad corpus fallback")
        if checkpoint.get("dry_run") is not True:
            lifecycle_failures.append("capture_checkpoint is not dry-run by default")
        if checkpoint.get("scope_type") not in {"session", "workspace"}:
            lifecycle_failures.append("capture_checkpoint scope is not session/workspace")
        if _json_contains_raw_secret(lifecycle):
            lifecycle_failures.append("lifecycle payload included a raw secret-like token")
        if lifecycle_failures:
            failures.extend(f"lifecycle_payload: {failure}" for failure in lifecycle_failures)
            checks.append(bridge_check("lifecycle_payload", "failed", failures=lifecycle_failures))
        else:
            checks.append(
                bridge_check(
                    "lifecycle_payload",
                    "ok",
                    recall="retrieve_agent_memory",
                    checkpoint="dry-run",
                )
            )
    except Exception as exc:
        failures.append(f"lifecycle_payload: {exc}")
        checks.append(bridge_check("lifecycle_payload", "failed", error=str(exc)))

    try:
        tool_names = codex_bridge_tool_names()
        missing_tools = sorted(CODEX_BRIDGE_REQUIRED_TOOLS - set(tool_names))
        prohibited = sorted(
            name
            for name in tool_names
            if any(fragment in name for fragment in CODEX_BRIDGE_PROHIBITED_TOOL_FRAGMENTS)
        )
        if missing_tools or prohibited:
            if missing_tools:
                failures.append(f"mcp_tool_surface missing tools: {', '.join(missing_tools)}")
            if prohibited:
                failures.append(f"mcp_tool_surface exposes prohibited tools: {', '.join(prohibited)}")
            checks.append(
                bridge_check(
                    "mcp_tool_surface",
                    "failed",
                    missing_tools=missing_tools,
                    prohibited_tools=prohibited,
                )
            )
        else:
            checks.append(
                bridge_check(
                    "mcp_tool_surface",
                    "ok",
                    required_tools=sorted(CODEX_BRIDGE_REQUIRED_TOOLS),
                    tool_count=len(tool_names),
                )
            )
    except Exception as exc:
        failures.append(f"mcp_tool_surface: {exc}")
        checks.append(bridge_check("mcp_tool_surface", "failed", error=str(exc)))

    return {
        "report": "codex-palace-bridge-verification",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "run_id": validate_run_id(args.run_id),
        "status": "passed" if not failures else "failed",
        "dry_run": True,
        "live_smoke_requested": bool(args.live_smoke),
        "scope": {"type": args.scope_type, **({"key": args.scope_key} if args.scope_key else {})},
        "workspace_key": args.workspace_key,
        "privacy": {
            "destructive_operations": False,
            "raw_secret_output": False,
            "raw_transcript_output": False,
            "production_writes_by_default": False,
        },
        "checks": checks,
        "failures": failures,
        "live_smoke_command": (
            setup_report.get("live_smoke_command")
            if "setup_report" in locals() and isinstance(setup_report, dict)
            else None
        ),
    }


def cmd_codex_bridge(args: argparse.Namespace) -> int:
    report = build_codex_bridge_report(args)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
    else:
        print(output, end="")
    if report["status"] != "passed":
        return 1
    if not args.live_smoke:
        return 0
    setup_module = _import_setup_module()
    setup_args = setup_module.build_parser().parse_args(
        [
            "--api-base-url",
            args.api_base_url,
            "--run-id",
            args.run_id,
            "--scope-type",
            args.scope_type,
            *([] if args.scope_key is None else ["--scope-key", args.scope_key]),
            "--live-smoke",
        ]
    )
    return setup_module.run_live_smoke(setup_args)


def _report_status(value: Any) -> str:
    if isinstance(value, dict):
        status = value.get("status")
        if isinstance(status, str):
            return status
        summary = value.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("passed"), bool):
            return "passed" if summary["passed"] else "failed"
    return "unknown"


def _startup_check(
    *,
    name: str,
    status: str,
    source: str,
    evidence_type: str = "direct_local",
    signals: dict[str, Any] | None = None,
    command: list[str] | None = None,
    warning: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "source": source,
        "evidence_type": evidence_type,
        **({"signals": signals} if signals else {}),
        **({"command": command} if command else {}),
        **({"warning": warning} if warning else {}),
    }


def _safe_count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _summarize_compatibility_fixture(args: argparse.Namespace) -> dict[str, Any]:
    benchmark_module = _import_benchmark_module()
    parser = benchmark_module.build_parser()
    compat_args = parser.parse_args(
        [
            "compatibility-report",
            "--pack",
            args.compatibility_pack,
            "--top-k",
            str(args.compatibility_top_k),
        ]
    )
    payload = benchmark_module.read_compatibility_fixture_eval_pack(Path(compat_args.pack))
    report = benchmark_module.evaluate_eval_pack(
        payload,
        top_k=compat_args.top_k,
        thresholds=benchmark_module.build_thresholds(compat_args),
    )
    summary = report.get("summary") or {}
    return {
        "status": "passed" if summary.get("passed") is True else "failed",
        "pack_id": payload.get("pack_id"),
        "case_count": _safe_count(payload, "cases"),
        "offline_report_only": (payload.get("artifact_metadata") or {}).get("offline_report_only"),
        "per_transport": summary.get("per_transport", {}),
        "forbidden_hit_count": summary.get("forbidden_hit_count"),
    }


def _task_pool_command(args: argparse.Namespace, status: str) -> list[str]:
    return [
        sys.executable,
        "/Users/asarver/.codex/project-manager/task_pool_ops.py",
        "list",
        "--automation-id",
        args.automation_id,
        "--status",
        status,
        "--limit",
        str(args.task_pool_limit),
    ]


def _task_pool_state(args: argparse.Namespace) -> dict[str, Any]:
    statuses = args.task_pool_status or list(DEFAULT_TASK_POOL_STATUSES)
    if not args.include_task_pool:
        return {
            "status": "skipped",
            "reason": "pass --include-task-pool to run read-only central task-pool listing",
            "statuses": list(statuses),
            "commands": [_task_pool_command(args, status) for status in statuses],
        }
    counts: dict[str, int] = {}
    failures: list[str] = []
    for status in statuses:
        command = _task_pool_command(args, status)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=args.task_pool_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{status}: {exc}")
            continue
        if result.returncode != 0:
            failures.append(f"{status}: exit {result.returncode}: {result.stderr.strip()[:240]}")
            continue
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            failures.append(f"{status}: invalid JSON: {exc}")
            continue
        counts[status] = int(payload.get("count") or 0)
    return {
        "status": "ok" if not failures else "failed",
        "counts": counts,
        "failures": failures,
        "read_only": True,
    }


def _http_get_status(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json,text/html,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(512)
            return {"status": "ok", "http_status": response.status}
    except urllib.error.HTTPError as exc:
        return {"status": "failed", "http_status": exc.code}
    except urllib.error.URLError as exc:
        return {"status": "failed", "error": str(exc)}


def _live_deploy_state(args: argparse.Namespace) -> dict[str, Any]:
    if not args.include_live_deploy:
        return {
            "status": "skipped",
            "reason": "pass --include-live-deploy for explicit read-only HTTP health checks",
            "frontend_url": args.frontend_url,
            "api_health_url": args.api_health_url,
        }
    frontend = _http_get_status(args.frontend_url, args.live_timeout)
    api = _http_get_status(args.api_health_url, args.live_timeout)
    status = "ok" if frontend.get("status") == "ok" and api.get("status") == "ok" else "failed"
    return {
        "status": status,
        "frontend": frontend,
        "api_health": api,
        "read_only": True,
    }


def _git_coordinates() -> dict[str, Any]:
    def run_git(*parts: str) -> str:
        result = subprocess.run(
            ["git", *parts],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip() or "unknown"

    return {
        "branch": run_git("rev-parse", "--abbrev-ref", "HEAD"),
        "head": run_git("rev-parse", "HEAD"),
        "dirty": bool(run_git("status", "--porcelain")),
    }


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            is_secret_key = (
                key_text.isupper() and SENSITIVE_ENV_RE.search(key_text) is not None
            ) or key_lower in {"api_key", "token", "secret", "password", "credential"}
            if is_secret_key or key == "body":
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_sensitive_values(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    if isinstance(value, str) and SENSITIVE_ENV_RE.search(value):
        return "<redacted>"
    return value


def build_startup_context_report(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_run_id(args.run_id or default_run_id())
    bridge_args = argparse.Namespace(
        api_base_url=args.api_base_url,
        run_id=run_id,
        scope_type="agent",
        scope_key=args.agent_scope_key,
        workspace_key=args.workspace_key,
        session_key=args.session_key,
        live_smoke=False,
    )
    scorecard_args_ns = argparse.Namespace(
        api_base_url=args.api_base_url,
        api_key=args.api_key,
        run_id=run_id[:39],
        transport=[],
        scope_type="workspace",
        scope_key=args.workspace_key,
        relationship_policy="deferred",
        request_timeout=30.0,
        sse_read_timeout=300.0,
        job_interval_seconds=5.0,
        job_timeout_seconds=300.0,
        backfill_limit=1,
        backfill_defer_seconds=0,
        skip_backfill=True,
        wakeup_scope_type="tenant",
        wakeup_scope_key=None,
        skip_wakeup_brief=False,
        fail_missing_wakeup_brief=False,
        active_skill=[],
        mcp_url=DEFAULT_MCP_URL,
        header=[],
        stdio_command="uv",
        stdio_arg=[],
        stdio_cwd=str(DEFAULT_REPO_ROOT),
        env=[],
        dry_run=True,
    )

    bridge = build_codex_bridge_report(bridge_args)
    scorecard = build_agent_memory_scorecard(scorecard_args_ns)
    compatibility = _summarize_compatibility_fixture(args)
    task_pool = _task_pool_state(args)
    deploy = _live_deploy_state(args)

    wakeup_tool_ready = any(
        check.get("name") == "mcp_tool_surface" and check.get("status") == "ok"
        for check in bridge.get("checks") or []
    )
    checks = [
        _startup_check(
            name="central_task_pool",
            status=task_pool["status"],
            source="task_pool_ops.py",
            evidence_type="direct_local" if args.include_task_pool else "explicit_opt_in_required",
            signals=task_pool,
        ),
        _startup_check(
            name="wake_up_route",
            status="ready" if wakeup_tool_ready else "warning",
            source="codex-bridge dry-run",
            signals={
                "tool": "get_wakeup_context",
                "tool_surface_ready": wakeup_tool_ready,
                "generated_synthesis_authoritative": False,
            },
        ),
        _startup_check(
            name="codex_bridge",
            status=_report_status(bridge),
            source="build_codex_bridge_report",
            signals={
                "dry_run": bridge.get("dry_run"),
                "live_smoke_requested": bridge.get("live_smoke_requested"),
                "failure_count": len(bridge.get("failures") or []),
            },
        ),
        _startup_check(
            name="agent_memory_scorecard",
            status=_report_status(scorecard),
            source="scorecard --dry-run",
            signals={
                "score": scorecard.get("score"),
                "max_score": scorecard.get("max_score"),
                "transport_count": _safe_count(scorecard, "results"),
            },
        ),
        _startup_check(
            name="offline_compatibility_fixture",
            status=compatibility["status"],
            source="compatibility-report",
            signals=compatibility,
        ),
        _startup_check(
            name="live_deploy_health",
            status=deploy["status"],
            source="read-only HTTP health checks",
            evidence_type="explicit_opt_in_required" if not args.include_live_deploy else "direct_live_read_only",
            signals=deploy,
        ),
    ]
    warning_statuses = {"failed", "warning", "skipped"}
    readiness_warnings = [
        f"{check['name']}: {check['status']}"
        for check in checks
        if check.get("status") in warning_statuses
    ]
    failed = [check for check in checks if check.get("status") == "failed"]
    report = {
        "report": "palace-startup-context-evidence-refresh",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "run_id": run_id,
        "status": "failed" if failed else "ready",
        "dry_run": True,
        "read_only": True,
        "current_chamber": {
            "workspace_key": args.workspace_key,
            "agent_scope_key": args.agent_scope_key,
            "session_key": args.session_key,
            "repo": _git_coordinates(),
        },
        "privacy": {
            "writes_memory": False,
            "queues_backfill": False,
            "deletes_data": False,
            "retries_jobs": False,
            "admin_actions": False,
            "raw_memory_content_reported": False,
            "raw_secret_output": False,
            "live_network_by_default": False,
        },
        "wake_up_route": {
            "tool": "get_wakeup_context",
            "status": "ready" if wakeup_tool_ready else "warning",
            "source": "direct local MCP tool-surface and lifecycle-payload evidence",
        },
        "room_source_evidence": checks,
        "readiness_warnings": readiness_warnings,
    }
    return _redact_sensitive_values(report)


def cmd_startup_context_report(args: argparse.Namespace) -> int:
    report = build_startup_context_report(args)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
    else:
        print(output, end="")
    return 0 if report["status"] == "ready" else 1


def scorecard_args(args: argparse.Namespace, *, transport: str, run_id: str) -> argparse.Namespace:
    common = {
        "api_base_url": args.api_base_url,
        "api_key": args.api_key,
        "run_id": run_id,
        "scope_type": args.scope_type,
        "scope_key": args.scope_key,
        "request_timeout": args.request_timeout,
        "job_interval_seconds": args.job_interval_seconds,
        "job_timeout_seconds": args.job_timeout_seconds,
        "skip_wakeup_brief": args.skip_wakeup_brief,
        "fail_missing_wakeup_brief": args.fail_missing_wakeup_brief,
        "active_skill": args.active_skill,
        "dry_run": args.dry_run,
    }
    if transport == "rest":
        return argparse.Namespace(**common, relationship_policy="immediate")
    mcp_common = {
        **common,
        "relationship_policy": args.relationship_policy,
        "backfill_limit": args.backfill_limit,
        "backfill_defer_seconds": args.backfill_defer_seconds,
        "skip_backfill": args.skip_backfill,
        "wakeup_scope_type": args.wakeup_scope_type,
        "wakeup_scope_key": args.wakeup_scope_key,
    }
    if transport == "mcp-http":
        return argparse.Namespace(
            **mcp_common,
            mcp_url=args.mcp_url,
            header=args.header,
            sse_read_timeout=args.sse_read_timeout,
        )
    return argparse.Namespace(
        **mcp_common,
        stdio_command=args.stdio_command,
        stdio_arg=args.stdio_arg,
        stdio_cwd=args.stdio_cwd,
        env=args.env,
    )


def dry_run_smoke_plan(transport: str, args: argparse.Namespace) -> dict[str, Any]:
    entry_tenant = "dry-run-tenant" if transport == "rest" else "mcp-managed-tenant"
    entry = make_memory_entry(
        tenant_id=entry_tenant,
        run_id=args.run_id,
        scope_type=args.scope_type,
        scope_key=args.scope_key,
        relationship_policy=args.relationship_policy,
        active_skills=args.active_skill,
    )
    plan: dict[str, Any] = {
        "dry_run": True,
        "transport": transport,
        "run_id": args.run_id,
        "matrix": SMOKE_MATRIX,
        "entry": entry,
    }
    if transport == "mcp-http":
        plan.update(
            {
                "mcp_url": args.mcp_url,
                "create_memory_entry_arguments": mcp_create_memory_arguments(entry),
                "list_memory_entries_arguments": mcp_list_memory_entries_arguments(entry, args.run_id),
            }
        )
    elif transport == "mcp-stdio":
        plan.update(
            {
                "stdio_command": args.stdio_command,
                "stdio_args": stdio_args_for(args),
                "stdio_cwd": args.stdio_cwd,
                "create_memory_entry_arguments": mcp_create_memory_arguments(entry),
                "list_memory_entries_arguments": mcp_list_memory_entries_arguments(entry, args.run_id),
            }
        )
    return plan


def run_transport_scorecard(args: argparse.Namespace, transport: str, run_id: str) -> dict[str, Any]:
    transport_args = scorecard_args(args, transport=transport, run_id=run_id)
    try:
        if args.dry_run:
            smoke_result = dry_run_smoke_plan(transport, transport_args)
            score = score_smoke_result(
                transport,
                {"steps": {name: {"status": "ok"} for name in REQUIRED_SCORE_STEPS[transport]}},
            )
        elif transport == "rest":
            smoke_result = run_rest_smoke(transport_args)
            score = score_smoke_result(transport, smoke_result)
        elif transport == "mcp-http":
            smoke_result = run_mcp_smoke(transport_args)
            score = score_smoke_result(transport, smoke_result)
        else:
            smoke_result = run_mcp_stdio_smoke(transport_args)
            score = score_smoke_result(transport, smoke_result)
    except (ApiError, RuntimeError, SystemExit) as exc:
        smoke_result = None
        score = score_smoke_result(transport, None, str(exc))
    return {"transport": transport, "run_id": run_id, "score": score, "result": smoke_result}


def build_agent_memory_scorecard(args: argparse.Namespace) -> dict[str, Any]:
    base_run_id = validate_run_id(args.run_id or default_run_id())
    transports = args.transport or list(SCORECARD_TRANSPORTS)
    results = [
        run_transport_scorecard(args, transport, transport_run_id(base_run_id, transport))
        for transport in transports
    ]
    max_score = sum(item["score"]["max_score"] for item in results)
    score = sum(item["score"]["score"] for item in results)
    failures = [
        {
            "transport": item["transport"],
            "error": item["score"].get("error"),
            "failed_steps": [
                name
                for name, step in item["score"]["required_steps"].items()
                if step["status"] != "passed"
            ],
        }
        for item in results
        if item["score"]["status"] != "passed"
    ]
    return {
        "report": "agent-memory-compatibility-scorecard",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "base_run_id": base_run_id,
        "status": "passed" if not failures else "failed",
        "score": score,
        "max_score": max_score,
        "score_percent": round((score / max_score) * 100, 2) if max_score else 0.0,
        "privacy": {
            "destructive_operations": False,
            "cleanup_automation": False,
            "raw_memory_content_reported": False,
        },
        "results": results,
        "failures": failures,
    }


def run_readiness_live_smoke(args: argparse.Namespace) -> dict[str, Any]:
    # Reuse the existing bounded REST smoke. It writes one deterministic scoped
    # memory and never calls admin, retry, delete, cleanup, or relationship backfill.
    smoke_args = argparse.Namespace(
        api_base_url=args.api_base_url,
        api_key=args.api_key,
        run_id=args.run_id,
        scope_type=args.scope_type,
        scope_key=args.scope_key,
        relationship_policy="immediate",
        request_timeout=args.request_timeout,
        job_interval_seconds=args.job_interval_seconds,
        job_timeout_seconds=args.job_timeout_seconds,
        skip_wakeup_brief=args.skip_wakeup_brief,
        fail_missing_wakeup_brief=args.fail_missing_wakeup_brief,
    )
    return run_rest_smoke(smoke_args)


def build_operator_readiness_report(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = utc_now().isoformat().replace("+00:00", "Z")
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    report: dict[str, Any] = {
        "report": "palace-operator-readiness",
        "generated_at": generated_at,
        "run_id": validate_run_id(args.run_id or default_run_id()),
        "api_base_url": args.api_base_url,
        "read_only": not args.live_smoke,
        "checks": checks,
        "env": env_diagnostics(args),
    }

    if args.dry_run:
        checks.append(readiness_check("dry_run", "ok", live_smoke=args.live_smoke))
        report["status"] = "dry-run"
        report["failure_count"] = 0
        report["failures"] = []
        return report

    api_key = (
        args.api_key
        or os.getenv("PALACEOFTRUTH_API_KEY")
        or os.getenv("SECONDBRAIN_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise SystemExit("--api-key, PALACEOFTRUTH_API_KEY, or SECONDBRAIN_API_KEY is required")

    client = Client(base_url=args.api_base_url, api_key=api_key)
    health = client.request("GET", "/api/v1/health", timeout=args.request_timeout)
    checks.append(readiness_check("api_health", "ok", response=health))

    whoami = client.request("GET", "/api/v1/memory/whoami", timeout=args.request_timeout)
    if not isinstance(whoami, dict) or not whoami.get("tenant_id"):
        raise RuntimeError(f"whoami did not return tenant_id: {whoami}")
    report["tenant_id"] = whoami["tenant_id"]
    checks.append(readiness_check("tenant_identity", "ok", tenant_id=whoami.get("tenant_id")))

    stats = client.request("GET", "/api/v1/stats", timeout=args.request_timeout)
    if not isinstance(stats, dict):
        raise RuntimeError(f"/stats response was not an object: {stats}")
    checks.append(readiness_check("stats", "ok", summary=analyze_stats(stats, failures)))

    control_tower = client.request("GET", "/api/v1/palace/control-tower", timeout=args.request_timeout)
    if not isinstance(control_tower, dict):
        raise RuntimeError("Control Tower response was not an object")
    checks.append(readiness_check("control_tower", "ok", summary=analyze_control_tower(control_tower, failures)))

    jobs = client.request(
        "GET",
        "/api/v1/memory/jobs",
        query={"page": 1, "per_page": 10},
        timeout=args.request_timeout,
    )
    if not isinstance(jobs, dict) or "jobs" not in jobs:
        raise RuntimeError(f"memory jobs listing response was not valid: {jobs}")
    checks.append(
        readiness_check(
            "memory_jobs",
            "ok",
            returned=len(jobs.get("jobs") or []),
            total=jobs.get("total"),
        )
    )
    checks.append(
        readiness_check(
            "mcp_config",
            "ok",
            summary={
                "mcp_url": getattr(args, "mcp_url", DEFAULT_MCP_URL),
                "stdio_command": getattr(args, "stdio_command", "uv"),
                "stdio_args": stdio_args_for(args),
            },
        )
    )

    if args.skip_retained_corpus:
        checks.append(readiness_check("retained_corpus", "skipped", reason="--skip-retained-corpus"))
    elif not args.nist_run_id:
        _append_failure(failures, "retained_corpus", "at least one --nist-run-id is required")
        checks.append(readiness_check("retained_corpus", "failed", reason="missing --nist-run-id"))
    else:
        retained = summarize_retained_corpus(args.nist_run_id, failures)
        report["retained_corpus"] = retained
        checks.append(readiness_check("retained_corpus", "ok", run_ids=args.nist_run_id))

    if args.live_smoke:
        live_smoke = run_readiness_live_smoke(args)
        trace_failures = strict_retrieve_trace_failures(live_smoke)
        for failure in trace_failures:
            _append_failure(failures, "live_smoke", failure)
        report["live_smoke"] = live_smoke
        checks.append(readiness_check("live_smoke", "ok" if not trace_failures else "failed"))
    else:
        checks.append(readiness_check("live_smoke", "skipped", reason="read-only default"))

    report["failure_count"] = len(failures)
    report["failures"] = failures
    report["status"] = "ready" if not failures else "failed"
    return report


def format_operator_readiness_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Palace Operator Readiness Report",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"Run id: `{report['run_id']}`",
        f"API: `{report['api_base_url']}`",
        f"Status: `{report['status']}`",
        f"Read-only: `{_markdown_bool(report['read_only'])}`",
        "",
        "## Checks",
        "",
        "| check | status | detail |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        detail = check.get("reason") or check.get("tenant_id") or check.get("returned") or ""
        if check.get("summary") is not None:
            detail = json.dumps(check["summary"], sort_keys=True)
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_markdown_table(check["name"]),
                    _escape_markdown_table(check["status"]),
                    _escape_markdown_table(str(detail)),
                )
            )
            + " |"
        )
    if report.get("failures"):
        lines.extend(["", "## Failures", ""])
        lines.extend(f"- {failure}" for failure in report["failures"])
    return "\n".join(lines) + "\n"


def format_activation_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Palace Activation Onboarding Report",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"Run id: `{report['run_id']}`",
        f"API: `{report['api_base_url']}`",
        f"Status: `{report['status']}`",
        f"Score: `{report['score']}/{report['max_score']} ({report['score_percent']}%)`",
        f"Read-only: `{_markdown_bool(report['read_only'])}`",
        "",
        "## Categories",
        "",
        "| category | status | score | signals |",
        "| --- | --- | --- | --- |",
    ]
    for category in report["categories"]:
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_markdown_table(category["name"]),
                    _escape_markdown_table(category["status"]),
                    _escape_markdown_table(f"{category['score']}/{category['max_score']}"),
                    _escape_markdown_table(json.dumps(category.get("signals", {}), sort_keys=True)),
                )
            )
            + " |"
        )
    if report.get("remediations"):
        lines.extend(["", "## Remediations", ""])
        lines.extend(f"- {remediation}" for remediation in report["remediations"])
    return "\n".join(lines) + "\n"


def _markdown_bool(value: Any) -> str:
    return "yes" if value else "no"


def _escape_markdown_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def cmd_operator_readiness(args: argparse.Namespace) -> int:
    report = build_operator_readiness_report(args)
    if args.format == "json":
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        output = format_operator_readiness_markdown(report)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
    else:
        print(output, end="")
    return 0 if report["status"] in {"ready", "dry-run"} else 1


def cmd_activation_onboarding(args: argparse.Namespace) -> int:
    report = build_activation_onboarding_report(args)
    if args.format == "json":
        output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    else:
        output = format_activation_report_markdown(report)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
    else:
        print(output, end="")
    return 0 if report["status"] in {"activated", "dry-run"} else 1


def cmd_matrix(_: argparse.Namespace) -> int:
    print(json.dumps({"matrix": SMOKE_MATRIX}, indent=2))
    return 0


def cmd_rest(args: argparse.Namespace) -> int:
    if args.dry_run:
        run_id = validate_run_id(args.run_id or default_run_id())
        entry = make_memory_entry(
            tenant_id="dry-run-tenant",
            run_id=run_id,
            scope_type=args.scope_type,
            scope_key=args.scope_key,
            relationship_policy=args.relationship_policy,
            active_skills=args.active_skill,
        )
        print(json.dumps({"dry_run": True, "matrix": SMOKE_MATRIX, "entry": entry}, indent=2))
        return 0
    print(json.dumps(run_rest_smoke(args), indent=2))
    return 0


def cmd_mcp_http(args: argparse.Namespace) -> int:
    if args.dry_run:
        run_id = validate_run_id(args.run_id or default_run_id())
        entry = make_memory_entry(
            tenant_id="mcp-managed-tenant",
            run_id=run_id,
            scope_type=args.scope_type,
            scope_key=args.scope_key,
            relationship_policy=args.relationship_policy,
            active_skills=args.active_skill,
        )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "matrix": SMOKE_MATRIX,
                    "mcp_url": args.mcp_url,
                    "create_memory_entry_arguments": mcp_create_memory_arguments(entry),
                    "list_memory_entries_arguments": mcp_list_memory_entries_arguments(entry, run_id),
                    "backfill_deferred_relationships_arguments": (
                        None
                        if args.relationship_policy != "deferred" or args.skip_backfill
                        else {
                            "limit": args.backfill_limit,
                            "defer_seconds": args.backfill_defer_seconds,
                        }
                    ),
                },
                indent=2,
            )
        )
        return 0
    print(json.dumps(run_mcp_smoke(args), indent=2))
    return 0


def cmd_mcp_stdio(args: argparse.Namespace) -> int:
    command_args = stdio_args_for(args)
    if args.dry_run:
        run_id = validate_run_id(args.run_id or default_run_id())
        entry = make_memory_entry(
            tenant_id="mcp-managed-tenant",
            run_id=run_id,
            scope_type=args.scope_type,
            scope_key=args.scope_key,
            relationship_policy=args.relationship_policy,
            active_skills=args.active_skill,
        )
        env_overrides = parse_env_overrides(args.env)
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "matrix": SMOKE_MATRIX,
                    "transport": "stdio",
                    "stdio_command": args.stdio_command,
                    "stdio_args": command_args,
                    "stdio_cwd": args.stdio_cwd,
                    "env": {
                        "PALACEOFTRUTH_API_BASE_URL": args.api_base_url,
                        **{key: ("<redacted>" if "KEY" in key or "TOKEN" in key else value) for key, value in env_overrides.items()},
                    },
                    "create_memory_entry_arguments": mcp_create_memory_arguments(entry),
                    "list_memory_entries_arguments": mcp_list_memory_entries_arguments(entry, run_id),
                    "backfill_deferred_relationships_arguments": (
                        None
                        if args.relationship_policy != "deferred" or args.skip_backfill
                        else {
                            "limit": args.backfill_limit,
                            "defer_seconds": args.backfill_defer_seconds,
                        }
                    ),
                },
                indent=2,
            )
        )
        return 0
    print(json.dumps(run_mcp_stdio_smoke(args), indent=2))
    return 0


def redact_memory_bodies(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted memory body>" if key == "body" else redact_memory_bodies(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_memory_bodies(item) for item in value]
    return value


def cmd_scorecard(args: argparse.Namespace) -> int:
    report = build_agent_memory_scorecard(args)
    if args.redact_memory_bodies:
        report = redact_memory_bodies(report)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
    else:
        print(output, end="")
    return 0 if report["status"] == "passed" else 1


def cmd_codex_memory_health(args: argparse.Namespace) -> int:
    report = run_codex_memory_health(args)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output)
    else:
        print(output, end="")
    return 0 if report["status"] == "passed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("PALACEOFTRUTH_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    parser.add_argument("--api-key", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    matrix = sub.add_parser("matrix", help="Print the REST/MCP compatibility smoke matrix.")
    matrix.set_defaults(func=cmd_matrix)

    codex_bridge = sub.add_parser(
        "codex-bridge",
        help=(
            "Verify the Codex-facing Palace bridge contract. Dry-run/read-only by "
            "default; use --live-smoke to additionally run the one-memory stdio smoke."
        ),
    )
    codex_bridge.add_argument("--run-id", default="codex-bridge")
    codex_bridge.add_argument(
        "--scope-type",
        choices=["session", "agent", "workspace", "tenant_shared"],
        default="agent",
    )
    codex_bridge.add_argument("--scope-key", default="codex")
    codex_bridge.add_argument("--workspace-key", default="palaceoftruth")
    codex_bridge.add_argument("--session-key", default=None)
    codex_bridge.add_argument("--live-smoke", action="store_true")
    codex_bridge.add_argument("--output", default=None)
    codex_bridge.set_defaults(func=cmd_codex_bridge)

    startup = sub.add_parser(
        "startup-context-report",
        help=(
            "Compose a report-only Palace startup-context evidence refresh for "
            "DOTODO and improvement-planning runs."
        ),
    )
    startup.add_argument("--run-id", default=None)
    startup.add_argument("--automation-id", default="dotodo-palace-of-truth")
    startup.add_argument("--workspace-key", default="palaceoftruth")
    startup.add_argument("--agent-scope-key", default="codex")
    startup.add_argument("--session-key", default=None)
    startup.add_argument(
        "--compatibility-pack",
        default=str(DEFAULT_REPO_ROOT / "backend" / "tests" / "fixtures" / "agent_memory_compatibility_fixture_pack.json"),
    )
    startup.add_argument("--compatibility-top-k", type=int, default=5)
    startup.add_argument(
        "--include-task-pool",
        action="store_true",
        help="Run read-only central task-pool listings. Default only prints command previews.",
    )
    startup.add_argument(
        "--task-pool-status",
        action="append",
        choices=list(DEFAULT_TASK_POOL_STATUSES),
        default=None,
    )
    startup.add_argument("--task-pool-limit", type=int, default=20)
    startup.add_argument("--task-pool-timeout", type=float, default=30.0)
    startup.add_argument(
        "--include-live-deploy",
        action="store_true",
        help="Run explicit read-only HTTP checks against the configured frontend/API URLs.",
    )
    startup.add_argument("--frontend-url", default="https://palace.sarvent.cloud/")
    startup.add_argument("--api-health-url", default="https://api.palace.sarvent.cloud/api/v1/health")
    startup.add_argument("--live-timeout", type=float, default=10.0)
    startup.add_argument("--output", default=None)
    startup.set_defaults(func=cmd_startup_context_report)

    scorecard = sub.add_parser(
        "scorecard",
        help="Run a scored agent-memory compatibility benchmark across REST, HTTP MCP, and stdio MCP.",
    )
    scorecard.add_argument("--run-id", default=None)
    scorecard.add_argument(
        "--transport",
        action="append",
        choices=list(SCORECARD_TRANSPORTS),
        default=[],
        help="Transport to include. Repeatable. Defaults to rest, mcp-http, and mcp-stdio.",
    )
    scorecard.add_argument(
        "--scope-type",
        choices=["session", "agent", "workspace", "tenant_shared"],
        default="workspace",
    )
    scorecard.add_argument("--scope-key", default="agent-memory-smoke")
    scorecard.add_argument("--relationship-policy", choices=["immediate", "deferred"], default="deferred")
    scorecard.add_argument("--request-timeout", type=float, default=30.0)
    scorecard.add_argument("--sse-read-timeout", type=float, default=300.0)
    scorecard.add_argument("--job-interval-seconds", type=float, default=5.0)
    scorecard.add_argument("--job-timeout-seconds", type=float, default=300.0)
    scorecard.add_argument("--backfill-limit", type=int, default=1)
    scorecard.add_argument("--backfill-defer-seconds", type=int, default=0)
    scorecard.add_argument("--skip-backfill", action="store_true")
    scorecard.add_argument("--wakeup-scope-type", choices=["tenant", "wing"], default="tenant")
    scorecard.add_argument("--wakeup-scope-key", default=None)
    scorecard.add_argument("--skip-wakeup-brief", action="store_true")
    scorecard.add_argument("--fail-missing-wakeup-brief", action="store_true")
    scorecard.add_argument(
        "--active-skill",
        action="append",
        default=[],
        help="Active skill name to write as metadata and filter as a deterministic skill-* system tag. Repeatable.",
    )
    scorecard.add_argument(
        "--mcp-url",
        default=os.getenv("PALACEOFTRUTH_MCP_URL", os.getenv("SECONDBRAIN_MCP_URL", DEFAULT_MCP_URL)),
    )
    scorecard.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header for the MCP endpoint, as NAME=VALUE or NAME:VALUE. Repeatable.",
    )
    scorecard.add_argument("--stdio-command", default="uv")
    scorecard.add_argument(
        "--stdio-arg",
        action="append",
        default=[],
        help=(
            "Argument for the stdio server command. Repeatable. Defaults to "
            "launching scripts/palaceoftruth_mcp.py from the backend uv project."
        ),
    )
    scorecard.add_argument("--stdio-cwd", default=str(DEFAULT_REPO_ROOT))
    scorecard.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment override for the stdio MCP server, as NAME=VALUE. Repeatable.",
    )
    scorecard.add_argument("--dry-run", action="store_true")
    scorecard.add_argument("--output", default=None)
    scorecard.add_argument(
        "--redact-memory-bodies",
        action="store_true",
        help="Replace memory body fields in the JSON report before printing or writing it.",
    )
    scorecard.set_defaults(func=cmd_scorecard)

    rest = sub.add_parser("rest", help="Run the canonical REST compatibility smoke.")
    rest.add_argument("--run-id", default=None)
    rest.add_argument("--scope-type", choices=["session", "agent", "workspace", "tenant_shared"], default="workspace")
    rest.add_argument("--scope-key", default="agent-memory-smoke")
    rest.add_argument("--relationship-policy", choices=["immediate", "deferred"], default="immediate")
    rest.add_argument("--request-timeout", type=float, default=30.0)
    rest.add_argument("--job-interval-seconds", type=float, default=5.0)
    rest.add_argument("--job-timeout-seconds", type=float, default=300.0)
    rest.add_argument("--skip-wakeup-brief", action="store_true")
    rest.add_argument("--fail-missing-wakeup-brief", action="store_true")
    rest.add_argument(
        "--active-skill",
        action="append",
        default=[],
        help="Active skill name to write as metadata and filter as a deterministic skill-* system tag. Repeatable.",
    )
    rest.add_argument("--dry-run", action="store_true")
    rest.set_defaults(func=cmd_rest)

    incident_doctor = sub.add_parser(
        "incident-retrieval-doctor",
        help="Run a read-only FeedValue/Receipt Shelf retrieval doctor probe without exposing memory content.",
    )
    incident_doctor.add_argument("--agent-scope-key", default="coder")
    incident_doctor.add_argument("--request-timeout", type=float, default=30.0)
    incident_doctor.add_argument("--candidate-limit", type=int, default=10)
    incident_doctor.add_argument("--display-limit", type=int, default=5)
    incident_doctor.add_argument("--probe-limit", type=int, default=5)
    incident_doctor.set_defaults(func=cmd_incident_retrieval_doctor)

    codex_health = sub.add_parser(
        "codex-memory-health",
        help="Run a read-only post-deploy health smoke for the Codex Palace memory path.",
    )
    codex_health.add_argument("--agent-scope-key", default="codex")
    codex_health.add_argument(
        "--workspace-scope-key",
        action="append",
        default=None,
        help="Workspace scope to include in route-aware recall. Repeatable.",
    )
    codex_health.add_argument(
        "--query",
        default="Codex Palace MCP memory integration test",
        help="Harmless semantic query expected to recall existing Codex memory.",
    )
    codex_health.add_argument("--limit", type=int, default=5)
    codex_health.add_argument("--display-limit", type=int, default=5)
    codex_health.add_argument("--context-budget-chars", type=int, default=1200)
    codex_health.add_argument("--scope-limit", type=int, default=100)
    codex_health.add_argument("--entry-limit", type=int, default=5)
    codex_health.add_argument("--request-timeout", type=float, default=30.0)
    codex_health.add_argument("--output", default=None)
    codex_health.set_defaults(func=cmd_codex_memory_health)

    activation = sub.add_parser(
        "activation-report",
        help="Build a read-only Palace activation/onboarding score report with remediation recommendations.",
    )
    activation.add_argument("--run-id", default=None)
    activation.add_argument("--agent-scope-key", default="codex")
    activation.add_argument(
        "--workspace-scope-key",
        action="append",
        default=None,
        help="Workspace scope that should be ready for activation. Repeatable. Defaults to palaceoftruth.",
    )
    activation.add_argument(
        "--activation-query",
        default="Palace operator onboarding readiness and recent project decisions",
        help="Harmless query used for read-only trajectory and retrieval-doctor probes.",
    )
    activation.add_argument("--target-score-percent", type=float, default=80.0)
    activation.add_argument("--scope-limit", type=int, default=100)
    activation.add_argument("--candidate-limit", type=int, default=10)
    activation.add_argument("--display-limit", type=int, default=5)
    activation.add_argument("--context-budget-chars", type=int, default=1200)
    activation.add_argument("--request-timeout", type=float, default=30.0)
    activation.add_argument("--job-interval-seconds", type=float, default=5.0)
    activation.add_argument("--job-timeout-seconds", type=float, default=300.0)
    activation.add_argument("--skip-wakeup-brief", action="store_true")
    activation.add_argument("--fail-missing-wakeup-brief", action="store_true")
    activation.add_argument("--nist-run-id", action="append", default=[])
    activation.add_argument("--skip-retained-corpus", action="store_true")
    activation.add_argument(
        "--mcp-url",
        default=os.getenv("PALACEOFTRUTH_MCP_URL", os.getenv("SECONDBRAIN_MCP_URL", DEFAULT_MCP_URL)),
    )
    activation.add_argument("--stdio-command", default="uv")
    activation.add_argument("--stdio-arg", action="append", default=[])
    activation.add_argument("--stdio-cwd", default=str(DEFAULT_REPO_ROOT))
    activation.add_argument(
        "--live-smoke",
        action="store_true",
        help="Also run the existing one-memory live smoke inside operator-readiness.",
    )
    activation.add_argument("--dry-run", action="store_true")
    activation.add_argument("--format", choices=["json", "markdown"], default="json")
    activation.add_argument("--output", default=None)
    activation.set_defaults(func=cmd_activation_onboarding)

    mcp_http = sub.add_parser("mcp-http", help="Run the streamable HTTP MCP compatibility smoke.")
    mcp_http.add_argument(
        "--mcp-url",
        default=os.getenv("PALACEOFTRUTH_MCP_URL", os.getenv("SECONDBRAIN_MCP_URL", DEFAULT_MCP_URL)),
    )
    mcp_http.add_argument(
        "--header",
        action="append",
        default=[],
        help="Extra HTTP header for the MCP endpoint, as NAME=VALUE or NAME:VALUE. Repeatable.",
    )
    mcp_http.add_argument("--run-id", default=None)
    mcp_http.add_argument("--scope-type", choices=["session", "agent", "workspace", "tenant_shared"], default="workspace")
    mcp_http.add_argument("--scope-key", default="agent-memory-smoke")
    mcp_http.add_argument("--relationship-policy", choices=["immediate", "deferred"], default="deferred")
    mcp_http.add_argument("--request-timeout", type=float, default=30.0)
    mcp_http.add_argument("--sse-read-timeout", type=float, default=300.0)
    mcp_http.add_argument("--job-interval-seconds", type=float, default=5.0)
    mcp_http.add_argument("--job-timeout-seconds", type=float, default=300.0)
    mcp_http.add_argument("--backfill-limit", type=int, default=1)
    mcp_http.add_argument("--backfill-defer-seconds", type=int, default=0)
    mcp_http.add_argument("--skip-backfill", action="store_true")
    mcp_http.add_argument("--wakeup-scope-type", choices=["tenant", "wing"], default="tenant")
    mcp_http.add_argument("--wakeup-scope-key", default=None)
    mcp_http.add_argument("--skip-wakeup-brief", action="store_true")
    mcp_http.add_argument("--fail-missing-wakeup-brief", action="store_true")
    mcp_http.add_argument(
        "--active-skill",
        action="append",
        default=[],
        help="Active skill name to write as metadata and filter as a deterministic skill-* system tag. Repeatable.",
    )
    mcp_http.add_argument("--dry-run", action="store_true")
    mcp_http.set_defaults(func=cmd_mcp_http)

    mcp_stdio = sub.add_parser("mcp-stdio", help="Run the local stdio MCP compatibility smoke.")
    mcp_stdio.add_argument("--run-id", default=None)
    mcp_stdio.add_argument("--scope-type", choices=["session", "agent", "workspace", "tenant_shared"], default="workspace")
    mcp_stdio.add_argument("--scope-key", default="agent-memory-smoke")
    mcp_stdio.add_argument("--relationship-policy", choices=["immediate", "deferred"], default="deferred")
    mcp_stdio.add_argument("--request-timeout", type=float, default=30.0)
    mcp_stdio.add_argument("--job-interval-seconds", type=float, default=5.0)
    mcp_stdio.add_argument("--job-timeout-seconds", type=float, default=300.0)
    mcp_stdio.add_argument("--backfill-limit", type=int, default=1)
    mcp_stdio.add_argument("--backfill-defer-seconds", type=int, default=0)
    mcp_stdio.add_argument("--skip-backfill", action="store_true")
    mcp_stdio.add_argument("--wakeup-scope-type", choices=["tenant", "wing"], default="tenant")
    mcp_stdio.add_argument("--wakeup-scope-key", default=None)
    mcp_stdio.add_argument("--skip-wakeup-brief", action="store_true")
    mcp_stdio.add_argument("--fail-missing-wakeup-brief", action="store_true")
    mcp_stdio.add_argument(
        "--active-skill",
        action="append",
        default=[],
        help="Active skill name to write as metadata and filter as a deterministic skill-* system tag. Repeatable.",
    )
    mcp_stdio.add_argument("--stdio-command", default="uv")
    mcp_stdio.add_argument(
        "--stdio-arg",
        action="append",
        default=[],
        help="Argument for the stdio server command. Repeatable. Defaults to launching scripts/palaceoftruth_mcp.py from the backend uv project.",
    )
    mcp_stdio.add_argument("--stdio-cwd", default=str(DEFAULT_REPO_ROOT))
    mcp_stdio.add_argument(
        "--env",
        action="append",
        default=[],
        help="Environment override for the stdio MCP server, as NAME=VALUE. Repeatable.",
    )
    mcp_stdio.add_argument("--dry-run", action="store_true")
    mcp_stdio.set_defaults(func=cmd_mcp_stdio)

    readiness = sub.add_parser(
        "operator-readiness",
        help="Build a strict Palace first-use readiness report; read-only unless --live-smoke is set.",
    )
    readiness.add_argument("--run-id", default=None)
    readiness.add_argument(
        "--scope-type",
        choices=["session", "agent", "workspace", "tenant_shared"],
        default="workspace",
    )
    readiness.add_argument("--scope-key", default="agent-memory-smoke")
    readiness.add_argument("--request-timeout", type=float, default=30.0)
    readiness.add_argument("--job-interval-seconds", type=float, default=5.0)
    readiness.add_argument("--job-timeout-seconds", type=float, default=300.0)
    readiness.add_argument("--skip-wakeup-brief", action="store_true")
    readiness.add_argument("--fail-missing-wakeup-brief", action="store_true")
    readiness.add_argument(
        "--nist-run-id",
        action="append",
        default=[],
        help="Retained NIST benchmark run id whose local artifacts should be included. Repeatable.",
    )
    readiness.add_argument("--skip-retained-corpus", action="store_true")
    readiness.add_argument(
        "--mcp-url",
        default=os.getenv("PALACEOFTRUTH_MCP_URL", os.getenv("SECONDBRAIN_MCP_URL", DEFAULT_MCP_URL)),
    )
    readiness.add_argument("--stdio-command", default="uv")
    readiness.add_argument("--stdio-arg", action="append", default=[])
    readiness.add_argument("--stdio-cwd", default=str(DEFAULT_REPO_ROOT))
    readiness.add_argument("--live-smoke", action="store_true")
    readiness.add_argument("--dry-run", action="store_true")
    readiness.add_argument("--format", choices=["json", "markdown"], default="json")
    readiness.add_argument("--output", default=None)
    readiness.set_defaults(func=cmd_operator_readiness)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ApiError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
