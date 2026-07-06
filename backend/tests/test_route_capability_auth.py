import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.api.chat import _get_service as chat_get_service
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.curation_artifacts import router as curation_artifacts_router
from app.api.jobs import router as jobs_router
from app.api.palace import router as palace_router
from app.api.system import router as system_router
from app.api.web_saves import router as web_saves_router
from app.database import get_db
from app.schemas.chat import ChatResponse


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
        self.commits = 0
        self.updates: list[dict] = []

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


class EmptyConversationSession:
    async def execute(self, statement, params=None):
        assert params == {"tenant_id": "tenant-a"}
        return self

    def mappings(self):
        return self

    def all(self):
        return []


class PalaceClientSession:
    async def execute(self, statement, params=None):
        assert params == {"tenant_id": "tenant-a"}
        return self

    def mappings(self):
        return self

    def all(self):
        return []


class FakeChatService:
    async def chat(self, messages, *, model=None, conversation_id=None):
        assert messages[0].content == "Status?"
        return ChatResponse(response="ok", sources=[], conversation_id=conversation_id)


def _oauth_token_row(
    *,
    scopes: list[str],
    resource: str | None = "https://testserver/mcp",
    expires_at: datetime | None = None,
    revoked: bool = False,
):
    return {
        "token_id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "token_scopes": scopes,
        "token_resource": resource,
        "expires_at": expires_at or datetime.now(timezone.utc) + timedelta(minutes=5),
        "token_revoked_at": datetime.now(timezone.utc) if revoked else None,
        "client_id": uuid.uuid4(),
        "client_key": "codex-remote",
        "display_name": "Codex remote",
        "allowed_scopes": ["read", "write", "admin"],
        "client_revoked_at": None,
    }


def _client(monkeypatch, auth_session: AuthSession, db_session=object()) -> TestClient:
    app = FastAPI()
    for router in (
        conversations_router,
        curation_artifacts_router,
        jobs_router,
        palace_router,
        system_router,
        web_saves_router,
        chat_router,
    ):
        app.include_router(router, prefix="/api/v1")
    app.state.arq_pool = object()
    app.state.embedder = object()

    async def override_get_db():
        yield db_session

    monkeypatch.setattr(auth, "async_session", lambda: auth_session)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[chat_get_service] = lambda: FakeChatService()
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path", "row", "expected_detail"),
    [
        ("GET", "/api/v1/conversations", _oauth_token_row(scopes=["write"]), "MCP bearer token missing read scope"),
        ("POST", "/api/v1/chat", _oauth_token_row(scopes=["read"]), "MCP bearer token missing write scope"),
        (
            "DELETE",
            f"/api/v1/jobs/{uuid.uuid4()}",
            _oauth_token_row(scopes=["read"]),
            "MCP bearer token missing write scope",
        ),
        (
            "POST",
            "/api/v1/palace/mcp-clients/register",
            _oauth_token_row(scopes=["write"]),
            "MCP bearer token missing admin scope",
        ),
        (
            "GET",
            "/api/v1/stats",
            _oauth_token_row(scopes=["read"], expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)),
            "MCP bearer token expired",
        ),
        (
            "GET",
            "/api/v1/curation-artifacts",
            _oauth_token_row(scopes=["read"], revoked=True),
            "MCP bearer token revoked",
        ),
        (
            "GET",
            "/api/v1/palace",
            _oauth_token_row(scopes=["read"], resource="https://wrong.example/mcp"),
            "MCP bearer token resource is invalid",
        ),
        (
            "PATCH",
            f"/api/v1/web-saves/{uuid.uuid4()}",
            _oauth_token_row(scopes=["read"]),
            "MCP bearer token missing write scope",
        ),
    ],
)
def test_route_capability_auth_rejects_invalid_oauth_token_before_handlers(
    monkeypatch,
    method: str,
    path: str,
    row: dict,
    expected_detail: str,
) -> None:
    auth_session = AuthSession(row)
    client = _client(monkeypatch, auth_session)

    response = client.request(
        method,
        path,
        json={"messages": [{"role": "user", "content": "Status?"}], "client_key": "codex-remote", "display_name": "Codex remote", "allowed_scopes": ["read"]},
        headers={"Authorization": "Bearer raw-token"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == expected_detail
    assert (
        'resource_metadata="https://testserver/.well-known/oauth-protected-resource/api/v1"'
        in response.headers["WWW-Authenticate"]
    )


def test_conversations_accepts_oauth_read_token(monkeypatch) -> None:
    client = _client(monkeypatch, AuthSession(_oauth_token_row(scopes=["read"])), EmptyConversationSession())

    response = client.get("/api/v1/conversations", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 200
    assert response.json() == []


def test_chat_accepts_oauth_write_token(monkeypatch) -> None:
    client = _client(monkeypatch, AuthSession(_oauth_token_row(scopes=["write"])))

    response = client.post(
        "/api/v1/chat",
        json={"messages": [{"role": "user", "content": "Status?"}]},
        headers={"Authorization": "Bearer raw-token"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "ok"


def test_palace_admin_endpoint_accepts_oauth_admin_token(monkeypatch) -> None:
    client = _client(monkeypatch, AuthSession(_oauth_token_row(scopes=["admin"])), PalaceClientSession())

    response = client.get("/api/v1/palace/mcp-clients", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"
    assert response.json()["clients"] == []
