from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from arq.jobs import JobStatus

from app.workers.media_fairness import dispatch_tenant_fair_media_jobs
from app.workers.queues import MEDIA_WORKER_QUEUE


class _FakeFetchResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, *, pending_rows, active_rows) -> None:
        self.pending_rows = pending_rows
        self.active_rows = active_rows
        self.execute_calls = 0
        self.statements: list[str] = []

    async def execute(self, _statement, _params=None):
        self.execute_calls += 1
        self.statements.append(str(_statement))
        if self.execute_calls == 1:
            return _FakeFetchResult(self.pending_rows)
        return _FakeFetchResult(self.active_rows)


class _FakeSessionManager:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedis:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


class _FakeArqJob:
    statuses: dict[str, JobStatus] = {}

    def __init__(self, job_id: str, *, redis, _queue_name: str) -> None:
        self.job_id = job_id
        self.redis = redis
        self.queue_name = _queue_name

    async def status(self):
        return self.statuses.get(self.job_id, JobStatus.not_found)


def _media_row(
    *,
    tenant_id: str,
    created_at: datetime,
    source_url: str,
    payload: dict | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        job_type="media",
        payload=payload,
        created_at=created_at,
        source_url=source_url,
    )


@pytest.mark.asyncio
async def test_dispatch_tenant_fair_media_jobs_round_robins_noisy_tenant(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    tenant_a_first = _media_row(tenant_id="tenant-a", created_at=now, source_url="https://example.com/a1")
    tenant_a_second = _media_row(
        tenant_id="tenant-a",
        created_at=now + timedelta(seconds=1),
        source_url="https://example.com/a2",
    )
    tenant_b_first = _media_row(
        tenant_id="tenant-b",
        created_at=now + timedelta(seconds=2),
        source_url="https://example.com/b1",
        payload={
            "retry_task": {
                "name": "process_media",
                "kwargs": {"url": "https://example.com/stale", "model": "media-model"},
            }
        },
    )
    session = _FakeSession(pending_rows=[tenant_a_first, tenant_a_second, tenant_b_first], active_rows=[])
    redis = _FakeRedis()
    _FakeArqJob.statuses = {}

    monkeypatch.setattr("app.workers.media_fairness.async_session", lambda: _FakeSessionManager(session))
    monkeypatch.setattr("app.workers.media_fairness.ArqJob", _FakeArqJob)

    dispatched = await dispatch_tenant_fair_media_jobs(
        {"redis": redis},
        limit=2,
        per_tenant_limit=1,
        candidate_limit=10,
    )

    assert dispatched == 2
    assert "pending_availability" in session.statements[0]
    assert "retry_after_at" in session.statements[0]
    assert redis.enqueued == [
        (
            "process_media",
            {
                "_job_id": str(tenant_a_first.id),
                "_queue_name": MEDIA_WORKER_QUEUE,
                "job_id": str(tenant_a_first.id),
                "tenant_id": "tenant-a",
                "url": "https://example.com/a1",
            },
        ),
        (
            "process_media",
            {
                "_job_id": str(tenant_b_first.id),
                "_queue_name": MEDIA_WORKER_QUEUE,
                "job_id": str(tenant_b_first.id),
                "tenant_id": "tenant-b",
                "url": "https://example.com/b1",
                "model": "media-model",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_dispatch_counts_active_arq_jobs_against_tenant_cap(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    tenant_a_active = _media_row(tenant_id="tenant-a", created_at=now, source_url="https://example.com/a1")
    tenant_a_waiting = _media_row(
        tenant_id="tenant-a",
        created_at=now + timedelta(seconds=1),
        source_url="https://example.com/a2",
    )
    tenant_b_waiting = _media_row(
        tenant_id="tenant-b",
        created_at=now + timedelta(seconds=2),
        source_url="https://example.com/b1",
    )
    session = _FakeSession(pending_rows=[tenant_a_active, tenant_a_waiting, tenant_b_waiting], active_rows=[])
    redis = _FakeRedis()
    _FakeArqJob.statuses = {str(tenant_a_active.id): JobStatus.queued}

    monkeypatch.setattr("app.workers.media_fairness.async_session", lambda: _FakeSessionManager(session))
    monkeypatch.setattr("app.workers.media_fairness.ArqJob", _FakeArqJob)

    dispatched = await dispatch_tenant_fair_media_jobs(
        {"redis": redis},
        limit=2,
        per_tenant_limit=1,
        candidate_limit=10,
    )

    assert dispatched == 1
    assert redis.enqueued == [
        (
            "process_media",
            {
                "_job_id": str(tenant_b_waiting.id),
                "_queue_name": MEDIA_WORKER_QUEUE,
                "job_id": str(tenant_b_waiting.id),
                "tenant_id": "tenant-b",
                "url": "https://example.com/b1",
            },
        )
    ]


@pytest.mark.asyncio
async def test_dispatch_candidate_query_ranks_per_tenant(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    tenant_a_first = _media_row(tenant_id="tenant-a", created_at=now, source_url="https://example.com/a1")
    session = _FakeSession(pending_rows=[tenant_a_first], active_rows=[])
    redis = _FakeRedis()
    _FakeArqJob.statuses = {}

    monkeypatch.setattr("app.workers.media_fairness.async_session", lambda: _FakeSessionManager(session))
    monkeypatch.setattr("app.workers.media_fairness.ArqJob", _FakeArqJob)

    await dispatch_tenant_fair_media_jobs(
        {"redis": redis},
        limit=1,
        per_tenant_limit=1,
        candidate_limit=1,
    )

    pending_query = session.statements[0]
    assert "ROW_NUMBER() OVER" in pending_query
    assert "PARTITION BY j.tenant_id" in pending_query
    assert "tenant_rank <= :candidate_limit" in pending_query
