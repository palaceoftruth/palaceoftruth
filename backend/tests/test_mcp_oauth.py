import uuid
import base64
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mcp_oauth
from app.auth import hash_secret


class _MappingRows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return _MappingRows(self._rows)


class FakeSession:
    def __init__(self, row=None) -> None:
        self.row = row
        self.tokens = []
        self.revoked = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        if "from mcp_clients" in sql:
            if self.row is None or self.row["client_key"] != params["client_key"]:
                return _Result([])
            return _Result([self.row])
        if "insert into mcp_oauth_access_tokens" in sql:
            self.tokens.append(params)
            return _Result([])
        if "update mcp_oauth_access_tokens" in sql:
            self.revoked.append(params)
            return _Result([])
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _client(session: FakeSession, monkeypatch) -> TestClient:
    app = FastAPI()
    app.include_router(mcp_oauth.router, prefix="/api/v1")
    app.include_router(mcp_oauth.metadata_router)
    monkeypatch.setattr(mcp_oauth, "async_session", lambda: session)
    return TestClient(app)


def _client_row(**overrides) -> dict:
    row = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "client_key": "codex-remote",
        "allowed_scopes": ["read", "write"],
        "oauth_client_secret_hash": hash_secret("client-secret"),
        "oauth_revoked_at": None,
        "oauth_token_ttl_seconds": 3600,
    }
    row.update(overrides)
    return row


def test_mcp_oauth_token_endpoint_mints_scoped_bearer_token(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "scope": "read",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "read"
    assert session.tokens[0]["tenant_id"] == "tenant-a"
    assert session.tokens[0]["scopes"] == '["read"]'
    assert session.tokens[0]["expires_at"] > datetime.now(timezone.utc)


def test_mcp_oauth_token_endpoint_rejects_invalid_secret(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "wrong",
        },
    )

    assert response.status_code == 401
    assert session.tokens == []


def test_mcp_oauth_token_endpoint_accepts_http_basic_client_auth(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)
    basic = base64.b64encode(b"codex-remote:client-secret").decode()

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "client_credentials"},
    )

    assert response.status_code == 200
    assert response.json()["scope"] == "read write"


def test_mcp_oauth_token_endpoint_fails_closed_on_malformed_scope_row(monkeypatch) -> None:
    session = FakeSession(_client_row(allowed_scopes={"read": True}))
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
        },
    )

    assert response.status_code == 403
    assert session.tokens == []


def test_mcp_oauth_revoke_is_idempotent(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.post("/api/v1/memory/mcp/oauth/revoke", data={"token": "raw-token"})

    assert response.status_code == 200
    assert response.json() == {"revoked": True}
    assert session.revoked == [{"token_hash": hash_secret("raw-token")}]


def test_mcp_oauth_protected_resource_metadata_lists_scopes(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    body = response.json()
    assert body["resource"].endswith("/mcp")
    assert body["bearer_methods_supported"] == ["header"]
    assert "read" in body["scopes_supported"]
