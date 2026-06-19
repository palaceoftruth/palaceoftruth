import io
import json
import uuid
import zipfile
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.embedding_profile import DEFAULT_EMBEDDING_MODEL
from app.api.admin import router
from app.database import get_db


class FakeSession:
    def __init__(
        self,
        *,
        items: list[dict] | None = None,
        conversations: list[dict] | None = None,
        conversation_messages: list[dict] | None = None,
        api_keys: list[dict] | None = None,
    ) -> None:
        self.scalar_calls = 0
        self.added = []
        self.items = list(items or [])
        self.conversations = list(conversations or [])
        self.conversation_messages = list(conversation_messages or [])
        self.api_keys = list(api_keys or [])

    async def scalar(self, statement, params=None):
        self.scalar_calls += 1
        sql = str(statement).lower()
        params = params or {}
        tenant_id = params.get("tenant_id")
        if tenant_id is None:
            return None
        if "from items" in sql:
            return any(row["tenant_id"] == tenant_id for row in self.items)
        if "from conversations" in sql and "conversation_messages" not in sql:
            return any(row["tenant_id"] == tenant_id for row in self.conversations)
        if "from conversation_messages" in sql:
            return any(row["tenant_id"] == tenant_id for row in self.conversation_messages)
        if "from api_keys" in sql:
            return any(row["tenant_id"] == tenant_id for row in self.api_keys)
        return None

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        return None

    async def refresh(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        return None

    async def get(self, *args, **kwargs):
        return None


class FakeArqPool:
    def __init__(self) -> None:
        self.enqueued = []

    async def enqueue_job(self, name: str, **kwargs) -> None:
        self.enqueued.append((name, kwargs))


def _bundle_bytes(*, role: str = "user") -> bytes:
    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "bundle_version": 1,
        "exported_at": now,
        "source_instance": {"app": "palaceoftruth", "tenant_id": "tenant-a"},
        "embedding": {"source_model": DEFAULT_EMBEDDING_MODEL, "rebuild_required": True},
        "items_file": "items.json",
        "conversations_file": "conversations.json",
        "artifacts_dir": None,
    }
    items = [
        {
            "id": str(uuid.uuid4()),
            "source_type": "note",
            "title": "One",
            "summary": None,
            "raw_content": "Hello world",
            "content_chunks": [{"index": 0, "text": "Hello world"}],
            "metadata": {},
            "tags": [],
            "categories": [],
            "content_hash": "abc123",
            "created_at": now,
            "updated_at": now,
        }
    ]
    conversations = [
        {
            "id": str(uuid.uuid4()),
            "title": "Conversation",
            "created_at": now,
            "updated_at": now,
            "messages": [
                {
                    "id": str(uuid.uuid4()),
                    "role": role,
                    "content": "Hi",
                    "created_at": now,
                }
            ],
        }
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("items.json", json.dumps(items))
        zf.writestr("conversations.json", json.dumps(conversations))
    return buf.getvalue()


def _client(session: FakeSession, pool: FakeArqPool) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = pool

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_admin_import_requires_admin_secret() -> None:
    client = _client(FakeSession(), FakeArqPool())

    response = client.post(
        "/api/v1/admin/bundles/import",
        data={"tenant_id": "fresh-tenant"},
        files={"bundle": ("bundle.zip", _bundle_bytes(), "application/zip")},
    )

    assert response.status_code == 403


def test_admin_import_accepts_valid_bundle_and_enqueues_restore_job() -> None:
    session = FakeSession()
    pool = FakeArqPool()
    client = _client(session, pool)

    response = client.post(
        "/api/v1/admin/bundles/import",
        headers={"X-Admin-Secret": "test-admin-secret"},
        data={"tenant_id": "fresh-tenant"},
        files={"bundle": ("bundle.zip", _bundle_bytes(), "application/zip")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["tenant_id"] == "fresh-tenant"
    assert body["status"] == "validated"
    assert pool.enqueued[0][0] == "restore_bundle"


def test_admin_import_allows_control_plane_api_keys_without_tenant_content() -> None:
    session = FakeSession(
        api_keys=[
            {
                "tenant_id": "fresh-tenant",
            }
        ]
    )
    pool = FakeArqPool()
    client = _client(session, pool)

    response = client.post(
        "/api/v1/admin/bundles/import",
        headers={"X-Admin-Secret": "test-admin-secret"},
        data={"tenant_id": "fresh-tenant"},
        files={"bundle": ("bundle.zip", _bundle_bytes(), "application/zip")},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["tenant_id"] == "fresh-tenant"
    assert body["status"] == "validated"
    assert pool.enqueued[0][0] == "restore_bundle"


def test_admin_import_rejects_tenant_content_even_without_api_keys() -> None:
    session = FakeSession(
        items=[
            {
                "tenant_id": "fresh-tenant",
            }
        ]
    )
    pool = FakeArqPool()
    client = _client(session, pool)

    response = client.post(
        "/api/v1/admin/bundles/import",
        headers={"X-Admin-Secret": "test-admin-secret"},
        data={"tenant_id": "fresh-tenant"},
        files={"bundle": ("bundle.zip", _bundle_bytes(), "application/zip")},
    )

    assert response.status_code == 409
    assert pool.enqueued == []


def test_admin_import_rejects_invalid_bundle() -> None:
    client = _client(FakeSession(), FakeArqPool())

    response = client.post(
        "/api/v1/admin/bundles/import",
        headers={"X-Admin-Secret": "test-admin-secret"},
        data={"tenant_id": "fresh-tenant"},
        files={"bundle": ("bundle.zip", b"not-a-zip", "application/zip")},
    )

    assert response.status_code == 422


def test_admin_import_rejects_structurally_invalid_bundle() -> None:
    client = _client(FakeSession(), FakeArqPool())

    response = client.post(
        "/api/v1/admin/bundles/import",
        headers={"X-Admin-Secret": "test-admin-secret"},
        data={"tenant_id": "fresh-tenant"},
        files={"bundle": ("bundle.zip", _bundle_bytes(role="system"), "application/zip")},
    )

    assert response.status_code == 422
