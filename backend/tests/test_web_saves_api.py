import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.web_saves import router as web_saves_router
from app.auth import verify_api_key, verify_capture_job_read_auth
from app.database import get_db
from app.models.item import Item
from app.models.web_save import WebSave


class _Result:
    def __init__(self, rows: list[object], *, scalar: int | None = None) -> None:
        self._rows = rows
        self._scalar = scalar

    def all(self) -> list[object]:
        return self._rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> int:
        if self._scalar is None:
            raise AssertionError("No scalar value configured")
        return self._scalar


class FakeSession:
    def __init__(self, rows: list[tuple[WebSave, Item]]) -> None:
        self.rows = rows
        self.commits = 0
        self.refreshed: list[object] = []

    async def execute(self, statement):
        statement_text = str(statement)
        if "count" in statement_text.lower():
            return _Result([], scalar=len(self.rows))
        return _Result(self.rows)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value) -> None:
        self.refreshed.append(value)


def _client(session: FakeSession) -> TestClient:
    app = FastAPI()
    app.include_router(web_saves_router, prefix="/api/v1")

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.tenant_id = "tenant-a"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_api_key] = override_verify
    app.dependency_overrides[verify_capture_job_read_auth] = override_verify
    return TestClient(app)


def _read_token_client(session: FakeSession) -> TestClient:
    app = FastAPI()
    app.include_router(web_saves_router, prefix="/api/v1")

    async def override_get_db():
        yield session

    async def override_read_token(request: Request):
        request.state.tenant_id = "tenant-a"
        return "capture-token"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_capture_job_read_auth] = override_read_token
    return TestClient(app)


def _row() -> tuple[WebSave, Item]:
    item_id = uuid.uuid4()
    save_id = uuid.uuid4()
    now = datetime(2026, 5, 12, tzinfo=timezone.utc)
    item = Item(
        id=item_id,
        tenant_id="tenant-a",
        source_type="webpage",
        source_url="https://example.com/brief",
        title="Example Brief",
        summary="A saved page summary.",
        raw_content=None,
        content_chunks=None,
        metadata_={},
        tags=["research"],
        categories=[],
        status="ready",
        created_at=now,
        updated_at=now,
    )
    save = WebSave(
        id=save_id,
        tenant_id="tenant-a",
        item_id=item_id,
        original_url="https://example.com/brief",
        normalized_url="https://example.com/brief",
        source_title="Example Brief",
        source_domain="example.com",
        capture_kind="webpage",
        user_tags=["research", "policy"],
        saved_at=now,
        archived_at=None,
        extension_version="0.1.9",
        metadata_={"browser_capture": {"preview_media": None}},
    )
    return save, item


def test_list_web_saves_returns_active_collection_rows() -> None:
    session = FakeSession([_row()])
    client = _client(session)

    response = client.get("/api/v1/web-saves")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["web_saves"][0]["source_domain"] == "example.com"
    assert payload["web_saves"][0]["capture_kind"] == "webpage"
    assert payload["web_saves"][0]["user_tags"] == ["research", "policy"]
    assert payload["web_saves"][0]["item"]["title"] == "Example Brief"
    assert payload["web_saves"][0]["item"]["status"] == "ready"


def test_list_web_saves_accepts_extension_read_token_dependency() -> None:
    session = FakeSession([_row()])
    client = _read_token_client(session)

    response = client.get("/api/v1/web-saves")

    assert response.status_code == 200
    assert response.json()["web_saves"][0]["source_domain"] == "example.com"


def test_update_web_save_archives_without_deleting_item() -> None:
    row = _row()
    session = FakeSession([row])
    client = _client(session)

    response = client.patch(f"/api/v1/web-saves/{row[0].id}", json={"archived": True})

    assert response.status_code == 200
    assert session.commits == 1
    assert row[0].archived_at is not None
    assert response.json()["archived_at"] is not None
    assert row[1].status == "ready"
