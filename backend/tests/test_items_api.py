import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.items import _encode_items_cursor, router
from app.auth import verify_api_key
from app.database import get_db
from app.models.item import Item


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeSession:
    def __init__(self, item) -> None:
        self.item = item
        self.execute_calls: list[tuple[str, dict]] = []

    async def get(self, model, key):
        if model is Item and key == self.item.id:
            return self.item
        return None

    async def execute(self, statement, params):
        self.execute_calls.append((str(statement), params))
        return _FakeResult(
            [
                SimpleNamespace(
                    item_id=uuid.uuid4(),
                    title="Tenant-safe related item",
                    source_type="note",
                    relationship="related_to",
                    confidence=0.8,
                )
            ]
        )


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one(self):
        return self._value


class _ItemsResult:
    def __init__(self, items) -> None:
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class ListItemsSession:
    def __init__(self, items) -> None:
        self.items = items
        self.execute_sql: list[str] = []

    async def execute(self, statement):
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.execute_sql.append(sql)
        if "count(*)" in sql.lower():
            return _ScalarResult(len(self.items))
        return _ItemsResult(self.items)


class UpdateItemSession:
    def __init__(self, item) -> None:
        self.item = item
        self.execute_calls: list[str] = []

    async def get(self, model, key):
        if model is Item and key == self.item.id:
            return self.item
        return None

    async def execute(self, statement, params=None):
        self.execute_calls.append(str(statement))
        return _FakeResult([])

    async def commit(self) -> None:
        return None

    async def refresh(self, value) -> None:
        assert value is self.item


class DeleteItemSession:
    def __init__(self, item) -> None:
        self.item = item
        self.commits = 0
        self.refreshes = 0

    async def get(self, model, key):
        if model is Item and key == self.item.id:
            return self.item
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        assert value is self.item
        self.refreshes += 1


class BatchDeleteSession:
    def __init__(self, items) -> None:
        self.items = items
        self.commits = 0

    async def execute(self, _statement):
        return _ItemsResult(self.items)

    async def commit(self) -> None:
        self.commits += 1


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


def _client(session: FakeSession, *, arq_pool: FakeArqPool | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = arq_pool or FakeArqPool()

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        request.state.key_hash = "key-hash"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
    return TestClient(app)


def test_related_items_query_scopes_joined_rows_to_authenticated_tenant() -> None:
    item_id = uuid.uuid4()
    session = FakeSession(
        Item(
            id=item_id,
            source_type="note",
            title="Origin",
            tenant_id="tenant-a",
            status="ready",
            created_at=datetime.now(timezone.utc),
        )
    )
    client = _client(session)

    response = client.get(f"/api/v1/items/{item_id}/related")

    assert response.status_code == 200
    sql, params = session.execute_calls[0]
    assert "i.tenant_id = :tenant_id" in sql
    assert params == {"item_id": str(item_id), "tenant_id": "tenant-a"}


def test_get_item_artifact_serves_image_analysis_upload(tmp_path: Path, monkeypatch) -> None:
    item_id = uuid.uuid4()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    artifact_path = upload_dir / "roadmap.png"
    artifact_path.write_bytes(b"image-bytes")
    monkeypatch.setattr("app.api.items.settings.upload_artifact_dir", str(upload_dir))
    session = FakeSession(
        Item(
            id=item_id,
            source_type="image",
            title="Roadmap",
            tenant_id="tenant-a",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={
                "image_analysis": {
                    "artifact": {
                        "filename": "roadmap.png",
                        "media_type": "image/png",
                        "storage_path": str(artifact_path),
                    }
                }
            },
        )
    )
    client = _client(session)

    response = client.get(f"/api/v1/items/{item_id}/artifact")

    assert response.status_code == 200
    assert response.content == b"image-bytes"
    assert response.headers["content-type"] == "image/png"


def test_get_item_artifact_rejects_paths_outside_upload_root(tmp_path: Path, monkeypatch) -> None:
    item_id = uuid.uuid4()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    outside_path = tmp_path / "outside.png"
    outside_path.write_bytes(b"outside")
    monkeypatch.setattr("app.api.items.settings.upload_artifact_dir", str(upload_dir))
    session = FakeSession(
        Item(
            id=item_id,
            source_type="image",
            title="Roadmap",
            tenant_id="tenant-a",
            status="ready",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            metadata_={
                "image_analysis": {
                    "artifact": {
                        "filename": "outside.png",
                        "media_type": "image/png",
                        "storage_path": str(outside_path),
                    }
                }
            },
        )
    )
    client = _client(session)

    response = client.get(f"/api/v1/items/{item_id}/artifact")

    assert response.status_code == 404


def test_list_items_excludes_failed_items_from_library_results() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="Ready",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    session = ListItemsSession([ready_item])
    client = _client(session)

    response = client.get("/api/v1/items")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [row["title"] for row in payload["items"]] == ["Ready"]
    assert any("items.status != 'failed'" in sql for sql in session.execute_sql)
    assert any("items.status != 'deleted'" in sql for sql in session.execute_sql)
    assert any("items.deleted_at IS NULL" in sql for sql in session.execute_sql)
    assert any("ORDER BY items.created_at DESC, items.id DESC" in sql for sql in session.execute_sql)


def test_list_items_applies_case_insensitive_title_sort() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="alpha",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    session = ListItemsSession([ready_item])
    client = _client(session)

    response = client.get("/api/v1/items?sort=title&order=asc")

    assert response.status_code == 200
    assert any("ORDER BY lower(items.title) ASC, items.id ASC" in sql for sql in session.execute_sql)


def test_list_items_filters_by_exact_source_url_and_returns_cursor() -> None:
    first = Item(
        id=uuid.uuid4(),
        source_type="note",
        source_url="https://example.test/audit",
        title="First",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime(2026, 4, 14, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    second = Item(
        id=uuid.uuid4(),
        source_type="note",
        source_url="https://example.test/audit",
        title="Second",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    session = ListItemsSession([first, second])
    client = _client(session)

    response = client.get("/api/v1/items?source_url=https%3A%2F%2Fexample.test%2Faudit&per_page=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [row["title"] for row in payload["items"]] == ["First"]
    assert payload["next_cursor"] == _encode_items_cursor(first)
    assert any("items.source_url = 'https://example.test/audit'" in sql for sql in session.execute_sql)


def test_list_items_cursor_requires_created_at_page_one() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="Ready",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    cursor = _encode_items_cursor(ready_item)
    session = ListItemsSession([ready_item])
    client = _client(session)

    response = client.get(f"/api/v1/items?cursor={cursor}&sort=title")

    assert response.status_code == 422
    assert response.json()["detail"] == "cursor pagination requires sort=created_at and page=1"
    assert session.execute_sql == []


def test_list_items_rejects_malformed_cursor() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="Ready",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    session = ListItemsSession([ready_item])
    client = _client(session)

    response = client.get("/api/v1/items?cursor=%25%25%25%25")

    assert response.status_code == 422
    assert response.json()["detail"] == "cursor must be a valid item listing cursor"


def test_list_items_rejects_unknown_sort_field() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="Ready",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    session = ListItemsSession([ready_item])
    client = _client(session)

    response = client.get("/api/v1/items?sort=relevance&order=desc")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported sort field: relevance"
    assert session.execute_sql == []


def test_list_items_rejects_unknown_sort_order() -> None:
    ready_item = Item(
        id=uuid.uuid4(),
        source_type="note",
        title="Ready",
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        metadata_={},
        tags=[],
        categories=[],
    )
    session = ListItemsSession([ready_item])
    client = _client(session)

    response = client.get("/api/v1/items?sort=created_at&order=sideways")

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported sort order: sideways"
    assert session.execute_sql == []


def test_patch_item_raw_content_requeues_embedding_and_clears_stale_summary() -> None:
    item_id = uuid.uuid4()
    session = UpdateItemSession(
        Item(
            id=item_id,
            source_type="note",
            title="Draft memory",
            tenant_id="tenant-a",
            status="ready",
            raw_content="old body",
            summary="Old summary",
            content_chunks=[{"index": 0, "text": "old body"}],
            content_hash="abc123",
            metadata_={},
            tags=["memory"],
            categories=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    )
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.patch(
        f"/api/v1/items/{item_id}",
        json={"raw_content": "new body for reliable recall"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "processing"
    assert payload["summary"] is None
    assert session.item.raw_content == "new body for reliable recall"
    assert session.item.content_chunks is None
    assert session.item.content_hash is None
    assert any("DELETE FROM embeddings" in sql for sql in session.execute_calls)
    assert arq_pool.enqueued == [
        (
            "embed_item",
            {
                "item_id": str(item_id),
                "skip_ai_enrichment": False,
                "tenant_id": "tenant-a",
            },
        )
    ]


def test_delete_item_soft_deletes_and_marks_palace_dirty() -> None:
    item_id = uuid.uuid4()
    item = Item(
        id=item_id,
        source_type="note",
        title="Recoverable memory",
        tenant_id="tenant-a",
        status="ready",
        metadata_={},
        tags=[],
        categories=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session = DeleteItemSession(item)
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.delete(f"/api/v1/items/{item_id}")

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert item.status == "deleted"
    assert item.deleted_at is not None
    assert item.metadata_["previous_status"] == "ready"
    assert item.metadata_["deleted_via"] == "items.delete"
    assert session.commits == 1
    assert arq_pool.enqueued == [
        (
            "mark_items_dirty_and_schedule",
            {
                "_queue_name": "arq:queue:palace",
                "item_ids": [str(item_id)],
                "tenant_id": "tenant-a",
                "reason": "item-soft-delete",
            },
        )
    ]


def test_restore_item_reactivates_soft_deleted_item() -> None:
    item_id = uuid.uuid4()
    deleted_at = datetime.now(timezone.utc)
    item = Item(
        id=item_id,
        source_type="note",
        title="Recoverable memory",
        tenant_id="tenant-a",
        status="deleted",
        deleted_at=deleted_at,
        metadata_={
            "deleted_at": deleted_at.isoformat(),
            "deleted_by": "key-hash",
            "deleted_via": "items.delete",
            "previous_status": "ready",
        },
        tags=[],
        categories=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session = DeleteItemSession(item)
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(f"/api/v1/items/{item_id}/restore")

    assert response.status_code == 200
    assert response.json()["restored"] is True
    assert item.status == "ready"
    assert item.deleted_at is None
    assert "deleted_at" not in item.metadata_
    assert session.commits == 1
    assert session.refreshes == 1
    assert arq_pool.enqueued == [
        (
            "mark_items_dirty_and_schedule",
            {
                "_queue_name": "arq:queue:palace",
                "item_ids": [str(item_id)],
                "tenant_id": "tenant-a",
                "reason": "item-restore",
            },
        )
    ]


def test_batch_delete_soft_deletes_without_raw_item_delete() -> None:
    items = [
        Item(
            id=uuid.uuid4(),
            source_type="note",
            title="Batch memory",
            tenant_id="tenant-a",
            status="ready",
            metadata_={},
            tags=[],
            categories=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    ]
    session = BatchDeleteSession(items)
    arq_pool = FakeArqPool()
    client = _client(session, arq_pool=arq_pool)

    response = client.post(
        "/api/v1/items/batch",
        json={"action": "delete", "ids": [str(items[0].id)]},
    )

    assert response.status_code == 200
    assert response.json() == {"affected": 1, "action": "delete"}
    assert items[0].status == "deleted"
    assert items[0].deleted_at is not None
    assert session.commits == 1
