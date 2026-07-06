import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.api.source_subscriptions import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.models.item import Item
from app.models.job import Job
from app.models.source_subscription import SourceSubscription, SourceSubscriptionEntry
from app.workers.queues import DEFAULT_WORKER_QUEUE, MEDIA_FAIR_DISPATCH_TASK_NAME, singleton_job_id


class FakeScalarResult:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, *, subscriptions=None, entries=None) -> None:
        self.subscriptions = subscriptions or {}
        self.entries = entries or []
        self.items = {}
        self.jobs = {}
        self.statements = []
        self.commits = 0

    def add(self, obj):
        if isinstance(obj, Item):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.items[obj.id] = obj
        if isinstance(obj, Job):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.jobs[obj.id] = obj

    async def flush(self):
        return None

    async def get(self, model, key):
        if model is SourceSubscription:
            return self.subscriptions.get(key)
        if model is SourceSubscriptionEntry:
            return next((entry for entry in self.entries if entry.id == key), None)
        if model is Item:
            return self.items.get(key)
        if model is Job:
            return self.jobs.get(key)
        return None

    async def execute(self, statement):
        self.statements.append(str(statement))
        statement_text = str(statement)
        if "source_subscription_entries" in statement_text:
            return FakeScalarResult(self.entries)
        if "FROM items" in statement_text:
            return FakeScalarResult([])
        return FakeScalarResult(list(self.subscriptions.values()))

    async def commit(self):
        self.commits += 1


class FakeArqPool:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.enqueued = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        if self.error is not None:
            raise self.error
        self.enqueued.append((name, kwargs))


class _AuthMappingResult:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _AuthResult:
    def __init__(self, row) -> None:
        self._row = row

    def mappings(self):
        return _AuthMappingResult(self._row)


class AuthSession:
    def __init__(self, row) -> None:
        self.row = row
        self.updates: list[dict] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        if "from mcp_oauth_access_tokens" in sql:
            return _AuthResult(self.row)
        if "insert into mcp_request_audit_events" in sql:
            return _AuthResult(None)
        if "update mcp_oauth_access_tokens" in sql or "update mcp_clients" in sql:
            self.updates.append(params)
            return _AuthResult(None)
        raise AssertionError(f"Unexpected auth SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _oauth_token_row(*, scopes: list[str], resource: str | None = "https://testserver/api/v1"):
    return {
        "token_id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "token_scopes": scopes,
        "token_resource": resource,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "token_revoked_at": None,
        "client_id": uuid.uuid4(),
        "client_key": "codex-remote",
        "allowed_scopes": ["read", "write", "admin"],
        "client_revoked_at": None,
    }


def _subscription(*, tenant_id: str, status: str = "active") -> SourceSubscription:
    now = datetime.now(timezone.utc)
    return SourceSubscription(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider_type="youtube_channel",
        source_url="https://www.youtube.com/@example",
        external_id="UC123",
        external_url="https://www.youtube.com/channel/UC123",
        display_name="Example Channel",
        status=status,
        auto_tags=[],
        poll_interval_seconds=3600,
        cursor={"no_backfill": True},
        provider_metadata={"youtube_channel_id": "UC123"},
        consecutive_failures=0,
        created_at=now,
        updated_at=now,
    )


def _client(session: FakeSession, *, arq_pool: FakeArqPool | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = arq_pool or FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(
            tenant_id="tenant-a",
            auth_mode="api_key",
            token_hash_reference="key-hash",
        )
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def _oauth_client(monkeypatch, session: FakeSession, auth_session: AuthSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()

    async def override_get_db():
        yield session

    monkeypatch.setattr(auth, "async_session", lambda: auth_session)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_list_source_subscriptions_scopes_query_by_tenant() -> None:
    sub = _subscription(tenant_id="tenant-a")
    session = FakeSession(subscriptions={sub.id: sub})
    client = _client(session)

    response = client.get("/api/v1/source-subscriptions")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    normalized_sql = " ".join(session.statements[0].lower().split())
    assert "source_subscriptions.tenant_id = :tenant_id_1" in normalized_sql
    assert "source_subscriptions.deleted_at is null" in normalized_sql


def test_list_source_subscriptions_accepts_oauth_bearer_read_scope(monkeypatch) -> None:
    sub = _subscription(tenant_id="tenant-a")
    session = FakeSession(subscriptions={sub.id: sub})
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["read"])))

    response = client.get("/api/v1/source-subscriptions", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    normalized_sql = " ".join(session.statements[0].lower().split())
    assert "source_subscriptions.tenant_id = :tenant_id_1" in normalized_sql


def test_manual_sync_rejects_oauth_bearer_missing_write_scope(monkeypatch) -> None:
    sub = _subscription(tenant_id="tenant-a")
    session = FakeSession(subscriptions={sub.id: sub})
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["read"])))

    response = client.post(
        f"/api/v1/source-subscriptions/{sub.id}/sync",
        headers={"Authorization": "Bearer raw-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token missing write scope"
    assert session.statements == []
    assert client.app.state.arq_pool.enqueued == []


def test_get_source_subscription_hides_other_tenant_subscription() -> None:
    sub = _subscription(tenant_id="tenant-b")
    client = _client(FakeSession(subscriptions={sub.id: sub}))

    response = client.get(f"/api/v1/source-subscriptions/{sub.id}")

    assert response.status_code == 404


def test_manual_sync_authorizes_tenant_before_enqueueing() -> None:
    sub = _subscription(tenant_id="tenant-b")
    client = _client(FakeSession(subscriptions={sub.id: sub}))

    response = client.post(f"/api/v1/source-subscriptions/{sub.id}/sync")

    assert response.status_code == 404
    assert client.app.state.arq_pool.enqueued == []


def test_manual_sync_enqueues_poll_for_active_subscription() -> None:
    sub = _subscription(tenant_id="tenant-a")
    session = FakeSession(subscriptions={sub.id: sub})
    client = _client(session)

    response = client.post(f"/api/v1/source-subscriptions/{sub.id}/sync")

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "subscription_id": str(sub.id)}
    assert client.app.state.arq_pool.enqueued == [
        ("poll_source_subscription_task", {"subscription_id": str(sub.id), "tenant_id": "tenant-a"})
    ]
    assert session.commits == 1
    assert "last_manual_sync_at" in sub.cursor


def test_manual_sync_rate_limits_recent_manual_sync() -> None:
    sub = _subscription(tenant_id="tenant-a")
    sub.cursor["last_manual_sync_at"] = datetime.now(timezone.utc).isoformat()
    session = FakeSession(subscriptions={sub.id: sub})
    client = _client(session)

    response = client.post(f"/api/v1/source-subscriptions/{sub.id}/sync")

    assert response.status_code == 429
    assert client.app.state.arq_pool.enqueued == []
    assert session.commits == 0


def test_manual_sync_reports_enqueue_failure_without_recording_sync() -> None:
    sub = _subscription(tenant_id="tenant-a")
    session = FakeSession(subscriptions={sub.id: sub})
    client = _client(session, arq_pool=FakeArqPool(error=RuntimeError("redis unavailable")))

    response = client.post(f"/api/v1/source-subscriptions/{sub.id}/sync")

    assert response.status_code == 503
    assert "last_manual_sync_at" not in sub.cursor
    assert session.commits == 0


def test_recent_entries_are_scoped_by_tenant_and_limited() -> None:
    sub = _subscription(tenant_id="tenant-a")
    now = datetime.now(timezone.utc)
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=sub.id,
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        title="New upload",
        discovered_at=now,
        status="captured",
        item_id=uuid.uuid4(),
        captured_at=now,
        metadata_={"youtube_video_id": "video-123"},
        created_at=now,
        updated_at=now,
    )
    session = FakeSession(subscriptions={sub.id: sub}, entries=[entry])
    client = _client(session)

    response = client.get(f"/api/v1/source-subscriptions/{sub.id}/entries?limit=10")

    assert response.status_code == 200
    assert response.json()["entries"][0]["status"] == "captured"
    normalized_sql = " ".join(session.statements[0].lower().split())
    assert "source_subscription_entries.tenant_id = :tenant_id_1" in normalized_sql
    assert "source_subscription_entries.subscription_id = :subscription_id_1" in normalized_sql


def test_retry_source_subscription_entry_queues_failed_entry() -> None:
    sub = _subscription(tenant_id="tenant-a")
    now = datetime.now(timezone.utc)
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=sub.id,
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        title="Failed upload",
        discovered_at=now,
        status="failed",
        error_message="transient failure",
        metadata_={"youtube_video_id": "video-123"},
        created_at=now,
        updated_at=now,
    )
    session = FakeSession(subscriptions={sub.id: sub}, entries=[entry])
    client = _client(session)

    response = client.post(f"/api/v1/source-subscriptions/entries/{entry.id}/retry")

    assert response.status_code == 202
    assert response.json() == {"status": "queued", "subscription_id": str(sub.id), "entry_id": str(entry.id)}
    assert entry.status == "queued"
    assert entry.error_message is None
    assert client.app.state.arq_pool.enqueued == [
        (
            MEDIA_FAIR_DISPATCH_TASK_NAME,
            {
                "_queue_name": DEFAULT_WORKER_QUEUE,
                "_job_id": singleton_job_id(MEDIA_FAIR_DISPATCH_TASK_NAME, "media"),
            },
        )
    ]


def test_retry_source_subscription_entry_rejects_non_failed_entry() -> None:
    sub = _subscription(tenant_id="tenant-a")
    entry = SourceSubscriptionEntry(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        subscription_id=sub.id,
        provider_entry_id="video-123",
        source_url="https://www.youtube.com/watch?v=video-123",
        status="captured",
    )
    client = _client(FakeSession(subscriptions={sub.id: sub}, entries=[entry]))

    response = client.post(f"/api/v1/source-subscriptions/entries/{entry.id}/retry")

    assert response.status_code == 409
