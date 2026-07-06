import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.api.tags import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db


class _RowsResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class TagsSession:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params):
        self.execute_calls.append((str(statement), params))
        return _RowsResult([SimpleNamespace(tag="alpha"), SimpleNamespace(tag="beta")])


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        if "from mcp_oauth_access_tokens" in sql:
            return _AuthResult(self.row)
        if "insert into mcp_request_audit_events" in sql:
            return _AuthResult(None)
        if "update mcp_oauth_access_tokens" in sql or "update mcp_clients" in sql:
            return _AuthResult(None)
        raise AssertionError(f"Unexpected auth SQL: {sql}")

    async def commit(self) -> None:
        return None


def _oauth_token_row(*, scopes: list[str]):
    return {
        "token_id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "token_scopes": scopes,
        "token_resource": "https://testserver/api/v1",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "token_revoked_at": None,
        "client_id": uuid.uuid4(),
        "client_key": "codex-remote",
        "allowed_scopes": ["read", "write", "admin"],
        "client_revoked_at": None,
    }


def _client(session: TagsSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

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


def _oauth_client(monkeypatch, session: TagsSession, auth_session: AuthSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield session

    monkeypatch.setattr(auth, "async_session", lambda: auth_session)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_list_tags_uses_authenticated_tenant_and_prefix_filter() -> None:
    session = TagsSession()
    client = _client(session)

    response = client.get("/api/v1/tags?q=a")

    assert response.status_code == 200
    assert response.json() == {"tags": ["alpha", "beta"], "total": 2}
    sql, params = session.execute_calls[0]
    assert "tenant_id = :tenant_id" in sql
    assert params == {"q": "a", "tenant_id": "tenant-a"}


def test_list_tags_accepts_oauth_bearer_read_scope(monkeypatch) -> None:
    session = TagsSession()
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["read"])))

    response = client.get("/api/v1/tags", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert session.execute_calls[0][1] == {"q": None, "tenant_id": "tenant-a"}


def test_list_tags_rejects_oauth_bearer_missing_read_scope(monkeypatch) -> None:
    session = TagsSession()
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["write"])))

    response = client.get("/api/v1/tags", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token missing read scope"
    assert session.execute_calls == []
