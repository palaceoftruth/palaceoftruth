from __future__ import annotations

import math
import threading
from collections import defaultdict
from time import monotonic
from typing import Any

from sqlalchemy import text

from app.services.memory_telemetry import memory_telemetry_snapshot
from app.services.queue_telemetry import build_worker_backpressure
from app.services.relationship_telemetry import relationship_telemetry_snapshot

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_SOURCE_TYPE_LABEL_ALLOWLIST = frozenset(
    {
        "note",
        "doc",
        "pdf",
        "webpage",
        "youtube",
        "feed",
        "image",
        "transcript",
        "social_post",
        "memory",
    }
)


class HttpMetricsRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, str], int] = defaultdict(int)
        self._duration_sum: dict[tuple[str, str, str], float] = defaultdict(float)

    def record(self, *, method: str, route: str, status_code: int, duration_seconds: float) -> None:
        labels = (method.upper(), _bounded_route(route), str(status_code))
        with self._lock:
            self._requests[labels] += 1
            self._duration_sum[labels] += max(duration_seconds, 0.0)

    def snapshot(self) -> list[tuple[tuple[str, str, str], int, float]]:
        with self._lock:
            return [
                (labels, count, self._duration_sum[labels])
                for labels, count in sorted(self._requests.items())
            ]


def prometheus_content_type() -> str:
    return _PROMETHEUS_CONTENT_TYPE


def monotonic_seconds() -> float:
    return monotonic()


def _bounded_route(route: str) -> str:
    if not route or route == "/":
        return "/"
    if len(route) > 160:
        return route[:157] + "..."
    return route


def _escape_label(value: object) -> str:
    text_value = str(value)
    return text_value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(labels: dict[str, object] | None = None) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_escape_label(value)}"' for key, value in sorted(labels.items())) + "}"


def _format_value(value: object) -> str:
    if value is None:
        return "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "0"
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return str(value)


class PrometheusTextBuilder:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._declared: set[str] = set()

    def metric(self, name: str, help_text: str, metric_type: str, value: object, labels: dict[str, object] | None = None) -> None:
        if name not in self._declared:
            self._lines.append(f"# HELP {name} {help_text}")
            self._lines.append(f"# TYPE {name} {metric_type}")
            self._declared.add(name)
        self._lines.append(f"{name}{_format_labels(labels)} {_format_value(value)}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _add_http_metrics(builder: PrometheusTextBuilder, recorder: HttpMetricsRecorder | None) -> None:
    if recorder is None:
        return
    for (method, route, status_code), count, duration_sum in recorder.snapshot():
        labels = {"method": method, "route": route, "status_code": status_code}
        builder.metric(
            "palace_http_requests_total",
            "Total HTTP requests handled by this process.",
            "counter",
            count,
            labels,
        )
        builder.metric(
            "palace_http_request_duration_seconds_sum",
            "Total HTTP request duration observed by this process.",
            "counter",
            duration_sum,
            labels,
        )
        builder.metric(
            "palace_http_request_duration_seconds_count",
            "HTTP request duration sample count observed by this process.",
            "counter",
            count,
            labels,
        )


def _add_memory_runtime_metrics(builder: PrometheusTextBuilder) -> None:
    snapshot = memory_telemetry_snapshot()
    for (status, scope_type), count in snapshot["semantic_recall"]:
        builder.metric(
            "palace_semantic_recall_total",
            "Semantic recall requests by outcome and scope type.",
            "counter",
            count,
            {"status": status, "scope_type": scope_type},
        )
    for (status, mode), count in snapshot["retention_extraction"]:
        builder.metric(
            "palace_retention_extraction_total",
            "Retention extraction outcomes by mode.",
            "counter",
            count,
            {"status": status, "mode": mode},
        )
    for (reason,), count in snapshot["scope_guard_violations"]:
        builder.metric(
            "palace_memory_scope_guard_violations_total",
            "Memory scope guard denials by bounded reason.",
            "counter",
            count,
            {"reason": reason},
        )
    for (status, failure_kind, retryable), count in snapshot["embedding_requests"]:
        builder.metric(
            "palace_embedding_requests_total",
            "Embedding provider requests by bounded outcome classification.",
            "counter",
            count,
            {"status": status, "failure_kind": failure_kind, "retryable": retryable},
        )
    relationship_snapshot = relationship_telemetry_snapshot()
    for (provider, validation_outcome, fallback_used), count in relationship_snapshot["extractions"]:
        builder.metric(
            "palace_relationship_extractions_total",
            "Relationship classifications by provider, validation outcome, and fallback use.",
            "counter",
            count,
            {"provider": provider, "validation_outcome": validation_outcome, "fallback_used": fallback_used},
        )
    for (provider,), count in relationship_snapshot["retries"]:
        builder.metric(
            "palace_relationship_extraction_retries_total",
            "Bounded relationship classification retries by provider.",
            "counter",
            count,
            {"provider": provider},
        )
    duration_counts = dict(relationship_snapshot["duration_counts"])
    for labels, duration_sum in relationship_snapshot["duration_sums"]:
        provider, validation_outcome = labels
        metric_labels = {"provider": provider, "validation_outcome": validation_outcome}
        builder.metric(
            "palace_relationship_extraction_duration_seconds_sum",
            "Total relationship classification duration by provider and validation outcome.",
            "counter",
            duration_sum,
            metric_labels,
        )
        builder.metric(
            "palace_relationship_extraction_duration_seconds_count",
            "Relationship classification duration sample count by provider and validation outcome.",
            "counter",
            duration_counts.get(labels, 0),
            metric_labels,
        )
    for (provider,), count in relationship_snapshot["edges"]:
        builder.metric(
            "palace_relationship_edges_extracted_total",
            "Relationship edges stored by classified provider.",
            "counter",
            count,
            {"provider": provider},
        )


async def _query_rows(db: Any, sql: str) -> list[Any]:
    return (await db.execute(text(sql))).mappings().all()


def _add_rows(
    builder: PrometheusTextBuilder,
    *,
    name: str,
    help_text: str,
    rows: list[Any],
    label_columns: tuple[str, ...],
    value_column: str = "count",
) -> None:
    for row in rows:
        labels = {column: row[column] for column in label_columns}
        builder.metric(name, help_text, "gauge", int(row[value_column] or 0), labels)


def _source_type_label(source_type: object) -> str:
    value = str(source_type or "").strip().lower()
    return value if value in _SOURCE_TYPE_LABEL_ALLOWLIST else "other"


def _add_item_rows(builder: PrometheusTextBuilder, rows: list[Any]) -> None:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        counts[(_source_type_label(row["source_type"]), str(row["status"]))] += int(row["count"] or 0)
    for (source_type, status), count in sorted(counts.items()):
        builder.metric(
            "palace_items",
            "Items by normalized source type and status.",
            "gauge",
            count,
            {"source_type": source_type, "status": status},
        )


async def _add_database_metrics(builder: PrometheusTextBuilder, db: Any) -> None:
    try:
        _add_rows(
            builder,
            name="palace_jobs",
            help_text="Jobs by type and status.",
            rows=await _query_rows(db, "SELECT job_type, status, COUNT(*) AS count FROM jobs GROUP BY job_type, status"),
            label_columns=("job_type", "status"),
        )
        _add_rows(
            builder,
            name="palace_memory_jobs",
            help_text="Memory artifact jobs by status.",
            rows=await _query_rows(
                db,
                "SELECT status, COUNT(*) AS count FROM jobs WHERE job_type = 'memory_artifact' GROUP BY status",
            ),
            label_columns=("status",),
        )
        _add_item_rows(
            builder,
            rows=await _query_rows(
                db,
                "SELECT source_type, status, COUNT(*) AS count FROM items WHERE deleted_at IS NULL GROUP BY source_type, status",
            ),
        )
        _add_rows(
            builder,
            name="palace_sync_runs",
            help_text="Palace sync runs by status.",
            rows=await _query_rows(db, "SELECT status, COUNT(*) AS count FROM sync_runs GROUP BY status"),
            label_columns=("status",),
        )
        _add_rows(
            builder,
            name="palace_runs",
            help_text="Palace build runs by status.",
            rows=await _query_rows(db, "SELECT status, COUNT(*) AS count FROM palace_runs GROUP BY status"),
            label_columns=("status",),
        )
        indexed_rows = await _query_rows(
            db,
            """
            SELECT
              COUNT(DISTINCT embeddings.item_id) AS indexed_items,
              COUNT(*) AS embedding_chunks
            FROM embeddings
            JOIN items ON embeddings.item_id = items.id
            WHERE items.deleted_at IS NULL
            """,
        )
        indexed = indexed_rows[0] if indexed_rows else {}
        builder.metric("palace_indexed_items", "Items with at least one embedding chunk.", "gauge", indexed.get("indexed_items", 0))
        builder.metric("palace_embedding_chunks", "Total embedding chunks for non-deleted items.", "gauge", indexed.get("embedding_chunks", 0))

        dirty_rows = await _query_rows(
            db,
            """
            SELECT
              COUNT(palace_dirty_items.id) AS dirty_items,
              COALESCE(SUM(GREATEST(dirty_generation - indexed_generation, 0)), 0) AS backlog_generation
            FROM palace_tenant_state
            LEFT JOIN palace_dirty_items USING (tenant_id)
            """,
        )
        dirty = dirty_rows[0] if dirty_rows else {}
        builder.metric("palace_dirty_backlog_items", "Items waiting for Palace rebuild processing.", "gauge", dirty.get("dirty_items", 0))
        builder.metric(
            "palace_dirty_backlog_generation",
            "Aggregate dirty generation distance across Palace tenants.",
            "gauge",
            dirty.get("backlog_generation", 0),
        )

        webhook_rows = await _query_rows(
            db,
            """
            SELECT status, COUNT(*) AS count
            FROM jobs
            WHERE webhook_url IS NOT NULL
            GROUP BY status
            """,
        )
        _add_rows(
            builder,
            name="palace_webhook_jobs",
            help_text="Webhook-configured jobs by status.",
            rows=webhook_rows,
            label_columns=("status",),
        )
        builder.metric("palace_metrics_database_scrape_error", "Database metric scrape failure state.", "gauge", 0)
    except Exception:
        builder.metric("palace_metrics_database_scrape_error", "Database metric scrape failure state.", "gauge", 1)


async def _add_queue_metrics(builder: PrometheusTextBuilder, arq_pool: Any | None, db: Any) -> None:
    try:
        backpressure = await build_worker_backpressure(arq_pool, db=db)
        for queue in backpressure.queues:
            labels = {"key": queue.key, "queue": queue.queue_name}
            builder.metric("palace_arq_queue_depth", "Ready ARQ jobs by Palace queue group.", "gauge", queue.queued_depth, labels)
            builder.metric("palace_arq_queue_deferred_depth", "Deferred ARQ jobs by Palace queue group.", "gauge", queue.deferred_depth, labels)
            builder.metric(
                "palace_arq_queue_oldest_queued_age_seconds",
                "Oldest ready ARQ job age by Palace queue group.",
                "gauge",
                queue.oldest_queued_age_seconds or 0,
                labels,
            )
            builder.metric("palace_arq_worker_queue_depth", "Worker-reported ARQ queue depth.", "gauge", queue.worker_queue_depth or 0, labels)
            builder.metric("palace_arq_worker_concurrency", "Worker-reported active job count.", "gauge", queue.worker_concurrency or 0, labels)
            builder.metric("palace_arq_recent_failures", "Recent failed jobs by Palace queue group.", "gauge", queue.recent_failed, labels)
            builder.metric("palace_arq_recent_latency_seconds", "Recent average successful job latency.", "gauge", queue.recent_avg_latency_seconds or 0, labels)
        builder.metric("palace_metrics_queue_scrape_error", "Queue metric scrape failure state.", "gauge", 0)
    except Exception:
        builder.metric("palace_metrics_queue_scrape_error", "Queue metric scrape failure state.", "gauge", 1)


async def build_prometheus_metrics(
    *,
    db: Any,
    arq_pool: Any | None,
    http_metrics: HttpMetricsRecorder | None = None,
) -> str:
    builder = PrometheusTextBuilder()
    builder.metric("palace_metrics_scrape", "Successful Palace metrics scrape.", "gauge", 1)
    _add_http_metrics(builder, http_metrics)
    _add_memory_runtime_metrics(builder)
    await _add_database_metrics(builder, db)
    await _add_queue_metrics(builder, arq_pool, db)
    return builder.render()
