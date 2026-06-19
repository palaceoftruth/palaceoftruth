import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.feeds import router
from app.auth import verify_api_key
from app.database import get_db


class _FakeMappingsResult:
    def __init__(self, *, row=None, rows=None) -> None:
        self._row = row
        self._rows = rows or []

    def mappings(self):
        return self

    def one_or_none(self):
        return self._row

    def all(self):
        return self._rows


class _FakeScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one(self):
        return self._value


class ImportOpmlSession:
    def __init__(self, feed_id: uuid.UUID) -> None:
        now = datetime.now(timezone.utc)
        self.feed_id = feed_id
        self.commits = 0
        self.feed_row = {
            "id": feed_id,
            "url": "https://example.com/feed.xml",
            "name": None,
            "auto_tags": [],
            "poll_interval": 300,
            "enabled": True,
            "paused_reason": None,
            "last_fetched_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "feed_metadata": {},
            "item_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        self.execute_calls = 0

    async def execute(self, _statement, _params):
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _FakeMappingsResult(row={"id": self.feed_id})
        return _FakeMappingsResult(rows=[self.feed_row])

    async def commit(self) -> None:
        self.commits += 1


class FeedListScopeSession:
    def __init__(self, feed_id: uuid.UUID) -> None:
        now = datetime.now(timezone.utc)
        self.calls: list[tuple[str, dict]] = []
        self.feed_row = {
            "id": feed_id,
            "url": "https://example.com/feed.xml",
            "name": "Feed",
            "auto_tags": [],
            "poll_interval": 300,
            "enabled": True,
            "paused_reason": None,
            "last_fetched_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "feed_metadata": {},
            "item_count": 1,
            "created_at": now,
            "updated_at": now,
        }

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _FakeMappingsResult(rows=[self.feed_row])


class FeedItemsScopeSession:
    def __init__(self, feed_id: uuid.UUID) -> None:
        now = datetime.now(timezone.utc)
        self.feed_id = feed_id
        self.calls: list[tuple[str, dict]] = []
        self.item_row = {
            "id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "title": "Tenant feed item",
            "source_type": "feed",
            "summary": "Only the caller's tenant should see this item.",
            "raw_content": "body",
            "content_chunks": None,
            "source_url": "https://example.com/post",
            "metadata": {"feed_id": str(feed_id)},
            "tags": [],
            "categories": [],
            "status": "ready",
            "created_at": now,
            "updated_at": now,
        }

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        if len(self.calls) == 1:
            return _FakeMappingsResult(row={"id": self.feed_id})
        if len(self.calls) == 2:
            return _FakeMappingsResult(rows=[self.item_row])
        return _FakeScalarResult(1)


class DeleteFeedSession:
    def __init__(self, *, affected: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.affected = affected

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return type("Result", (), {"rowcount": 1 if self.affected else 0})()

    async def commit(self) -> None:
        self.commits += 1


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


def _client(session) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
    return TestClient(app)


def test_import_opml_propagates_tenant_id_when_enqueueing_polls() -> None:
    feed_id = uuid.uuid4()
    client = _client(ImportOpmlSession(feed_id))

    response = client.post(
        "/api/v1/feeds/import_opml",
        files={
            "file": (
                "feeds.opml",
                b'<?xml version="1.0"?><opml><body><outline type="rss" xmlUrl="https://example.com/feed.xml" /></body></opml>',
                "text/xml",
            )
        },
    )

    assert response.status_code == 202
    assert client.app.state.arq_pool.enqueued == [
        ("poll_feed", {"feed_id": str(feed_id), "tenant_id": "tenant-a"})
    ]


def test_list_feeds_scopes_item_count_query_by_tenant() -> None:
    session = FeedListScopeSession(uuid.uuid4())
    client = _client(session)

    response = client.get("/api/v1/feeds")

    assert response.status_code == 200
    assert response.json()["total"] == 1

    sql, params = session.calls[0]
    normalized_sql = " ".join(sql.lower().split())
    assert "select count(*) from items" in normalized_sql
    assert normalized_sql.count("tenant_id = :tenant_id") >= 2
    assert "f.deleted_at is null" in normalized_sql
    assert "deleted_at is null" in normalized_sql
    assert params == {"tenant_id": "tenant-a"}


def test_list_feed_items_scopes_rows_and_total_by_tenant() -> None:
    feed_id = uuid.uuid4()
    session = FeedItemsScopeSession(feed_id)
    client = _client(session)

    response = client.get(f"/api/v1/feeds/{feed_id}/items?limit=5&offset=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["tenant_id"] == "tenant-a"
    assert payload["items"][0]["metadata"]["feed_id"] == str(feed_id)

    items_sql, items_params = session.calls[1]
    count_sql, count_params = session.calls[2]
    for sql, params in ((items_sql, items_params), (count_sql, count_params)):
        normalized_sql = " ".join(sql.lower().split())
        assert "metadata->>'feed_id' = :feed_id" in sql
        assert "tenant_id = :tenant_id" in normalized_sql
        assert "deleted_at is null" in normalized_sql
        assert params["feed_id"] == str(feed_id)
        assert params["tenant_id"] == "tenant-a"

    assert items_params["limit"] == 5
    assert items_params["offset"] == 2


def test_delete_feed_soft_deletes_and_disables_polling() -> None:
    feed_id = uuid.uuid4()
    session = DeleteFeedSession()
    client = _client(session)

    response = client.delete(f"/api/v1/feeds/{feed_id}")

    assert response.status_code == 204
    sql, params = session.calls[0]
    normalized_sql = " ".join(sql.lower().split())
    assert "update feeds set deleted_at = :deleted_at" in normalized_sql
    assert "enabled = false" in normalized_sql
    assert "paused_reason = 'soft_deleted'" in normalized_sql
    assert "deleted_at is null" in normalized_sql
    assert params["id"] == feed_id
    assert params["tenant_id"] == "tenant-a"
    assert params["deleted_at"] is not None
    assert session.commits == 1
