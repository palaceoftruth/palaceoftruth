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

_SOURCE_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)


def _source_duration_bucket_select(*, field: str, prefix: str) -> str:
    """Build fixed, replica-aggregatable histogram bucket projections."""

    return ",\n              ".join(
        f"COUNT(*) FILTER (WHERE ({field})::double precision <= {boundary}) AS {prefix}_{_format_value(boundary).replace('.', '_')}"
        for boundary in _SOURCE_DURATION_BUCKETS
    )

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
_JOB_TYPE_LABEL_ALLOWLIST = frozenset(
    {"memory_artifact", "note", "doc", "pdf", "webpage", "youtube", "feed", "image", "media", "bundle"}
)
_JOB_STATUS_LABEL_ALLOWLIST = frozenset({"queued", "processing", "completed", "failed", "duplicate"})
_JOB_ATTEMPT_STATUS_LABEL_ALLOWLIST = frozenset(
    {"queued", "processing", "completed", "failed", "dead_lettered"}
)
_JOB_ATTEMPT_TRIGGER_LABEL_ALLOWLIST = frozenset(
    {"initial", "arq_retry", "manual_retry", "stale_recovery"}
)
_JOB_FAILURE_KIND_LABEL_ALLOWLIST = frozenset(
    {"worker_error", "worker_cancelled", "enqueue_failed", "non_retryable", "max_attempts"}
)
_SOURCE_KIND_LABEL_ALLOWLIST = frozenset({"http"})
_SOURCE_STATUS_LABEL_ALLOWLIST = frozenset({"active", "unreachable", "gone", "paused"})


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

    def histogram(
        self,
        name: str,
        help_text: str,
        *,
        buckets: tuple[float, ...],
        counts: list[int],
        count: int,
        value_sum: float,
        labels: dict[str, object],
    ) -> None:
        if name not in self._declared:
            self._lines.append(f"# HELP {name} {help_text}")
            self._lines.append(f"# TYPE {name} histogram")
            self._declared.add(name)
        for boundary, bucket_count in zip(buckets, counts, strict=True):
            self._lines.append(
                f'{name}_bucket{_format_labels({**labels, "le": _format_value(boundary)})} {bucket_count}'
            )
        self._lines.append(f'{name}_bucket{_format_labels({**labels, "le": "+Inf"})} {count}')
        self._lines.append(f"{name}_sum{_format_labels(labels)} {_format_value(value_sum)}")
        self._lines.append(f"{name}_count{_format_labels(labels)} {count}")


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
    retrieval_label_names = ("endpoint", "outcome")
    for labels, count in snapshot["retrieval_requests"]:
        builder.metric(
            "palace_retrieval_requests_total",
            "Retrieval requests by bounded outcome and routing classification.",
            "counter",
            count,
            dict(zip(retrieval_label_names, labels, strict=True)),
        )
    for labels, count in snapshot["retrieval_classifications"]:
        builder.metric(
            "palace_retrieval_classifications_total",
            "Retrieval decisions split into bounded low-cardinality dimensions.",
            "counter",
            count,
            dict(zip(("endpoint", "dimension", "value"), labels, strict=True)),
        )
    result_label_names = ("endpoint", "rank_band", "freshness", "trust_class", "source_support_state")
    for labels, count in snapshot["retrieval_results"]:
        builder.metric(
            "palace_retrieval_results_total",
            "Returned retrieval results by bounded rank and evidence classification.",
            "counter",
            count,
            dict(zip(result_label_names, labels, strict=True)),
        )
    histogram_specs = {
        "retrieval_stage_duration": (
            "palace_retrieval_stage_duration_seconds",
            "Retrieval stage latency using fixed replica-aggregatable buckets.",
            ("endpoint", "stage"),
        ),
        "embedding_duration": (
            "palace_embedding_duration_seconds",
            "Embedding provider request latency using fixed buckets.",
            ("provider", "input_type", "status", "failure_kind"),
        ),
        "embedding_batch_size": (
            "palace_embedding_batch_size",
            "Embedding inputs per provider request.",
            ("provider", "input_type"),
        ),
        "embedding_input_tokens": (
            "palace_embedding_input_tokens",
            "Estimated embedding input tokens per provider request.",
            ("provider", "input_type"),
        ),
    }
    for snapshot_name, labels, state in snapshot["histograms"]:
        metric_name, help_text, label_names = histogram_specs[snapshot_name]
        builder.histogram(
            metric_name,
            help_text,
            buckets=state["buckets"],
            counts=state["counts"],
            count=state["count"],
            value_sum=state["sum"],
            labels=dict(zip(label_names, labels, strict=True)),
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


def _allowlisted_label(value: object, allowed: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else "other"


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
        attempt_rows = await _query_rows(
            db,
            """
            SELECT j.job_type, a.status, a.trigger, a.failure_kind, COUNT(*) AS count
            FROM job_attempts a
            JOIN jobs j ON j.id = a.job_id
            GROUP BY j.job_type, a.status, a.trigger, a.failure_kind
            """,
        )
        attempt_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        recovery_counts: dict[tuple[str, str], int] = defaultdict(int)
        dead_letter_counts: dict[tuple[str, str], int] = defaultdict(int)
        for row in attempt_rows:
            job_type = _allowlisted_label(row["job_type"], _JOB_TYPE_LABEL_ALLOWLIST)
            status = _allowlisted_label(row["status"], _JOB_ATTEMPT_STATUS_LABEL_ALLOWLIST)
            trigger = _allowlisted_label(row["trigger"], _JOB_ATTEMPT_TRIGGER_LABEL_ALLOWLIST)
            count = int(row["count"] or 0)
            attempt_counts[(job_type, status, trigger)] += count
            if trigger == "stale_recovery":
                recovery_counts[(job_type, status)] += count
            if status == "dead_lettered":
                failure_kind = _allowlisted_label(
                    row["failure_kind"], _JOB_FAILURE_KIND_LABEL_ALLOWLIST
                )
                dead_letter_counts[(job_type, failure_kind)] += count
        for (job_type, status, trigger), count in sorted(attempt_counts.items()):
            builder.metric(
                "palace_job_attempts",
                "Durable job attempts by bounded type, status, and trigger.",
                "gauge",
                count,
                {"job_type": job_type, "status": status, "trigger": trigger},
            )
        if not attempt_counts:
            builder.metric(
                "palace_job_attempts",
                "Durable job attempts by bounded type, status, and trigger.",
                "gauge",
                0,
                {"job_type": "other", "status": "other", "trigger": "other"},
            )
        for (job_type, outcome), count in sorted(recovery_counts.items()):
            builder.metric(
                "palace_job_recoveries",
                "Durable stale-recovery attempts by bounded type and outcome.",
                "gauge",
                count,
                {"job_type": job_type, "outcome": outcome},
            )
        if not recovery_counts:
            builder.metric(
                "palace_job_recoveries",
                "Durable stale-recovery attempts by bounded type and outcome.",
                "gauge",
                0,
                {"job_type": "other", "outcome": "other"},
            )
        for (job_type, failure_kind), count in sorted(dead_letter_counts.items()):
            builder.metric(
                "palace_job_dead_letters",
                "Durable dead-lettered attempts by bounded type and failure class.",
                "gauge",
                count,
                {"job_type": job_type, "failure_kind": failure_kind},
            )
        if not dead_letter_counts:
            builder.metric(
                "palace_job_dead_letters",
                "Durable dead-lettered attempts by bounded type and failure class.",
                "gauge",
                0,
                {"job_type": "other", "failure_kind": "other"},
            )
        job_age_rows = await _query_rows(
                db,
                """
                SELECT job_type, status,
                       EXTRACT(EPOCH FROM (NOW() - MIN(created_at)))::bigint AS age_seconds
                FROM jobs
                WHERE status IN ('queued', 'processing')
                GROUP BY job_type, status
                """,
            )
        bounded_job_ages: dict[tuple[str, str], int] = {}
        for row in job_age_rows:
            labels = (
                _allowlisted_label(row["job_type"], _JOB_TYPE_LABEL_ALLOWLIST),
                _allowlisted_label(row["status"], _JOB_STATUS_LABEL_ALLOWLIST),
            )
            bounded_job_ages[labels] = max(bounded_job_ages.get(labels, 0), int(row["age_seconds"] or 0))
        for (job_type, status), age_seconds in sorted(bounded_job_ages.items()):
            builder.metric(
                "palace_jobs_oldest_age_seconds",
                "Oldest durable job age by bounded job type and status.",
                "gauge",
                age_seconds,
                {"job_type": job_type, "status": status},
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

        source_rows = await _query_rows(
            db,
            """
            SELECT kind, status,
                   COUNT(*) AS count,
                   EXTRACT(EPOCH FROM (NOW() - MIN(last_success_at)))::bigint
                     AS oldest_success_age_seconds,
                   COUNT(*) FILTER (WHERE last_success_at IS NULL) AS never_success_count,
                   COUNT(*) FILTER (WHERE next_due_at IS NOT NULL AND next_due_at <= NOW()) AS due_count,
                   COALESCE(MAX(EXTRACT(EPOCH FROM (NOW() - next_due_at))) FILTER (
                     WHERE next_due_at IS NOT NULL AND next_due_at <= NOW()
                   ), 0)::bigint AS oldest_due_age_seconds
            FROM source_resources
            GROUP BY kind, status
            """,
        )
        for row in source_rows:
            labels = {
                "kind": _allowlisted_label(row["kind"], _SOURCE_KIND_LABEL_ALLOWLIST),
                "status": _allowlisted_label(row["status"], _SOURCE_STATUS_LABEL_ALLOWLIST),
            }
            builder.metric("palace_source_resources", "Source resources by kind and status.", "gauge", row["count"], labels)
            if row["oldest_success_age_seconds"] is not None:
                builder.metric(
                    "palace_source_last_success_age_seconds",
                    "Oldest successful source refresh age by kind and status.",
                    "gauge",
                    row["oldest_success_age_seconds"],
                    labels,
                )
            builder.metric(
                "palace_source_never_succeeded",
                "Source resources with no successful refresh by kind and status.",
                "gauge",
                row["never_success_count"],
                labels,
            )
            builder.metric("palace_source_refresh_due", "Source resources currently due for refresh.", "gauge", row["due_count"], labels)
            builder.metric(
                "palace_source_refresh_oldest_due_age_seconds",
                "Oldest overdue source refresh age by kind and status.",
                "gauge",
                row["oldest_due_age_seconds"],
                labels,
            )

        refresh_bucket_select = _source_duration_bucket_select(
            field="next_snapshot->>'refresh_duration_seconds'", prefix="refresh_duration_le"
        )
        change_to_index_bucket_select = _source_duration_bucket_select(
            field="next_snapshot->>'change_to_index_seconds'", prefix="change_to_index_le"
        )
        source_refresh_rows = await _query_rows(
            db,
            f"""
            SELECT
              next_snapshot->>'outcome' AS outcome,
              next_snapshot->>'validator' AS validator,
              next_snapshot->>'change' AS change,
              COUNT(*) AS count,
              COALESCE(SUM((next_snapshot->>'refresh_duration_seconds')::double precision), 0) AS refresh_duration_sum,
              {refresh_bucket_select},
              COUNT(next_snapshot->>'change_to_index_seconds') AS change_to_index_count,
              COALESCE(SUM((next_snapshot->>'change_to_index_seconds')::double precision), 0) AS change_to_index_sum,
              {change_to_index_bucket_select}
            FROM source_resource_audit_snapshots
            WHERE event_kind = 'refresh_telemetry'
            GROUP BY next_snapshot->>'outcome', next_snapshot->>'validator', next_snapshot->>'change'
            """,
        )
        change_to_index_count = 0
        change_to_index_sum = 0.0
        change_to_index_bucket_counts = [0] * len(_SOURCE_DURATION_BUCKETS)
        for row in source_refresh_rows:
            labels = {
                "outcome": _allowlisted_label(row["outcome"], frozenset({"success", "not_modified", "failure", "gone"})),
                "validator": _allowlisted_label(row["validator"], frozenset({"etag", "last_modified", "none"})),
                "change": _allowlisted_label(row["change"], frozenset({"changed", "unchanged", "unknown"})),
            }
            builder.metric("palace_source_refreshes_total", "Durably committed HTTP source refreshes.", "counter", row["count"], labels)
            builder.histogram(
                "palace_source_refresh_duration_seconds",
                "Durable source refresh latency using fixed replica-aggregatable buckets.",
                buckets=_SOURCE_DURATION_BUCKETS,
                counts=[int(row.get(f"refresh_duration_le_{_format_value(boundary).replace('.', '_')}") or 0) for boundary in _SOURCE_DURATION_BUCKETS],
                count=int(row["count"]),
                value_sum=float(row["refresh_duration_sum"]),
                labels=labels,
            )
            change_to_index_count += int(row["change_to_index_count"] or 0)
            change_to_index_sum += float(row["change_to_index_sum"] or 0)
            for index, boundary in enumerate(_SOURCE_DURATION_BUCKETS):
                change_to_index_bucket_counts[index] += int(
                    row.get(f"change_to_index_le_{_format_value(boundary).replace('.', '_')}") or 0
                )
        if change_to_index_count:
            builder.histogram(
                "palace_source_change_to_index_duration_seconds",
                "Durable changed-content activation latency using fixed replica-aggregatable buckets.",
                buckets=_SOURCE_DURATION_BUCKETS,
                counts=change_to_index_bucket_counts,
                count=change_to_index_count,
                value_sum=change_to_index_sum,
                labels={},
            )

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
            builder.metric(
                "palace_arq_worker_available",
                "Whether a fresh ARQ worker heartbeat is available for this queue group.",
                "gauge",
                queue.worker_available,
                labels,
            )
            builder.metric(
                "palace_arq_worker_instances",
                "Number of ARQ worker instances with a fresh heartbeat for this queue group.",
                "gauge",
                queue.worker_instance_count,
                labels,
            )
            if queue.worker_heartbeat_age_seconds is not None:
                builder.metric(
                    "palace_arq_worker_heartbeat_age_seconds",
                    "Age of the latest ARQ worker heartbeat for this queue group.",
                    "gauge",
                    queue.worker_heartbeat_age_seconds,
                    labels,
                )
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
