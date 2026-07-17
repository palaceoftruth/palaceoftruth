import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_refresh_rechecks_lease_with_a_fresh_post_fetch_timestamp(monkeypatch) -> None:
    """A result observed after the lease expires must be discarded, not persisted."""

    lease_token = uuid.uuid4()
    before_fetch = datetime(2026, 7, 17, tzinfo=timezone.utc)
    after_fetch = before_fetch + timedelta(seconds=1)
    resource = _resource(due_at=before_fetch)
    resource.refresh_lease_token = lease_token
    resource.refresh_lease_expires_at = after_fetch + timedelta(seconds=30)
    resource.robots_allowed = True
    resource.robots_decision = "robots_cached"
    resource.robots_cached_at = before_fetch

    class _Clock:
        values = iter((before_fetch, after_fetch))

        @classmethod
        def now(cls, _tz):
            return next(cls.values)

    class _ScalarSession:
        def __init__(self, value):
            self.value = value
            self.lease_check_times: list[datetime] = []

        async def scalar(self, statement):
            params = statement.compile().params
            self.lease_check_times.extend(
                value
                for key, value in params.items()
                if key.startswith("refresh_lease_expires_at")
            )
            return self.value

    class _Manager:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    initial = _ScalarSession(resource)
    expired = _ScalarSession(None)
    managers = iter((_Manager(initial), _Manager(expired)))

    async def _fetch(*_args, **_kwargs):
        return SimpleNamespace(outcome="success", body=b"content", status_code=200, etag=None, last_modified=None, final_url=None)

    monkeypatch.setattr(source_resource_tasks, "datetime", _Clock)
    monkeypatch.setattr(source_resource_tasks, "async_session", lambda: next(managers))
    monkeypatch.setattr(source_resource_tasks, "fetch_http_resource", _fetch)

    await source_resource_tasks.refresh_source_resource({}, str(resource.id), resource.tenant_id, str(lease_token))

    assert initial.lease_check_times == [before_fetch]
    assert expired.lease_check_times == [after_fetch]
