from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from app.models.job import Job
from app.utils.webhook import maybe_dispatch_webhook
from app.workers.webhook_tasks import deliver_webhook


class _FakeSession:
    def __init__(self, job: Job | None) -> None:
        self.job = job

    async def get(self, _model, key):
        if self.job and key == self.job.id:
            return self.job
        return None


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


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.mark.asyncio
async def test_maybe_dispatch_webhook_snapshots_memory_status(monkeypatch) -> None:
    job_id = uuid.uuid4()
    job = Job(
        id=job_id,
        job_type="memory_artifact",
        tenant_id="tenant-a",
        status="completed",
        progress=100,
        error_message=None,
        completed_at=datetime(2026, 4, 18, 16, 0, tzinfo=timezone.utc),
        webhook_url="https://example.com/hook",
        signing_key="signing-key",
    )
    redis = _FakeRedis()

    monkeypatch.setattr(
        "app.database.async_session",
        lambda: _FakeSessionManager(_FakeSession(job)),
    )

    await maybe_dispatch_webhook(redis, str(job_id))

    assert redis.enqueued == [
        (
            "deliver_webhook",
            {
                "job_id": str(job_id),
                "webhook_url": "https://example.com/hook",
                "signing_key": "signing-key",
                "payload_snapshot": {
                    "job_id": str(job_id),
                    "status": "complete",
                    "contract_status": "completed",
                    "error_message": None,
                    "duplicate_of": None,
                    "created_at": None,
                    "completed_at": "2026-04-18T16:00:00Z",
                    "poll_after_seconds": 5,
                    "retryable": False,
                    "retry_after_seconds": None,
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_deliver_webhook_uses_snapshot_even_if_job_state_changes(monkeypatch) -> None:
    job_id = uuid.uuid4()
    delivered: list[dict] = []
    job = Job(
        id=job_id,
        item_id=uuid.uuid4(),
        job_type="note",
        tenant_id="tenant-a",
        status="queued",
        progress=0,
        error_message=None,
        completed_at=None,
    )

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, content: bytes, headers: dict) -> _FakeResponse:
            delivered.append(
                {
                    "url": url,
                    "body": json.loads(content.decode()),
                    "headers": headers,
                }
            )
            return _FakeResponse(200)

    monkeypatch.setattr(
        "app.workers.webhook_tasks.async_session",
        lambda: _FakeSessionManager(_FakeSession(job)),
    )
    monkeypatch.setattr("app.workers.webhook_tasks.httpx.AsyncClient", _FakeAsyncClient)

    await deliver_webhook(
        {"redis": _FakeRedis()},
        job_id=str(job_id),
        webhook_url="https://example.com/hook",
        signing_key="signing-key",
        attempt=1,
        payload_snapshot={
            "id": str(job_id),
            "item_id": str(job.item_id),
            "job_type": "note",
            "status": "failed",
            "progress": 100,
            "error_message": "Original failure",
            "duplicate_of": None,
            "created_at": None,
            "completed_at": "2026-04-18T16:10:00Z",
        },
    )

    assert delivered[0]["url"] == "https://example.com/hook"
    assert delivered[0]["body"] == {
        "id": str(job_id),
        "item_id": str(job.item_id),
        "job_type": "note",
        "status": "failed",
        "progress": 100,
        "error_message": "Original failure",
        "duplicate_of": None,
        "created_at": None,
        "completed_at": "2026-04-18T16:10:00Z",
    }
    assert delivered[0]["headers"]["Content-Type"] == "application/json"
    assert delivered[0]["headers"]["X-Hub-Signature-256"].startswith("sha256=")
