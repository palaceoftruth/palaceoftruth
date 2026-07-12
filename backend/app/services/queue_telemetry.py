from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from arq.constants import job_key_prefix, result_key_prefix
from arq.jobs import DeserializationError, deserialize_job, deserialize_result
from sqlalchemy import text

from app.schemas.palace import PalaceWorkerBackpressureSummary, PalaceWorkerQueueMetrics
from app.workers.queues import (
    DEFAULT_WORKER_QUEUE,
    MEDIA_WORKER_QUEUE,
    PALACE_WORKER_QUEUE,
    WORKER_HEALTH_CHECK_TTL_SECONDS,
)


_HEALTH_RE = re.compile(r"\bj_ongoing=(?P<ongoing>\d+)\s+queued=(?P<queued>\d+)\b")
_MAX_RESULT_SAMPLES = 250
MEMORY_QUEUE_BACKPRESSURE_DEPTH = 25
MEMORY_QUEUE_SATURATED_DEPTH = 100
MEMORY_QUEUE_BACKPRESSURE_AGE_SECONDS = 300
MEMORY_QUEUE_SATURATED_AGE_SECONDS = 900
MEMORY_QUEUE_BACKPRESSURE_RETRY_AFTER_SECONDS = 15
MEMORY_QUEUE_SATURATED_RETRY_AFTER_SECONDS = 60
MEMORY_QUEUE_POLL_AFTER_SECONDS = 5


@dataclass(frozen=True)
class WorkerQueueGroup:
    key: str
    label: str
    queue_name: str
    functions: frozenset[str]


WORKER_QUEUE_GROUPS = (
    WorkerQueueGroup(
        key="media_ingest",
        label="Media ingest",
        queue_name=MEDIA_WORKER_QUEUE,
        functions=frozenset({"process_media", "process_youtube"}),
    ),
    WorkerQueueGroup(
        key="memory",
        label="Memory writes",
        queue_name=DEFAULT_WORKER_QUEUE,
        functions=frozenset({"memory_artifact", "embed_item"}),
    ),
    WorkerQueueGroup(
        key="relationships",
        label="Relationship extraction",
        queue_name=DEFAULT_WORKER_QUEUE,
        functions=frozenset({"extract_relationships", "backfill_deferred_relationships"}),
    ),
    WorkerQueueGroup(
        key="dirty_marking",
        label="Dirty marking",
        queue_name=PALACE_WORKER_QUEUE,
        functions=frozenset({"mark_item_dirty_and_schedule"}),
    ),
    WorkerQueueGroup(
        key="palace_builds",
        label="Palace builds",
        queue_name=PALACE_WORKER_QUEUE,
        functions=frozenset({"palace_run_build"}),
    ),
)


def _decode_job_id(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def _queue_entries(arq_pool: Any, queue_name: str) -> list[tuple[str, float]]:
    rows = await arq_pool.zrange(queue_name, 0, -1, withscores=True)
    return [(_decode_job_id(job_id), float(score)) for job_id, score in rows]


async def _job_functions(arq_pool: Any, job_ids: list[str]) -> dict[str, str]:
    if not job_ids:
        return {}
    deserializer = getattr(arq_pool, "job_deserializer", None)
    payloads = await arq_pool.mget([f"{job_key_prefix}{job_id}" for job_id in job_ids])
    functions: dict[str, str] = {}
    for job_id, payload in zip(job_ids, payloads, strict=False):
        if payload is None:
            continue
        try:
            functions[job_id] = deserialize_job(payload, deserializer=deserializer).function
        except DeserializationError:
            continue
    return functions


async def _health_by_queue(
    arq_pool: Any, queue_names: set[str]
) -> dict[str, tuple[int | None, int | None, int, float | None]]:
    result: dict[str, tuple[int | None, int | None, int, float | None]] = {}
    for queue_name in queue_names:
        samples: list[tuple[int, int, float]] = []
        async for raw_key in arq_pool.scan_iter(match=f"{queue_name}:health-check:*", count=100):
            if len(samples) >= 100:
                break
            health_key = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            raw = await arq_pool.get(health_key)
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            match = _HEALTH_RE.search(text)
            if not match:
                continue
            ttl_ms = await arq_pool.pttl(health_key)
            if ttl_ms <= 0:
                continue
            age = max(0.0, WORKER_HEALTH_CHECK_TTL_SECONDS - (ttl_ms / 1000))
            samples.append((int(match.group("ongoing")), int(match.group("queued")), age))
        if not samples:
            result[queue_name] = (None, None, 0, None)
            continue
        result[queue_name] = (
            sum(sample[0] for sample in samples),
            max(sample[1] for sample in samples),
            len(samples),
            round(min(sample[2] for sample in samples), 3),
        )
    return result


async def _recent_result_latencies(arq_pool: Any) -> dict[str, list[tuple[datetime, bool, float]]]:
    deserializer = getattr(arq_pool, "job_deserializer", None)
    by_function: dict[str, list[tuple[datetime, bool, float]]] = {}
    scanned = 0
    async for key in arq_pool.scan_iter(match=f"{result_key_prefix}*", count=100):
        if scanned >= _MAX_RESULT_SAMPLES:
            break
        scanned += 1
        raw = await arq_pool.get(key)
        if raw is None:
            continue
        try:
            result = deserialize_result(raw, deserializer=deserializer)
        except DeserializationError:
            continue
        latency = max((result.finish_time - result.enqueue_time).total_seconds(), 0.0)
        by_function.setdefault(result.function, []).append((result.finish_time, result.success, latency))
    return by_function


async def _media_pressure(db: Any | None) -> dict[str, Any]:
    if db is None:
        return {}

    try:
        aggregate_rows = (
            await db.execute(
                text(
                    """
                    SELECT
                        tenant_id,
                        COUNT(*) FILTER (WHERE status = 'queued') AS queued_depth,
                        COUNT(*) FILTER (WHERE status = 'processing') AS processing_depth,
                        EXTRACT(EPOCH FROM (NOW() - MIN(created_at) FILTER (WHERE status = 'queued'))) AS oldest_queued_age_seconds,
                        COUNT(*) FILTER (
                            WHERE status = 'failed'
                              AND completed_at >= NOW() - INTERVAL '24 hours'
                        ) AS recent_failed,
                        COUNT(*) FILTER (
                            WHERE status = 'failed'
                              AND completed_at >= NOW() - INTERVAL '24 hours'
                              AND error_message ~* 'timeout|timed out'
                        ) AS recent_timeout_count
                    FROM jobs
                    WHERE job_type IN ('media', 'video', 'youtube')
                      AND status IN ('queued', 'processing', 'failed')
                    GROUP BY tenant_id
                    HAVING
                        COUNT(*) FILTER (WHERE status IN ('queued', 'processing')) > 0
                        OR COUNT(*) FILTER (
                            WHERE status = 'failed'
                              AND completed_at >= NOW() - INTERVAL '24 hours'
                        ) > 0
                    ORDER BY
                        COUNT(*) FILTER (WHERE status = 'queued') DESC,
                        MIN(created_at) FILTER (WHERE status = 'queued') ASC NULLS LAST
                    """
                )
            )
        ).fetchall()
        sample_rows = (
            await db.execute(
                text(
                    """
                    SELECT
                        tenant_id,
                        COUNT(*) FILTER (WHERE status = 'queued') AS queued_depth,
                        COUNT(*) FILTER (WHERE status = 'processing') AS processing_depth,
                        EXTRACT(EPOCH FROM (NOW() - MIN(created_at) FILTER (WHERE status = 'queued'))) AS oldest_queued_age_seconds,
                        COUNT(*) FILTER (
                            WHERE status = 'failed'
                              AND completed_at >= NOW() - INTERVAL '24 hours'
                        ) AS recent_failed,
                        COUNT(*) FILTER (
                            WHERE status = 'failed'
                              AND completed_at >= NOW() - INTERVAL '24 hours'
                              AND error_message ~* 'timeout|timed out'
                        ) AS recent_timeout_count
                    FROM jobs
                    WHERE job_type IN ('media', 'video', 'youtube')
                      AND status IN ('queued', 'processing', 'failed')
                    GROUP BY tenant_id
                    HAVING
                        COUNT(*) FILTER (WHERE status IN ('queued', 'processing')) > 0
                        OR COUNT(*) FILTER (
                            WHERE status = 'failed'
                              AND completed_at >= NOW() - INTERVAL '24 hours'
                        ) > 0
                    ORDER BY
                        COUNT(*) FILTER (WHERE status = 'queued') DESC,
                        MIN(created_at) FILTER (WHERE status = 'queued') ASC NULLS LAST
                    LIMIT 5
                    """
                )
            )
        ).fetchall()
    except Exception as exc:
        return {"telemetry_error": f"media DB pressure unavailable: {type(exc).__name__}: {exc}"}

    db_queued_depth = 0
    db_processing_depth = 0
    recent_failed = 0
    recent_timeout_count = 0
    oldest_queued_age_seconds: int | None = None
    queued_tenant_count = 0
    processing_tenant_count = 0
    max_queued_per_tenant = 0
    max_processing_per_tenant = 0
    for row in aggregate_rows:
        oldest = row.oldest_queued_age_seconds
        queued_depth = int(row.queued_depth or 0)
        processing_depth = int(row.processing_depth or 0)
        row_recent_failed = int(row.recent_failed or 0)
        row_timeout_count = int(row.recent_timeout_count or 0)
        db_queued_depth += queued_depth
        db_processing_depth += processing_depth
        recent_failed += row_recent_failed
        recent_timeout_count += row_timeout_count
        if queued_depth > 0:
            queued_tenant_count += 1
        if processing_depth > 0:
            processing_tenant_count += 1
        max_queued_per_tenant = max(max_queued_per_tenant, queued_depth)
        max_processing_per_tenant = max(max_processing_per_tenant, processing_depth)
        if oldest is not None:
            oldest_seconds = int(oldest)
            oldest_queued_age_seconds = (
                oldest_seconds
                if oldest_queued_age_seconds is None
                else max(oldest_queued_age_seconds, oldest_seconds)
            )

    pressure: list[dict[str, int | None]] = []
    for index, row in enumerate(sample_rows, start=1):
        oldest = row.oldest_queued_age_seconds
        pressure.append(
            {
                "rank": index,
                "queued_depth": int(row.queued_depth or 0),
                "processing_depth": int(row.processing_depth or 0),
                "oldest_queued_age_seconds": int(oldest) if oldest is not None else None,
                "recent_failed": int(row.recent_failed or 0),
                "recent_timeout_count": int(row.recent_timeout_count or 0),
            }
        )
    return {
        "db_queued_depth": db_queued_depth,
        "db_processing_depth": db_processing_depth,
        "oldest_db_queued_age_seconds": oldest_queued_age_seconds,
        "queued_tenant_count": queued_tenant_count,
        "processing_tenant_count": processing_tenant_count,
        "max_queued_per_tenant": max_queued_per_tenant,
        "max_processing_per_tenant": max_processing_per_tenant,
        "recent_failed": recent_failed,
        "recent_timeout_count": recent_timeout_count,
        "tenant_pressure": pressure,
    }


async def build_worker_backpressure(arq_pool: Any | None, db: Any | None = None) -> PalaceWorkerBackpressureSummary:
    generated_at = datetime.now(UTC)
    empty_metrics = [
        PalaceWorkerQueueMetrics(
            key=group.key,
            label=group.label,
            queue_name=group.queue_name,
            functions=sorted(group.functions),
            telemetry_error="Redis telemetry unavailable",
        )
        for group in WORKER_QUEUE_GROUPS
    ]
    if arq_pool is None:
        return PalaceWorkerBackpressureSummary(generated_at=generated_at, queues=empty_metrics)

    try:
        queue_entries = {
            queue_name: await _queue_entries(arq_pool, queue_name)
            for queue_name in {group.queue_name for group in WORKER_QUEUE_GROUPS}
        }
        job_ids = [job_id for rows in queue_entries.values() for job_id, _score in rows]
        job_functions = await _job_functions(arq_pool, job_ids)
        health = await _health_by_queue(arq_pool, set(queue_entries))
        result_latencies = await _recent_result_latencies(arq_pool)
        media_pressure = await _media_pressure(db)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return PalaceWorkerBackpressureSummary(
            generated_at=generated_at,
            queues=[metric.model_copy(update={"telemetry_error": error}) for metric in empty_metrics],
        )

    now_ms = generated_at.timestamp() * 1000
    metrics: list[PalaceWorkerQueueMetrics] = []
    for group in WORKER_QUEUE_GROUPS:
        matched_scores = [
            score
            for job_id, score in queue_entries[group.queue_name]
            if job_functions.get(job_id) in group.functions
        ]
        unexpected_functions: list[str] = []
        unexpected_function_count = 0
        if group.key == "media_ingest":
            unexpected_function_names = [
                function
                for job_id, _score in queue_entries[group.queue_name]
                if (function := job_functions.get(job_id)) and function not in group.functions
            ]
            unexpected_function_count = len(unexpected_function_names)
            unexpected_functions = sorted(set(unexpected_function_names))[:5]
        ready_scores = [score for score in matched_scores if score <= now_ms]
        deferred_count = len(matched_scores) - len(ready_scores)
        oldest_age = int(max((now_ms - score) / 1000 for score in ready_scores)) if ready_scores else None
        worker_ongoing, worker_queued, worker_instance_count, heartbeat_age = health[group.queue_name]
        recent_rows = sorted(
            [
                row
                for function in group.functions
                for row in result_latencies.get(function, [])
            ],
            key=lambda row: row[0],
            reverse=True,
        )[:20]
        successful_latencies = [latency for _finished_at, success, latency in recent_rows if success]

        metrics.append(
            PalaceWorkerQueueMetrics(
                key=group.key,
                label=group.label,
                queue_name=group.queue_name,
                functions=sorted(group.functions),
                queued_depth=len(ready_scores),
                deferred_depth=deferred_count,
                oldest_queued_age_seconds=oldest_age,
                worker_concurrency=worker_ongoing,
                worker_queue_depth=worker_queued,
                worker_available=worker_instance_count > 0,
                worker_instance_count=worker_instance_count,
                worker_heartbeat_age_seconds=heartbeat_age,
                db_queued_depth=media_pressure.get("db_queued_depth") if group.key == "media_ingest" else None,
                db_processing_depth=media_pressure.get("db_processing_depth") if group.key == "media_ingest" else None,
                oldest_db_queued_age_seconds=(
                    media_pressure.get("oldest_db_queued_age_seconds") if group.key == "media_ingest" else None
                ),
                queued_tenant_count=media_pressure.get("queued_tenant_count") if group.key == "media_ingest" else None,
                processing_tenant_count=(
                    media_pressure.get("processing_tenant_count") if group.key == "media_ingest" else None
                ),
                max_queued_per_tenant=(
                    media_pressure.get("max_queued_per_tenant") if group.key == "media_ingest" else None
                ),
                max_processing_per_tenant=(
                    media_pressure.get("max_processing_per_tenant") if group.key == "media_ingest" else None
                ),
                recent_completed=len(successful_latencies),
                recent_failed=(
                    media_pressure.get("recent_failed", 0)
                    if group.key == "media_ingest"
                    else sum(1 for _finished_at, success, _latency in recent_rows if not success)
                ),
                recent_timeout_count=media_pressure.get("recent_timeout_count", 0) if group.key == "media_ingest" else 0,
                recent_avg_latency_seconds=round(mean(successful_latencies), 1) if successful_latencies else None,
                unexpected_function_count=unexpected_function_count,
                unexpected_functions=unexpected_functions,
                tenant_pressure=media_pressure.get("tenant_pressure", []) if group.key == "media_ingest" else [],
                telemetry_error=media_pressure.get("telemetry_error") if group.key == "media_ingest" else None,
            )
        )

    return PalaceWorkerBackpressureSummary(generated_at=generated_at, queues=metrics)


def _memory_queue_depth(metric: PalaceWorkerQueueMetrics) -> int:
    return max(metric.queued_depth, metric.worker_queue_depth or 0)


def _memory_queue_state(metric: PalaceWorkerQueueMetrics) -> tuple[str, int | None]:
    if metric.telemetry_error:
        return "unknown", None

    depth = _memory_queue_depth(metric)
    oldest_age = metric.oldest_queued_age_seconds or 0
    if depth >= MEMORY_QUEUE_SATURATED_DEPTH or oldest_age >= MEMORY_QUEUE_SATURATED_AGE_SECONDS:
        return "saturated", MEMORY_QUEUE_SATURATED_RETRY_AFTER_SECONDS
    if depth >= MEMORY_QUEUE_BACKPRESSURE_DEPTH or oldest_age >= MEMORY_QUEUE_BACKPRESSURE_AGE_SECONDS:
        return "backpressure", MEMORY_QUEUE_BACKPRESSURE_RETRY_AFTER_SECONDS
    return "healthy", None


async def build_memory_queue_hint(arq_pool: Any | None) -> dict[str, Any]:
    """Build the public memory-write queue hint from existing worker telemetry."""
    summary = await build_worker_backpressure(arq_pool)
    metric = next((queue for queue in summary.queues if queue.key == "memory"), None)
    if metric is None:
        return {
            "state": "unknown",
            "poll_after_seconds": MEMORY_QUEUE_POLL_AFTER_SECONDS,
            "rate_limit_state": "not_enforced",
            "retry_after_seconds": None,
            "telemetry_error": "Memory queue telemetry unavailable",
        }

    state, retry_after_seconds = _memory_queue_state(metric)
    return {
        "state": state,
        "queue_name": metric.queue_name,
        "queued_depth": metric.queued_depth,
        "deferred_depth": metric.deferred_depth,
        "worker_queue_depth": metric.worker_queue_depth,
        "oldest_queued_age_seconds": metric.oldest_queued_age_seconds,
        "retry_after_seconds": retry_after_seconds,
        "poll_after_seconds": MEMORY_QUEUE_POLL_AFTER_SECONDS,
        "rate_limit_state": "not_enforced",
        "telemetry_error": "Memory queue telemetry unavailable" if metric.telemetry_error else None,
    }
