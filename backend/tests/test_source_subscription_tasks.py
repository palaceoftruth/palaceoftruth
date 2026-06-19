from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.source_subscription import SourceSubscription
from app.workers.source_subscription_tasks import (
    diagnose_stale_queued_source_subscription_entries_task,
    poll_all_source_subscriptions,
)


class _FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _FakeResult(self.rows)


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeRedis:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, name, **kwargs):
        self.enqueued.append((name, kwargs))


@pytest.mark.asyncio
async def test_poll_all_source_subscriptions_dispatches_due_active_subscriptions(monkeypatch) -> None:
    due_subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@due",
        status="active",
        poll_interval_seconds=3600,
        last_checked_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    not_due_subscription = SourceSubscription(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@not-due",
        status="active",
        poll_interval_seconds=3600,
        last_checked_at=datetime.now(timezone.utc),
    )
    redis = _FakeRedis()

    monkeypatch.setattr(
        "app.workers.source_subscription_tasks.async_session",
        lambda: _FakeSessionManager(_FakeSession([due_subscription, not_due_subscription])),
    )

    await poll_all_source_subscriptions({"redis": redis})

    assert redis.enqueued == [
        (
            "poll_source_subscription_task",
            {
                "subscription_id": str(due_subscription.id),
                "tenant_id": "tenant-a",
            },
        )
    ]


@pytest.mark.asyncio
async def test_diagnose_stale_queued_source_subscription_entries_task_logs_diagnostics(monkeypatch) -> None:
    calls = []

    async def fake_diagnose(db, *, limit):
        calls.append((db, limit))
        return 1

    session = _FakeSession([])
    monkeypatch.setattr(
        "app.workers.source_subscription_tasks.async_session",
        lambda: _FakeSessionManager(session),
    )
    monkeypatch.setattr(
        "app.workers.source_subscription_tasks.diagnose_stale_queued_source_subscription_entries",
        fake_diagnose,
    )

    await diagnose_stale_queued_source_subscription_entries_task({}, limit=7)

    assert calls == [(session, 7)]
