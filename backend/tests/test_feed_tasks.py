from __future__ import annotations

import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from arq.jobs import JobStatus

from app.workers import feed_tasks
from app.workers.feed_tasks import requeue_stale_jobs
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_FAIR_DISPATCH_TASK_NAME, MEDIA_WORKER_QUEUE, singleton_job_id


class _FakeFetchResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, stale_rows, jobs) -> None:
        self.stale_rows = stale_rows
        self.jobs = jobs
        self.commits = 0

    async def execute(self, _statement):
        return _FakeFetchResult(self.stale_rows)

    async def get(self, _model, key):
        return self.jobs.get(key)

    async def commit(self) -> None:
        self.commits += 1


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
    status_value = JobStatus.not_found

    def __init__(self, job_id: str, *, redis, _queue_name: str) -> None:
        self.job_id = job_id
        self.redis = redis
        self.queue_name = _queue_name

    async def status(self):
        return self.status_value


@pytest.mark.asyncio
async def test_requeue_stale_pdf_rebuilds_payload_and_preserves_tenant(monkeypatch) -> None:
    job_id = uuid.uuid4()
    stale_rows = [
        SimpleNamespace(
            id=job_id,
            job_type="pdf",
            status="processing",
            item_id=uuid.uuid4(),
            tenant_id="tenant-a",
            payload=None,
            source_url=None,
            title="Recovered PDF",
            raw_content=None,
        )
    ]
    jobs = {
        job_id: SimpleNamespace(
            id=job_id,
            status="processing",
            progress=50,
            error_message="stuck",
            completed_at=None,
        )
    }
    session = _FakeSession(stale_rows, jobs)
    redis = _FakeRedis()

    monkeypatch.setattr(
        "app.workers.feed_tasks.async_session",
        lambda: _FakeSessionManager(session),
    )
    async def _requeueable(_redis, _job_id, **_kwargs):
        return True

    monkeypatch.setattr("app.workers.feed_tasks._stale_job_is_requeueable", _requeueable)
    monkeypatch.setattr("app.workers.feed_tasks.os.path.exists", lambda path: str(path).endswith(f"{job_id}.pdf"))
    monkeypatch.setattr(
        "app.workers.feed_tasks._extract_pdf_retry_payload",
        lambda _path: ("Recovered PDF text", {"page_count": 1}),
    )

    async def _noop_webhook(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.workers.feed_tasks.maybe_dispatch_webhook", _noop_webhook)

    await requeue_stale_jobs({"redis": redis})

    assert redis.enqueued == [
        (
            "process_pdf",
            {
                "_job_id": str(job_id),
                "job_id": str(job_id),
                "tenant_id": "tenant-a",
                "extracted_text": "Recovered PDF text",
                "pdf_metadata": {"page_count": 1},
            },
        )
    ]
    assert jobs[job_id].status == "queued"
    assert jobs[job_id].progress == 0
    assert jobs[job_id].error_message is None


@pytest.mark.asyncio
async def test_requeue_stale_pdf_uses_persisted_payload_when_source_file_is_gone(monkeypatch) -> None:
    job_id = uuid.uuid4()
    retry_payload = {
        "retry_task": {
            "name": "process_pdf",
            "kwargs": {
                "extracted_text": "Persisted PDF text",
                "pdf_metadata": {"page_count": 2, "source": "payload"},
                "model": "pdf-model",
            },
        }
    }
    stale_rows = [
        SimpleNamespace(
            id=job_id,
            job_type="pdf",
            status="processing",
            item_id=uuid.uuid4(),
            tenant_id="tenant-a",
            source_url=None,
            title="Persisted PDF",
            raw_content=None,
            payload=retry_payload,
        )
    ]
    jobs = {
        job_id: SimpleNamespace(
            id=job_id,
            status="processing",
            progress=50,
            error_message="stuck",
            completed_at=None,
            payload=retry_payload,
        )
    }
    session = _FakeSession(stale_rows, jobs)
    redis = _FakeRedis()

    monkeypatch.setattr(
        "app.workers.feed_tasks.async_session",
        lambda: _FakeSessionManager(session),
    )
    async def _requeueable(_redis, _job_id, **_kwargs):
        return True

    monkeypatch.setattr("app.workers.feed_tasks._stale_job_is_requeueable", _requeueable)
    monkeypatch.setattr("app.workers.feed_tasks.os.path.exists", lambda _path: False)

    def _unexpected_extract(_path: str) -> tuple[str, dict]:
        raise AssertionError("should not rebuild payload when job.payload is present")

    monkeypatch.setattr(
        "app.workers.feed_tasks._extract_pdf_retry_payload",
        _unexpected_extract,
    )

    async def _noop_webhook(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.workers.feed_tasks.maybe_dispatch_webhook", _noop_webhook)

    await requeue_stale_jobs({"redis": redis})

    assert redis.enqueued == [
        (
            "process_pdf",
            {
                "_job_id": str(job_id),
                "job_id": str(job_id),
                "tenant_id": "tenant-a",
                "extracted_text": "Persisted PDF text",
                "pdf_metadata": {"page_count": 2, "source": "payload"},
                "model": "pdf-model",
            },
        )
    ]
    assert jobs[job_id].status == "queued"
    assert jobs[job_id].progress == 0
    assert jobs[job_id].error_message is None


@pytest.mark.asyncio
async def test_requeue_stale_job_skips_active_arq_job(monkeypatch) -> None:
    job_id = uuid.uuid4()
    stale_rows = [
        SimpleNamespace(
            id=job_id,
            job_type="media",
            status="processing",
            item_id=uuid.uuid4(),
            tenant_id="tenant-a",
            source_url="https://example.com/watch?v=active",
            title="Active media",
            raw_content=None,
            payload=None,
        )
    ]
    jobs = {
        job_id: SimpleNamespace(
            id=job_id,
            status="processing",
            progress=10,
            error_message=None,
            completed_at=None,
        )
    }
    session = _FakeSession(stale_rows, jobs)
    redis = _FakeRedis()

    monkeypatch.setattr("app.workers.feed_tasks.async_session", lambda: _FakeSessionManager(session))
    async def _not_requeueable(_redis, _job_id, **_kwargs):
        return False

    monkeypatch.setattr("app.workers.feed_tasks._stale_job_is_requeueable", _not_requeueable)

    await requeue_stale_jobs({"redis": redis})

    assert redis.enqueued == []
    assert jobs[job_id].status == "processing"
    assert jobs[job_id].progress == 10


@pytest.mark.asyncio
async def test_requeue_stale_media_uses_media_queue_and_checks_media_arq_state(monkeypatch) -> None:
    job_id = uuid.uuid4()
    stale_rows = [
        SimpleNamespace(
            id=job_id,
            job_type="media",
            status="processing",
            item_id=uuid.uuid4(),
            tenant_id="tenant-a",
            source_url="https://example.com/watch?v=stale",
            title="Stale media",
            raw_content=None,
            payload=None,
        )
    ]
    jobs = {
        job_id: SimpleNamespace(
            id=job_id,
            status="processing",
            progress=30,
            error_message="stuck",
            completed_at=None,
        )
    }
    session = _FakeSession(stale_rows, jobs)
    redis = _FakeRedis()
    inspected: list[tuple[str, str]] = []

    monkeypatch.setattr("app.workers.feed_tasks.async_session", lambda: _FakeSessionManager(session))

    async def _requeueable(_redis, checked_job_id, *, queue_name):
        inspected.append((checked_job_id, queue_name))
        return True

    monkeypatch.setattr("app.workers.feed_tasks._stale_job_is_requeueable", _requeueable)

    await requeue_stale_jobs({"redis": redis})

    assert inspected == [(str(job_id), MEDIA_WORKER_QUEUE), (str(job_id), "arq:queue")]
    assert redis.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]
    assert jobs[job_id].status == "queued"
    assert jobs[job_id].progress == 0


@pytest.mark.asyncio
async def test_requeue_stale_media_refreshes_payload_url_and_resets_item_state(monkeypatch) -> None:
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    stale_created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    stale_rows = [
        SimpleNamespace(
            id=job_id,
            job_type="media",
            status="processing",
            item_id=item_id,
            tenant_id="tenant-a",
            source_url="https://example.com/watch?v=current",
            title="Stale media",
            raw_content=None,
            payload={
                "retry_task": {
                    "name": "process_media",
                    "kwargs": {
                        "url": "https://example.com/watch?v=stale",
                        "model": "media-model",
                    },
                }
            },
        )
    ]
    jobs = {
        job_id: SimpleNamespace(
            id=job_id,
            item_id=item_id,
            status="processing",
            progress=30,
            error_message="stuck",
            duplicate_of=uuid.uuid4(),
            created_at=stale_created_at,
            completed_at=None,
        ),
        item_id: SimpleNamespace(
            id=item_id,
            status="failed",
        ),
    }
    session = _FakeSession(stale_rows, jobs)
    redis = _FakeRedis()

    monkeypatch.setattr("app.workers.feed_tasks.async_session", lambda: _FakeSessionManager(session))

    async def _requeueable(_redis, _job_id, **_kwargs):
        return True

    monkeypatch.setattr("app.workers.feed_tasks._stale_job_is_requeueable", _requeueable)

    await requeue_stale_jobs({"redis": redis})

    assert redis.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]
    assert jobs[job_id].status == "queued"
    assert jobs[job_id].progress == 0
    assert jobs[job_id].error_message is None
    assert jobs[job_id].duplicate_of is None
    assert jobs[job_id].created_at > stale_created_at
    assert jobs[item_id].status == "processing"


@pytest.mark.asyncio
async def test_requeue_stale_media_with_payload_fails_without_source_url(monkeypatch) -> None:
    job_id = uuid.uuid4()
    stale_rows = [
        SimpleNamespace(
            id=job_id,
            job_type="media",
            status="processing",
            item_id=None,
            tenant_id="tenant-a",
            source_url=None,
            title="Orphan media",
            raw_content=None,
            payload={
                "retry_task": {
                    "name": "process_media",
                    "kwargs": {
                        "url": "https://example.com/watch?v=orphan",
                        "model": "media-model",
                    },
                }
            },
        )
    ]
    jobs = {
        job_id: SimpleNamespace(
            id=job_id,
            status="processing",
            progress=30,
            error_message="stuck",
            completed_at=None,
        )
    }
    session = _FakeSession(stale_rows, jobs)
    redis = _FakeRedis()
    webhook_jobs: list[str] = []

    monkeypatch.setattr("app.workers.feed_tasks.async_session", lambda: _FakeSessionManager(session))

    async def _requeueable(_redis, _job_id, **_kwargs):
        return True

    async def _webhook(_redis, checked_job_id):
        webhook_jobs.append(checked_job_id)

    monkeypatch.setattr("app.workers.feed_tasks._stale_job_is_requeueable", _requeueable)
    monkeypatch.setattr("app.workers.feed_tasks.maybe_dispatch_webhook", _webhook)

    await requeue_stale_jobs({"redis": redis})

    assert redis.enqueued == []
    assert jobs[job_id].status == "failed"
    assert jobs[job_id].error_message == "Stale job with no source URL — cannot requeue"
    assert jobs[job_id].completed_at is not None
    assert webhook_jobs == [str(job_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.queued, JobStatus.deferred, JobStatus.in_progress, JobStatus.complete])
async def test_stale_job_requeue_guard_skips_active_or_completed_arq_state(monkeypatch, status) -> None:
    _FakeArqJob.status_value = status
    monkeypatch.setattr(feed_tasks, "ArqJob", _FakeArqJob)

    assert await feed_tasks._stale_job_is_requeueable(object(), "job-1") is False


@pytest.mark.asyncio
async def test_stale_job_requeue_guard_allows_missing_arq_state(monkeypatch) -> None:
    _FakeArqJob.status_value = JobStatus.not_found
    monkeypatch.setattr(feed_tasks, "ArqJob", _FakeArqJob)

    assert await feed_tasks._stale_job_is_requeueable(object(), "job-1") is True
