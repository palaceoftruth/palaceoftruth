import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.api import export
from app.auth import AuthContext, verify_memory_auth
from app.models.item import Item


class _ItemsResult:
    def __init__(self, items) -> None:
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class ExportSession:
    def __init__(self, items) -> None:
        self.items = items
        self.execute_sql: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement):
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.execute_sql.append(sql)
        return _ItemsResult(self.items)


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


def _item() -> Item:
    now = datetime.now(timezone.utc)
    return Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        title="Exported note",
        source_type="note",
        source_url="https://example.com/note",
        summary="A small summary",
        raw_content="Body",
        tags=["alpha"],
        categories=[],
        metadata_={},
        status="ready",
        created_at=now,
        updated_at=now,
    )


def _client(monkeypatch, session: ExportSession) -> TestClient:
    app = FastAPI()
    app.include_router(export.router, prefix="/api/v1")

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

    app.dependency_overrides[verify_memory_auth] = override_verify
    monkeypatch.setattr(export, "async_session", lambda: session)
    return TestClient(app)


def _oauth_client(monkeypatch, session: ExportSession, auth_session: AuthSession) -> TestClient:
    app = FastAPI()
    app.include_router(export.router, prefix="/api/v1")

    monkeypatch.setattr(auth, "async_session", lambda: auth_session)
    monkeypatch.setattr(export, "async_session", lambda: session)
    return TestClient(app)


def test_export_json_preserves_legacy_api_key_access(monkeypatch) -> None:
    session = ExportSession([_item()])
    client = _client(monkeypatch, session)

    response = client.get("/api/v1/export?format=json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert session.execute_sql


def test_export_accepts_oauth_bearer_read_scope(monkeypatch) -> None:
    session = ExportSession([_item()])
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["read"])))

    response = client.get("/api/v1/export?format=json", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 200
    assert session.execute_sql


def test_export_rejects_oauth_bearer_missing_read_scope(monkeypatch) -> None:
    session = ExportSession([_item()])
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["write"])))

    response = client.get("/api/v1/export?format=json", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token missing read scope"
    assert session.execute_sql == []
