import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.models.source_resource import SourceResource
from app.services.source_resources import canonical_http_identity
from app.services.source_resources import RefreshLease, refresh_lease_job_id
from app.workers import source_resource_tasks


class _FakeRedis:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


class _FakeResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _ClaimSession:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.statement = None
        self.added = []
        self.flushed = False

    async def execute(self, statement):
        self.statement = statement
        return _FakeResult(self.rows)

    def add(self, resource) -> None:
        self.added.append(resource)

    async def flush(self) -> None:
        self.flushed = True


def _resource(*, status: str = "active", due_at=None, backoff_until=None) -> SourceResource:
    return SourceResource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        kind="http",
        canonical_url="https://example.com/source",
        canonical_identity=canonical_http_identity("https://example.com/source"),
        refresh_policy="interval",
        refresh_slo_seconds=3600,
        status=status,
        next_due_at=due_at,
        backoff_until=backoff_until,
        consecutive_failures=0,
    )


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
async def test_claim_due_resources_uses_skip_locked_predicates_and_defensive_lease_checks() -> None:
    now = datetime.now(timezone.utc)
    due = _resource(due_at=now)
    paused = _resource(status="paused", due_at=now)
    backed_off = _resource(due_at=now, backoff_until=now.replace(year=now.year + 1))
    session = _ClaimSession([due, paused, backed_off])

    leases = await source_resource_tasks.claim_due_source_resources(
        session,
        now=now,
        limit=2,
        lease_seconds=300,
    )

    assert [lease.resource_id for lease in leases] == [due.id]
    assert session.added == [due]
    assert session.flushed is True
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "LIMIT 2" in sql
    assert "source_resources.refresh_policy != 'manual'" in sql
    assert "source_resources.refresh_lease_expires_at" in sql


@pytest.mark.asyncio
async def test_no_network_refresh_entrypoint_rejects_malformed_identifiers() -> None:
    with pytest.raises(ValueError):
        await source_resource_tasks.refresh_source_resource({}, "not-a-uuid", "tenant-a", "not-a-uuid")
