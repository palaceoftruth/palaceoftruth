import uuid
from datetime import datetime, timezone

import pytest

from app.services.source_resources import RefreshLease, refresh_lease_job_id
from app.workers import source_resource_tasks


class _FakeRedis:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


@pytest.mark.asyncio
async def test_dispatch_enqueues_nothing_until_explicitly_enabled(monkeypatch) -> None:
    redis = _FakeRedis()
    monkeypatch.setattr(source_resource_tasks.settings, "source_resource_refresh_dispatch_enabled", False)

    assert await source_resource_tasks.dispatch_due_source_resources({"redis": redis}) == 0
    assert redis.enqueued == []


@pytest.mark.asyncio
async def test_dispatch_enqueues_auditable_bounded_claims(monkeypatch) -> None:
    resource_id = uuid.uuid4()
    token = uuid.UUID("00000000-0000-0000-0000-000000000010")
    lease = RefreshLease(
        resource_id=resource_id,
        tenant_id="tenant-a",
        token=token,
        expires_at=datetime.now(timezone.utc),
    )
    redis = _FakeRedis()

    async def fake_claim(*_args, **_kwargs):
        return [lease]

    class _Session:
        async def commit(self) -> None:
            return None

    class _Manager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(source_resource_tasks.settings, "source_resource_refresh_dispatch_enabled", True)
    monkeypatch.setattr(source_resource_tasks, "claim_due_source_resources", fake_claim)
    monkeypatch.setattr(source_resource_tasks, "async_session", lambda: _Manager())

    assert await source_resource_tasks.dispatch_due_source_resources({"redis": redis}) == 1
    assert redis.enqueued == [
        (
            "refresh_source_resource",
            {
                "resource_id": str(resource_id),
                "tenant_id": "tenant-a",
                "lease_token": str(token),
                "_job_id": refresh_lease_job_id(lease),
            },
        )
    ]


@pytest.mark.asyncio
async def test_no_network_refresh_entrypoint_rejects_malformed_identifiers() -> None:
    with pytest.raises(ValueError):
        await source_resource_tasks.refresh_source_resource({}, "not-a-uuid", "tenant-a", "not-a-uuid")
