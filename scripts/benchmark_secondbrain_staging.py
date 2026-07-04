#!/usr/bin/env python3
"""Manual dogfood helper for the Palace of Truth staging tenant.

This is intentionally not a test runner. It creates tagged benchmark memories,
waits for their embedding jobs, verifies retrieval surfaces, and can produce a
human-reviewed cleanup plan. Permanent deletion requires an explicit confirmation
string so a casual command cannot erase staging data.

The filename is historical. Defaults now point at standalone Palace of Truth
staging while the old API-key environment variable remains supported.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_API_BASE_URL = "http://localhost:8000"
DEFAULT_FRONTEND_URL = "http://localhost:8080"
RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{2,64}$")

RUN_DIR = Path(__file__).resolve().parent / "benchmark-runs"

TOPICS = (
    {
        "wing": "product",
        "room": "pricing",
        "tags": ["pricing", "launch", "cta"],
        "query": "pricing launch CTA",
        "phrases": [
            "pricing launch",
            "offer framing",
            "conversion proof",
            "buyer urgency",
        ],
    },
    {
        "wing": "customers",
        "room": "calls",
        "tags": ["calls", "customer", "objections"],
        "query": "customer calls objections",
        "phrases": [
            "customer objections",
            "onboarding drag",
            "annual contract friction",
            "rollout risk",
        ],
    },
    {
        "wing": "infra",
        "room": "agents",
        "tags": ["agents", "infra", "workers"],
        "query": "agent worker recovery",
        "phrases": [
            "worker recovery",
            "queue drain",
            "embedding backlog",
            "cluster failover",
        ],
    },
    {
        "wing": "founder",
        "room": "notes",
        "tags": ["notes", "founder", "strategy"],
        "query": "founder note strategy",
        "phrases": [
            "founder note",
            "strategic wedge",
            "operator trust",
            "memory continuity",
        ],
    },
    {
        "wing": "research",
        "room": "market",
        "tags": ["market", "research", "competitor"],
        "query": "market research competitor",
        "phrases": [
            "market research",
            "competitor analysis",
            "positioning gap",
            "category narrative",
        ],
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

    def _mcp_scope_headers(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, str]:
        normalized = method.upper(), path
        if normalized in {
            ("GET", "/api/v1/memory/whoami"),
            ("GET", "/api/v1/memory/jobs"),
            ("GET", "/api/v1/memory/entries"),
            ("GET", "/api/v1/memory/scopes"),
        } or (normalized[0] == "GET" and path.startswith("/api/v1/memory/jobs/")):
            return {"X-MCP-Scope": "read", "X-MCP-Scopes": "read"}
        if normalized in {
            ("POST", "/api/v1/memory/retrieve"),
            ("POST", "/api/v1/memory/retrieve-agent"),
        }:
            return {"X-MCP-Scope": "read", "X-MCP-Scopes": "read"}
        if normalized == ("POST", "/api/v1/memory/entries"):
            scopes = ["write"]
            write_grant = _write_scope_grant(body)
            if write_grant:
                scopes.append(write_grant)
            return {"X-MCP-Scope": "write", "X-MCP-Scopes": ",".join(scopes)}
        return {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        qs = ""
        if query:
            qs = "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        url = f"{self.base_url.rstrip('/')}{path}{qs}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "X-API-Key": self.api_key,
        }
        headers.update(self._mcp_scope_headers(method, path, body))
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


def _write_scope_grant(entry: dict[str, Any] | None) -> str | None:
    if not isinstance(entry, dict):
        return None
    scope = entry.get("scope")
    if not isinstance(scope, dict):
        return None
    scope_type = scope.get("type")
    if scope_type in {"agent", "workspace", "session"}:
        return f"write:{scope_type}"
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_run_id() -> str:
    return utc_now().strftime("%Y%m%d-%H%M%S")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise SystemExit("run id must be 3-65 chars and contain only letters, numbers, dot, dash, underscore")
    return run_id


def run_tag(run_id: str) -> str:
    return f"benchmark-run-{run_id}"


def artifact_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}.jsonl"


def cleanup_plan_path(run_id: str) -> Path:
    return RUN_DIR / f"{run_id}-cleanup-plan.json"


@dataclass
class RunArtifactLock:
    path: Path
    purpose: str
    _handle: Any | None = None

    def __enter__(self) -> "RunArtifactLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise SystemExit(
                f"benchmark run artifact lock is already held for {self.purpose}: {self.path}"
            ) from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"pid={os.getpid()} purpose={self.purpose}\n")
        self._handle.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def run_artifact_lock_path(run_id: str, *, namespace: str = "secondbrain") -> Path:
    return RUN_DIR / f"{run_id}-{namespace}.lock"


def acquire_run_artifact_lock(run_id: str, *, namespace: str = "secondbrain", purpose: str) -> RunArtifactLock:
    return RunArtifactLock(run_artifact_lock_path(run_id, namespace=namespace), purpose)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"missing run artifact: {path}")
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def make_body(run_id: str, index: int, topic: dict[str, Any]) -> str:
    sentinel = f"SBT-BENCH-{run_id}-{index:04d}"
    phrases = ", ".join(topic["phrases"])
    return (
        f"{sentinel}\n\n"
        f"This benchmark memory belongs to the {topic['wing']} wing and the {topic['room']} room. "
        f"It is part of a deliberate staging dogfood run for Palace of Truth, not production Hermes memory. "
        f"The main retrieval phrases are {phrases}. "
        f"Operators should be able to retrieve this entry by its sentinel, by the run tag, by the room topic, "
        f"and by ordinary semantic questions about {topic['query']}. "
        f"The record exists to exercise embedding throughput, Palace room routing, exact lexical rescue, "
        f"scoped retrieval, job polling, Control Tower health, and post-run cleanup. "
        f"Repeated details: {phrases}. "
        f"Verification note: the expected answer must include {sentinel} and preserve the relationship between "
        f"{topic['wing']}, {topic['room']}, and benchmark run {run_id}."
    )


def make_entry(run_id: str, tenant_id: str, index: int, *, enable_ai_enrichment: bool) -> dict[str, Any]:
    topic = TOPICS[index % len(TOPICS)]
    created = utc_now() - timedelta(seconds=index)
    tags = [
        *topic["tags"],
        "benchmark",
        "benchmark-cleanup-ok",
        run_tag(run_id),
        f"benchmark-wing-{topic['wing']}",
        f"benchmark-room-{topic['room']}",
    ]
    title = f"Benchmark {topic['wing']} / {topic['room']} memory {index:04d}"
    return {
        "tenant_id": tenant_id,
        "title": title,
        "summary": f"Benchmark staging memory for {topic['query']} with deterministic sentinel.",
        "body": make_body(run_id, index, topic),
        # Keep this historical namespace stable so old run IDs replay idempotently.
        "source": "secondbrain-staging-benchmark",
        "source_url": f"benchmark://{run_id}/{index:04d}",
        "created_at": created.isoformat(),
        "created_by_role": "benchmark-operator",
        "tags": tags,
        "scope": {"type": "tenant_shared", "key": None},
        "metadata": {
            "benchmark": {
                "run_id": run_id,
                "index": index,
                "wing": topic["wing"],
                "room": topic["room"],
                "cleanup_allowed": True,
            }
        },
        "idempotency_key": f"secondbrain-staging-benchmark:{run_id}:{index:04d}",
        "enable_ai_enrichment": enable_ai_enrichment,
    }


def client_from_args(args: argparse.Namespace) -> Client:
    api_key = args.api_key or os.getenv("PALACEOFTRUTH_API_KEY") or os.getenv("SECONDBRAIN_API_KEY")
    if not api_key:
        raise SystemExit("set PALACEOFTRUTH_API_KEY or SECONDBRAIN_API_KEY, or pass --api-key")
    return Client(base_url=args.api_base_url, api_key=api_key)


def cmd_whoami(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    print(json.dumps(client.request("GET", "/api/v1/memory/whoami"), indent=2))
    return 0


def resolve_tenant_id(client: Client, requested: str | None) -> str:
    if requested:
        return requested
    whoami = client.request("GET", "/api/v1/memory/whoami")
    tenant_id = whoami.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise SystemExit(f"could not resolve tenant id from /memory/whoami: {whoami}")
    return tenant_id


def cmd_ingest(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id or default_run_id())
    tenant_id = resolve_tenant_id(client, args.tenant_id)
    with acquire_run_artifact_lock(run_id, purpose="secondbrain benchmark ingest"):
        path = artifact_path(run_id)
        if path.exists() and not args.resume:
            raise SystemExit(f"{path} already exists; pass --resume or choose a new --run-id")

        existing = {int(row["index"]) for row in read_jsonl(path)} if path.exists() else set()
        indices = [idx for idx in range(args.count) if idx not in existing]
        print(
            f"target={client.base_url} tenant={tenant_id} run_id={run_id} "
            f"count={args.count} remaining={len(indices)}"
        )
        print(f"run_tag={run_tag(run_id)} artifact={path}")
        if args.dry_run:
            sample = make_entry(run_id, tenant_id, 0, enable_ai_enrichment=args.enable_ai_enrichment)
            print(json.dumps(sample, indent=2))
            return 0

        def submit(index: int) -> dict[str, Any]:
            entry = make_entry(run_id, tenant_id, index, enable_ai_enrichment=args.enable_ai_enrichment)
            started = time.monotonic()
            accepted = client.request("POST", "/api/v1/memory/entries", body=entry, timeout=args.timeout)
            latency_ms = int((time.monotonic() - started) * 1000)
            return {
                "run_id": run_id,
                "index": index,
                "job_id": accepted["job_id"],
                "status": accepted.get("status"),
                "accepted_as": accepted.get("accepted_as"),
                "accept_latency_ms": latency_ms,
                "idempotency_key": entry["idempotency_key"],
                "title": entry["title"],
                "tags": entry["tags"],
                "created_at": utc_now().isoformat(),
            }

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            future_to_index = {pool.submit(submit, index): index for index in indices}
            for future in concurrent.futures.as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    row = future.result()
                except Exception as exc:
                    print(f"ERROR index={index}: {exc}", file=sys.stderr)
                    if not args.keep_going:
                        raise
                    continue
                append_jsonl(path, row)
                completed += 1
                if completed % args.progress_every == 0 or completed == len(indices):
                    print(f"accepted {completed}/{len(indices)} new entries")

        print(f"done run_id={run_id} run_tag={run_tag(run_id)} artifact={path}")
    return 0


def job_counts(client: Client, rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        job = client.request("GET", f"/api/v1/memory/jobs/{row['job_id']}", timeout=30)
        status = str(job.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def cmd_wait(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    rows = read_jsonl(artifact_path(run_id))
    deadline = time.monotonic() + args.timeout_seconds
    terminal = {"complete", "duplicate", "failed", "cancelled"}
    last_counts: dict[str, int] = {}

    while True:
        counts = job_counts(client, rows)
        if counts != last_counts:
            print(json.dumps({"run_id": run_id, "counts": counts, "checked": len(rows)}, sort_keys=True))
            last_counts = counts
        non_terminal = sum(count for status, count in counts.items() if status not in terminal)
        failed = counts.get("failed", 0) + counts.get("cancelled", 0)
        if non_terminal == 0:
            return 1 if failed and not args.allow_failures else 0
        if time.monotonic() >= deadline:
            print("timed out waiting for memory jobs", file=sys.stderr)
            return 2
        time.sleep(args.interval_seconds)


def list_tagged_items(client: Client, tag: str, *, per_page: int = 100) -> list[dict[str, Any]]:
    first = client.request(
        "GET",
        "/api/v1/items",
        query={"tags": tag, "page": 1, "per_page": per_page, "sort": "created_at", "order": "desc"},
        timeout=60,
    )
    total = int(first.get("total", 0))
    items = list(first.get("items", []))
    page = 2
    while len(items) < total:
        payload = client.request(
            "GET",
            "/api/v1/items",
            query={"tags": tag, "page": page, "per_page": per_page, "sort": "created_at", "order": "desc"},
            timeout=60,
        )
        items.extend(payload.get("items", []))
        page += 1
    return items


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _result_has_required_tags(result: dict[str, Any], required_tags: set[str]) -> bool:
    tags = result.get("tags")
    if not isinstance(tags, list):
        return False
    return required_tags.issubset({tag for tag in tags if isinstance(tag, str)})


def _result_is_wakeup_brief(result: dict[str, Any]) -> bool:
    tags = result.get("tags")
    if isinstance(tags, list) and "wake-up-brief" in tags:
        return True
    source_url = result.get("source_url")
    return isinstance(source_url, str) and source_url.startswith("memory://wake-up/")


def build_dogfood_gate_report(
    *,
    palace: dict[str, Any],
    control_tower: dict[str, Any],
    retrieval_checks: list[dict[str, Any]],
    hit_ratios: dict[str, float] | None = None,
    min_hit_ratio: float | None = None,
) -> dict[str, Any]:
    """Evaluate staging dogfood health beyond a completed Palace run status."""

    failures: list[str] = []
    active_run = palace.get("active_palace_run")
    generations = {
        "dirty_generation": palace.get("dirty_generation"),
        "indexed_generation": palace.get("indexed_generation"),
        "backlog_generation": palace.get("backlog_generation"),
        "active_palace_run": active_run,
    }

    if palace.get("dirty_generation") != palace.get("indexed_generation"):
        failures.append(
            "Palace dirty_generation does not match indexed_generation "
            f"({palace.get('dirty_generation')} != {palace.get('indexed_generation')})"
        )
    if _as_int(palace.get("backlog_generation")) != 0:
        failures.append(f"Palace backlog_generation is {_as_int(palace.get('backlog_generation'))}")
    if active_run is not None:
        status = active_run.get("status") if isinstance(active_run, dict) else active_run
        failures.append(f"Palace still has an active run ({status})")

    room_artifacts = control_tower.get("room_artifacts") or {}
    room_failures: list[str] = []
    blocked_rooms = _as_int(room_artifacts.get("blocked_rooms"))
    if blocked_rooms:
        blocked_room_samples = [
            sample
            for sample in room_artifacts.get("blocked_room_samples", [])
            if isinstance(sample, dict)
        ]
        sample_names = [
            str(sample.get("room_name") or sample.get("room_stable_key") or sample.get("room_id"))
            for sample in blocked_room_samples[:3]
        ]
        detail = f"blocked_rooms={blocked_rooms}"
        if sample_names:
            detail += " sample=" + "; ".join(sample_names)
        room_failures.append(detail)
    for section_name in ("closets", "snapshots", "tunnels"):
        section = room_artifacts.get(section_name) or {}
        stale = _as_int(section.get("stale"))
        if stale:
            room_failures.append(f"{section_name}.stale={stale}")
    if room_failures:
        failures.append("Room artifact health is not clean: " + ", ".join(room_failures))

    wakeup_briefs = control_tower.get("wakeup_briefs") or {}
    stale_briefs = _as_int(wakeup_briefs.get("stale"))
    recent_stale_briefs = [
        brief.get("title") or brief.get("scope_key") or brief.get("scope_type") or "unknown"
        for brief in wakeup_briefs.get("recent_briefs", [])
        if isinstance(brief, dict) and brief.get("stale")
    ]
    if stale_briefs or recent_stale_briefs:
        failures.append(
            "Wake-up briefs are stale: "
            + ", ".join([f"stale={stale_briefs}", *[str(title) for title in recent_stale_briefs]])
        )

    diary_rollups = control_tower.get("diary_rollups") or {}
    stale_diaries = _as_int(diary_rollups.get("stale"))
    if stale_diaries:
        failures.append(f"Diary rollups are stale: stale={stale_diaries}")

    memory_health = control_tower.get("memory_health") or {}
    memory_failures = [
        f"{key}={_as_int(memory_health.get(key))}"
        for key in ("queued", "processing", "failed", "retryable")
        if _as_int(memory_health.get(key))
    ]
    if memory_failures:
        failures.append("Memory job health is not clean: " + ", ".join(memory_failures))

    webhook_health = control_tower.get("webhook_health") or {}
    webhook_failures = [
        f"{key}={_as_int(webhook_health.get(key))}"
        for key in ("pending", "failed_jobs", "retryable_jobs")
        if _as_int(webhook_health.get(key))
    ]
    if webhook_failures:
        failures.append("Webhook job health is not clean: " + ", ".join(webhook_failures))

    queue_reports: list[dict[str, Any]] = []
    for queue in (control_tower.get("worker_backpressure") or {}).get("queues", []):
        if not isinstance(queue, dict):
            continue
        queue_report = {
            "key": queue.get("key"),
            "queued_depth": _as_int(queue.get("queued_depth")),
            "deferred_depth": _as_int(queue.get("deferred_depth")),
            "worker_queue_depth": _as_int(queue.get("worker_queue_depth")),
            "recent_failed": _as_int(queue.get("recent_failed")),
            "telemetry_error": queue.get("telemetry_error"),
        }
        queue_reports.append(queue_report)
        queue_failures = [
            f"{name}={value}"
            for name, value in queue_report.items()
            if name not in {"key", "telemetry_error"} and value
        ]
        if queue_report["telemetry_error"]:
            queue_failures.append(f"telemetry_error={queue_report['telemetry_error']}")
        if queue_failures:
            failures.append(f"Worker queue {queue.get('key') or queue.get('label')} is not drained: " + ", ".join(queue_failures))

    retrieval_reports: list[dict[str, Any]] = []
    for check in retrieval_checks:
        name = str(check.get("name") or "retrieval")
        trace = check.get("trace") or {}
        results = [row for row in check.get("results", []) if isinstance(row, dict)]
        required_tags = {tag for tag in check.get("required_tags", []) if isinstance(tag, str)}
        source_hits = [row for row in results if _result_has_required_tags(row, required_tags)]
        wakeup_hits = [row for row in results if _result_is_wakeup_brief(row)]
        report = {
            "name": name,
            "total": _as_int(check.get("total")),
            "fallback_used": bool(trace.get("fallback_used")),
            "completeness_warning": trace.get("completeness_warning"),
            "source_item_hits": len(source_hits),
            "wakeup_brief_hits": len(wakeup_hits),
            "expected_hit": check.get("expected_hit"),
        }
        retrieval_reports.append(report)
        if report["fallback_used"]:
            failures.append(f"{name} used global fallback")
        if report["completeness_warning"]:
            failures.append(f"{name} returned a completeness warning: {report['completeness_warning']}")
        if report["total"] < 1:
            failures.append(f"{name} returned no retrieval results")
        if required_tags and not source_hits:
            failures.append(f"{name} returned no expected source items with tags {sorted(required_tags)}")
        if wakeup_hits and not source_hits:
            failures.append(f"{name} returned wake-up briefs instead of expected source items")
        if check.get("expected_hit") is False and check.get("strict_expected_hit"):
            failures.append(f"{name} missed the expected source publication")

    ratio_reports = hit_ratios or {}
    if min_hit_ratio is not None:
        for name, ratio in ratio_reports.items():
            if ratio < min_hit_ratio:
                failures.append(f"{name} hit ratio {ratio:.2f} is below {min_hit_ratio:.2f}")

    return {
        "passed": not failures,
        "failures": failures,
        "generations": generations,
        "room_artifacts": room_artifacts,
        "wakeup_briefs": wakeup_briefs,
        "memory_health": memory_health,
        "webhook_health": webhook_health,
        "worker_queues": queue_reports,
        "retrieval": retrieval_reports,
        "hit_ratios": ratio_reports,
        "min_hit_ratio": min_hit_ratio,
    }


def cmd_verify(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    tag = run_tag(run_id)
    items = list_tagged_items(client, tag)
    stats = client.request("GET", "/api/v1/stats")
    palace = client.request("GET", "/api/v1/palace")
    control_tower = client.request("GET", "/api/v1/palace/control-tower")

    search_query = f"SBT-BENCH-{run_id}-0000 pricing launch CTA"
    search = client.request(
        "POST",
        "/api/v1/search",
        body={"query": search_query, "limit": 10, "tags": [tag]},
        timeout=90,
    )
    retrieve = client.request(
        "POST",
        "/api/v1/memory/retrieve",
        body={
            "query": search_query,
            "limit": 10,
            "tags": [tag],
            "scope": {"type": "tenant_shared", "key": None},
        },
        timeout=90,
    )
    worker_backpressure = control_tower.get("worker_backpressure") or {}
    dogfood_gate = build_dogfood_gate_report(
        palace=palace,
        control_tower=control_tower,
        retrieval_checks=[
            {
                "name": "synthetic exact retrieval",
                "total": retrieve.get("total", 0),
                "trace": retrieve.get("trace"),
                "results": retrieve.get("results", []),
                "required_tags": [tag, "benchmark"],
            }
        ],
        hit_ratios={
            "synthetic_search": 1.0 if search.get("total", 0) >= 1 else 0.0,
            "synthetic_retrieval": 1.0 if retrieve.get("total", 0) >= 1 else 0.0,
        },
        min_hit_ratio=1.0,
    )

    report = {
        "run_id": run_id,
        "run_tag": tag,
        "tagged_items": len(items),
        "ready_tagged_items": sum(1 for item in items if item.get("status") == "ready"),
        "stats": stats,
        "palace_generations": {
            "dirty_generation": palace.get("dirty_generation"),
            "indexed_generation": palace.get("indexed_generation"),
            "backlog_generation": palace.get("backlog_generation"),
            "active_palace_run": palace.get("active_palace_run"),
        },
        "control_tower_keys": sorted(control_tower.keys()),
        "worker_backpressure": {
            "generated_at": worker_backpressure.get("generated_at"),
            "queues": [
                {
                    "key": queue.get("key"),
                    "label": queue.get("label"),
                    "queue_name": queue.get("queue_name"),
                    "queued_depth": queue.get("queued_depth"),
                    "deferred_depth": queue.get("deferred_depth"),
                    "oldest_queued_age_seconds": queue.get("oldest_queued_age_seconds"),
                    "worker_concurrency": queue.get("worker_concurrency"),
                    "worker_queue_depth": queue.get("worker_queue_depth"),
                    "recent_avg_latency_seconds": queue.get("recent_avg_latency_seconds"),
                    "recent_failed": queue.get("recent_failed"),
                    "telemetry_error": queue.get("telemetry_error"),
                }
                for queue in worker_backpressure.get("queues", [])
            ],
        },
        "search_total": search.get("total"),
        "search_titles": [row.get("title") for row in search.get("results", [])[:5]],
        "memory_retrieve_total": retrieve.get("total"),
        "memory_trace": retrieve.get("trace"),
        "dogfood_gate": dogfood_gate,
        "frontend_url": args.frontend_url,
    }
    print(json.dumps(report, indent=2, sort_keys=True))

    failures: list[str] = []
    if len(items) < args.expect_count:
        failures.append(f"expected at least {args.expect_count} tagged items, found {len(items)}")
    if search.get("total", 0) < 1 or retrieve.get("total", 0) < 1:
        failures.append("expected search and memory retrieval to return at least one result")
    failures.extend(dogfood_gate["failures"])
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


def cmd_cleanup_plan(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    tag = run_tag(run_id)
    items = list_tagged_items(client, tag)
    plan = {
        "run_id": run_id,
        "run_tag": tag,
        "generated_at": utc_now().isoformat(),
        "count": len(items),
        "items": [
            {
                "id": item["id"],
                "title": item.get("title"),
                "status": item.get("status"),
                "tags": item.get("tags", []),
                "created_at": item.get("created_at"),
            }
            for item in items
        ],
        "delete_confirmation": f"BENCHMARK-RUN-{run_id}",
    }
    plan_path = cleanup_plan_path(run_id)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({k: plan[k] for k in ("run_id", "run_tag", "count", "delete_confirmation")}, indent=2))
    print(f"wrote {plan_path}")
    return 0


def cmd_cleanup_delete(args: argparse.Namespace) -> int:
    client = client_from_args(args)
    run_id = validate_run_id(args.run_id)
    expected = f"BENCHMARK-RUN-{run_id}"
    if args.confirm_delete != expected:
        raise SystemExit(f"refusing delete; pass --confirm-delete {expected}")
    tag = run_tag(run_id)
    items = list_tagged_items(client, tag)
    if not items:
        print("no tagged items found")
        return 0

    unsafe = [
        item for item in items
        if tag not in item.get("tags", []) or "benchmark-cleanup-ok" not in item.get("tags", [])
    ]
    if unsafe:
        raise SystemExit(f"refusing delete; {len(unsafe)} items are missing expected benchmark cleanup tags")

    print(f"deleting {len(items)} items tagged {tag}")
    if args.dry_run:
        print("dry run only; no items removed")
        return 0

    deleted = delete_benchmark_items(
        client,
        items,
        method=args.method,
        batch_size=args.batch_size,
    )
    if deleted != len(items):
        raise SystemExit(f"removed {deleted}/{len(items)} items before cleanup stopped")
    print("cleanup remove complete; run a manual Palace rebuild if room state should refresh immediately")
    return 0


def delete_benchmark_items(
    client: Client,
    items: list[dict[str, Any]],
    *,
    method: str,
    batch_size: int,
) -> int:
    if method not in {"auto", "batch", "individual"}:
        raise ValueError(f"unsupported cleanup method: {method}")
    if method == "individual":
        return delete_benchmark_items_individually(client, items, already_deleted=0)

    deleted = 0
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        try:
            client.request(
                "POST",
                "/api/v1/items/batch",
                body={"action": "delete", "ids": [item["id"] for item in batch]},
                timeout=90,
            )
        except ApiError as exc:
            if method == "batch":
                raise
            print(
                f"batch delete failed for items {start + 1}-{start + len(batch)} "
                f"with HTTP {exc.status}; falling back to per-item delete",
                file=sys.stderr,
            )
            return delete_benchmark_items_individually(
                client,
                items[start:],
                already_deleted=deleted,
            )
        deleted += len(batch)
        print(f"removed {deleted}/{len(items)}")
    return deleted


def delete_benchmark_items_individually(
    client: Client,
    items: list[dict[str, Any]],
    *,
    already_deleted: int,
) -> int:
    deleted = already_deleted
    total = already_deleted + len(items)
    for item in items:
        item_id = item["id"]
        try:
            client.request("DELETE", f"/api/v1/items/{item_id}", timeout=90)
        except ApiError as exc:
            raise SystemExit(
                f"per-item delete failed for {item_id} ({item.get('title')}) "
                f"with HTTP {exc.status}: {exc.body[:500]}"
            ) from exc
        deleted += 1
        print(f"removed {deleted}/{total}")
    return deleted


def _connection_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    payload = {
        "api_base_url": args.api_base_url,
        "frontend_url": args.frontend_url,
        "api_key": args.api_key,
    }
    payload.update(overrides)
    return argparse.Namespace(**payload)


def trigger_palace_run(client: Client) -> dict[str, Any]:
    return client.request("POST", "/api/v1/palace/runs", timeout=60)


def wait_for_palace_fresh(client: Client, *, timeout_seconds: int, interval_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    last_summary: dict[str, Any] = {}

    while True:
        palace = client.request("GET", "/api/v1/palace", timeout=60)
        active = palace.get("active_palace_run")
        summary = {
            "dirty_generation": palace.get("dirty_generation"),
            "indexed_generation": palace.get("indexed_generation"),
            "backlog_generation": palace.get("backlog_generation"),
            "active_status": active.get("status") if isinstance(active, dict) else None,
            "active_run_id": active.get("id") if isinstance(active, dict) else None,
        }
        if summary != last_summary:
            print(json.dumps({"palace": summary}, sort_keys=True))
            last_summary = summary

        fresh = (
            summary["dirty_generation"] == summary["indexed_generation"]
            and summary["backlog_generation"] == 0
            and summary["active_status"] is None
        )
        if fresh:
            return 0
        if time.monotonic() >= deadline:
            print("timed out waiting for Palace generations to catch up", file=sys.stderr)
            return 2
        time.sleep(interval_seconds)


def cmd_run(args: argparse.Namespace) -> int:
    if "hermes" in args.api_base_url.lower() or "hermes" in args.frontend_url.lower():
        raise SystemExit("refusing to benchmark a Hermes host; use the standalone Palace of Truth staging host")

    client = client_from_args(args)
    run_id = validate_run_id(args.run_id or default_run_id())
    tenant_id = resolve_tenant_id(client, args.tenant_id)
    print(
        json.dumps(
            {
                "benchmark": "secondbrain-staging",
                "api_base_url": args.api_base_url,
                "frontend_url": args.frontend_url,
                "tenant_id": tenant_id,
                "run_id": run_id,
                "run_tag": run_tag(run_id),
                "count": args.count,
            },
            indent=2,
            sort_keys=True,
        )
    )

    if args.dry_run:
        return cmd_ingest(
            _connection_args(
                args,
                run_id=run_id,
                tenant_id=tenant_id,
                count=args.count,
                concurrency=args.concurrency,
                timeout=args.request_timeout,
                progress_every=args.progress_every,
                enable_ai_enrichment=args.enable_ai_enrichment,
                resume=args.resume,
                dry_run=True,
                keep_going=False,
            )
        )

    print("\n[1/5] ingest benchmark memories")
    rc = cmd_ingest(
        _connection_args(
            args,
            run_id=run_id,
            tenant_id=tenant_id,
            count=args.count,
            concurrency=args.concurrency,
            timeout=args.request_timeout,
            progress_every=args.progress_every,
            enable_ai_enrichment=args.enable_ai_enrichment,
            resume=args.resume,
            dry_run=False,
            keep_going=args.keep_going,
        )
    )
    if rc != 0:
        return rc

    print("\n[2/5] wait for embedding jobs")
    rc = cmd_wait(
        _connection_args(
            args,
            run_id=run_id,
            interval_seconds=args.job_interval_seconds,
            timeout_seconds=args.job_timeout_seconds,
            allow_failures=False,
        )
    )
    if rc != 0:
        return rc

    print("\n[3/5] trigger Palace rebuild")
    run = trigger_palace_run(client)
    print(json.dumps(run, indent=2, sort_keys=True))
    if not args.no_palace_wait:
        rc = wait_for_palace_fresh(
            client,
            timeout_seconds=args.palace_timeout_seconds,
            interval_seconds=args.palace_interval_seconds,
        )
        if rc != 0:
            if not args.allow_palace_timeout:
                print(
                    "hint: for large runs, Palace can be queued behind expensive relationship "
                    "extraction; rerun with --allow-palace-timeout to keep verification and "
                    "cleanup planning artifacts.",
                    file=sys.stderr,
                )
                return rc
            print(
                "continuing after Palace timeout because --allow-palace-timeout was set",
                file=sys.stderr,
            )

    print("\n[4/5] verify retrieval and product surfaces")
    rc = cmd_verify(_connection_args(args, run_id=run_id, expect_count=args.count))
    if rc != 0:
        return rc

    print("\n[5/5] write cleanup review plan")
    rc = cmd_cleanup_plan(_connection_args(args, run_id=run_id))
    if rc != 0:
        return rc

    print(
        "\nDONE. Cleanup is not automatic. Review the cleanup plan, then run:\n"
        f"python3 scripts/benchmark_secondbrain_staging.py cleanup-delete --run-id {run_id} "
        f"--confirm-delete BENCHMARK-RUN-{run_id} --dry-run\n"
        "Remove --dry-run only after the item list is exactly what you intend to remove."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-base-url",
        default=os.getenv("PALACEOFTRUTH_API_BASE_URL", DEFAULT_API_BASE_URL),
    )
    parser.add_argument(
        "--frontend-url",
        default=os.getenv("PALACEOFTRUTH_FRONTEND_URL", DEFAULT_FRONTEND_URL),
    )
    parser.add_argument("--api-key", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    whoami = sub.add_parser("whoami", help="Validate the API key and show tenant identity.")
    whoami.set_defaults(func=cmd_whoami)

    ingest = sub.add_parser("ingest", help="Create benchmark memory entries.")
    ingest.add_argument("--run-id", default=None)
    ingest.add_argument("--tenant-id", default=None)
    ingest.add_argument("--count", type=int, default=1000)
    ingest.add_argument("--concurrency", type=int, default=4)
    ingest.add_argument("--timeout", type=float, default=60.0)
    ingest.add_argument("--progress-every", type=int, default=50)
    ingest.add_argument("--enable-ai-enrichment", action="store_true")
    ingest.add_argument("--resume", action="store_true")
    ingest.add_argument("--dry-run", action="store_true")
    ingest.add_argument("--keep-going", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    wait = sub.add_parser("wait", help="Poll benchmark memory jobs from the local run artifact.")
    wait.add_argument("--run-id", required=True)
    wait.add_argument("--interval-seconds", type=int, default=30)
    wait.add_argument("--timeout-seconds", type=int, default=7200)
    wait.add_argument("--allow-failures", action="store_true")
    wait.set_defaults(func=cmd_wait)

    verify = sub.add_parser("verify", help="Verify tagged items, stats, Palace state, search, and memory retrieval.")
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--expect-count", type=int, default=1000)
    verify.set_defaults(func=cmd_verify)

    cleanup_plan = sub.add_parser("cleanup-plan", help="Write a deletion review file for tagged benchmark items.")
    cleanup_plan.add_argument("--run-id", required=True)
    cleanup_plan.set_defaults(func=cmd_cleanup_plan)

    cleanup_delete = sub.add_parser("cleanup-delete", help="Remove tagged benchmark items after human review.")
    cleanup_delete.add_argument("--run-id", required=True)
    cleanup_delete.add_argument("--confirm-delete", required=True)
    cleanup_delete.add_argument("--batch-size", type=int, default=100)
    cleanup_delete.add_argument("--method", choices=["auto", "batch", "individual"], default="auto")
    cleanup_delete.add_argument("--dry-run", action="store_true")
    cleanup_delete.set_defaults(func=cmd_cleanup_delete)

    run = sub.add_parser("run", help="One-command benchmark: ingest, wait, trigger Palace, verify, cleanup-plan.")
    run.add_argument("--run-id", default=None)
    run.add_argument("--tenant-id", default=None)
    run.add_argument("--count", type=int, default=25)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--request-timeout", type=float, default=60.0)
    run.add_argument("--progress-every", type=int, default=25)
    run.add_argument("--job-interval-seconds", type=int, default=30)
    run.add_argument("--job-timeout-seconds", type=int, default=7200)
    run.add_argument("--palace-interval-seconds", type=int, default=20)
    run.add_argument("--palace-timeout-seconds", type=int, default=1800)
    run.add_argument("--enable-ai-enrichment", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--keep-going", action="store_true")
    run.add_argument("--no-palace-wait", action="store_true")
    run.add_argument("--allow-palace-timeout", action="store_true")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
