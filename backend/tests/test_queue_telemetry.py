from __future__ import annotations

from datetime import datetime, timezone

import pytest
from arq.constants import job_key_prefix, result_key_prefix
from arq.jobs import serialize_job, serialize_result

from app.services.queue_telemetry import build_worker_backpressure
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_WORKER_QUEUE, PALACE_WORKER_QUEUE


class FakeArqPool:
    def __init__(self) -> None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.queues = {
            DEFAULT_WORKER_QUEUE: [
                ("memory-1", now_ms - 120_000),
                ("relationship-1", now_ms + 60_000),
            ],
            MEDIA_WORKER_QUEUE: [
                ("media-1", now_ms - 90_000),
                ("wrong-media-queue-1", now_ms - 30_000),
            ],
            PALACE_WORKER_QUEUE: [
                ("palace-1", now_ms - 30_000),
            ],
        }
        self.values = {
            f"{job_key_prefix}memory-1": serialize_job(
                "memory_artifact",
                (),
                {"job_id": "job-1"},
                None,
                now_ms - 120_000,
            ),
            f"{job_key_prefix}relationship-1": serialize_job(
                "extract_relationships",
                (),
                {"item_id": "item-1"},
                None,
                now_ms + 60_000,
            ),
            f"{job_key_prefix}media-1": serialize_job(
                "process_media",
                (),
                {"job_id": "job-media"},
                None,
                now_ms - 90_000,
            ),
            f"{job_key_prefix}wrong-media-queue-1": serialize_job(
                "extract_relationships",
                (),
                {"item_id": "item-1"},
                None,
                now_ms - 30_000,
            ),
            f"{job_key_prefix}palace-1": serialize_job(
                "palace_run_build",
                (),
                {"palace_run_id": "run-1"},
                None,
                now_ms - 30_000,
            ),
            f"{result_key_prefix}memory-result": serialize_result(
                "memory_artifact",
                (),
                {"job_id": "job-1"},
                1,
                now_ms - 20_000,
                True,
                None,
                now_ms - 15_000,
                now_ms - 2_000,
                "memory-result",
                DEFAULT_WORKER_QUEUE,
                "memory-result",
            ),
            f"{DEFAULT_WORKER_QUEUE}:health-check": b"Apr-26 12:00:00 j_complete=4 j_failed=1 j_retried=0 j_ongoing=3 queued=2",
            f"{MEDIA_WORKER_QUEUE}:health-check": b"Apr-26 12:00:00 j_complete=2 j_failed=0 j_retried=0 j_ongoing=1 queued=1",
            f"{PALACE_WORKER_QUEUE}:health-check": b"Apr-26 12:00:00 j_complete=7 j_failed=0 j_retried=0 j_ongoing=1 queued=1",
        }

    async def zrange(self, queue_name: str, _start: int, _end: int, *, withscores: bool):
        assert withscores is True
        return self.queues[queue_name]

    async def mget(self, keys: list[str]):
        return [self.values.get(key) for key in keys]

    async def get(self, key: str | bytes):
        if isinstance(key, bytes):
            key = key.decode("utf-8")
        return self.values.get(key)

    async def scan_iter(self, *, match: str, count: int):
        assert match == f"{result_key_prefix}*"
        assert count == 100
        for key in self.values:
            if key.startswith(result_key_prefix):
                yield key


class _RowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Row:
    def __init__(
        self,
        *,
        queued_depth: int,
        processing_depth: int,
        oldest_queued_age_seconds: int | None,
        recent_failed: int,
        recent_timeout_count: int,
    ) -> None:
        self.queued_depth = queued_depth
        self.processing_depth = processing_depth
        self.oldest_queued_age_seconds = oldest_queued_age_seconds
        self.recent_failed = recent_failed
        self.recent_timeout_count = recent_timeout_count


class FakeDb:
    def __init__(self) -> None:
        self.aggregate_rows = [
            _Row(
                queued_depth=4,
                processing_depth=1,
                oldest_queued_age_seconds=900,
                recent_failed=2,
                recent_timeout_count=1,
            ),
            _Row(
                queued_depth=1,
                processing_depth=2,
                oldest_queued_age_seconds=120,
                recent_failed=1,
                recent_timeout_count=1,
            ),
            _Row(
                queued_depth=7,
                processing_depth=0,
                oldest_queued_age_seconds=60,
                recent_failed=0,
                recent_timeout_count=0,
            ),
        ]
        self.sample_rows = [
            _Row(
                queued_depth=4,
                processing_depth=1,
                oldest_queued_age_seconds=900,
                recent_failed=2,
                recent_timeout_count=1,
            ),
            _Row(
                queued_depth=1,
                processing_depth=2,
                oldest_queued_age_seconds=120,
                recent_failed=1,
                recent_timeout_count=1,
            ),
        ]
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        rows = self.aggregate_rows if len(self.statements) == 1 else self.sample_rows
        return _RowsResult(rows)


@pytest.mark.asyncio
async def test_build_worker_backpressure_groups_arq_queue_metrics() -> None:
    summary = await build_worker_backpressure(FakeArqPool(), db=FakeDb())
    by_key = {queue.key: queue for queue in summary.queues}

    assert by_key["memory"].queued_depth == 1
    assert by_key["memory"].oldest_queued_age_seconds is not None
    assert by_key["memory"].oldest_queued_age_seconds >= 100
    assert by_key["memory"].worker_concurrency == 3
    assert by_key["memory"].worker_queue_depth == 2
    assert by_key["memory"].recent_completed == 1
    assert by_key["memory"].recent_avg_latency_seconds == 18.0

    assert by_key["media_ingest"].queued_depth == 1
    assert by_key["media_ingest"].queue_name == MEDIA_WORKER_QUEUE
    assert by_key["media_ingest"].worker_concurrency == 1
    assert by_key["media_ingest"].worker_queue_depth == 1
    assert by_key["media_ingest"].unexpected_function_count == 1
    assert by_key["media_ingest"].unexpected_functions == ["extract_relationships"]
    assert by_key["media_ingest"].db_queued_depth == 12
    assert by_key["media_ingest"].db_processing_depth == 3
    assert by_key["media_ingest"].oldest_db_queued_age_seconds == 900
    assert by_key["media_ingest"].queued_tenant_count == 3
    assert by_key["media_ingest"].processing_tenant_count == 2
    assert by_key["media_ingest"].max_queued_per_tenant == 7
    assert by_key["media_ingest"].max_processing_per_tenant == 2
    assert by_key["media_ingest"].recent_failed == 3
    assert by_key["media_ingest"].recent_timeout_count == 2
    assert by_key["media_ingest"].tenant_pressure == [
        {
            "rank": 1,
            "queued_depth": 4,
            "processing_depth": 1,
            "oldest_queued_age_seconds": 900,
            "recent_failed": 2,
            "recent_timeout_count": 1,
        },
        {
            "rank": 2,
            "queued_depth": 1,
            "processing_depth": 2,
            "oldest_queued_age_seconds": 120,
            "recent_failed": 1,
            "recent_timeout_count": 1,
        },
    ]
    assert "tenant_id" not in by_key["media_ingest"].tenant_pressure[0]

    assert by_key["relationships"].queued_depth == 0
    assert by_key["relationships"].deferred_depth == 1
    assert by_key["relationships"].unexpected_function_count == 0
    assert by_key["palace_builds"].queued_depth == 1
    assert by_key["palace_builds"].worker_concurrency == 1
