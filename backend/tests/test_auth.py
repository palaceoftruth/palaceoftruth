import uuid

import pytest
from fastapi import HTTPException, Request

from app import auth
from datetime import datetime, timedelta, timezone


class _MappingResult:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Result:
    def __init__(self, row) -> None:
        self._row = row

    def mappings(self):
        return _MappingResult(self._row)


class FakeSession:
    def __init__(self, row) -> None:
        self.row = row
        self.updates = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        if "select id, tenant_id from api_keys" in sql:
            return _Result(self.row)
        if "update api_keys set last_used_at" in sql:
            self.updates.append(params["id"])
            return _Result(None)
        if "from mcp_oauth_access_tokens" in sql:
            return _Result(self.row)
        if "update mcp_oauth_access_tokens" in sql or "update mcp_clients" in sql:
            self.updates.append(params)
            return _Result(None)
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/v1/memory/whoami", "headers": []})


@pytest.mark.asyncio
async def test_verify_api_key_sets_tenant_and_updates_last_used(monkeypatch) -> None:
    key_id = uuid.uuid4()
    session = FakeSession({"id": key_id, "tenant_id": "tenant-a"})
    monkeypatch.setattr(auth, "async_session", lambda: session)

    request = _request()
    result = await auth.verify_api_key(request, api_key="raw-key")

    assert result == "raw-key"
    assert request.state.auth_context == auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        subject_id=str(key_id),
        token_hash_reference=auth.hash_secret("raw-key"),
        audit_metadata={"api_key_id": str(key_id)},
    )
    assert request.state.auth_context.capabilities == frozenset()
    assert session.updates == [key_id]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_require_mcp_scope_requires_scope_header_for_api_key() -> None:
    request = _request()
    request.state.auth_mode = "api_key"

    dependency = auth.require_mcp_scope("write")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key", mcp_scope=None, mcp_scopes=None)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "API key missing write MCP scope header"


@pytest.mark.asyncio
async def test_require_mcp_scope_accepts_api_key_scope_header() -> None:
    request = _request()
    request.state.tenant_id = "tenant-a"
    request.state.key_hash = "key-hash"
    request.state.auth_mode = "api_key"

    dependency = auth.require_mcp_scope("write")
    await dependency(request, _="raw-key", mcp_scope="write", mcp_scopes="write:workspace,read")

    assert request.state.mcp_allowed_scopes == ["write", "write:workspace", "read"]
    assert request.state.auth_context.scopes == ("write", "write:workspace", "read")
    assert request.state.auth_context.has_capability("write")


@pytest.mark.asyncio
async def test_require_capability_accepts_admin_api_key_scope_header() -> None:
    request = _request()
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
    )
    request.state.tenant_id = "tenant-a"
    request.state.auth_mode = "api_key"
    request.state.key_hash = "key-hash"

    dependency = auth.require_capability("write:workspace")
    await dependency(request, _="raw-key", mcp_scope="admin", mcp_scopes=None)

    assert request.state.auth_context.scopes == ("admin",)
    assert request.state.auth_context.has_capability("write:workspace")


@pytest.mark.asyncio
async def test_require_mcp_scope_rejects_unknown_api_key_scope_header() -> None:
    request = _request()
    request.state.auth_mode = "api_key"

    dependency = auth.require_mcp_scope("read")
    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key", mcp_scope="read,root", mcp_scopes=None)

    assert exc_info.value.status_code == 403
    assert "Unsupported MCP scope header" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_api_key_rejects_invalid_without_usage_update(monkeypatch) -> None:
    session = FakeSession(None)
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(_request(), api_key="raw-key")

    assert exc_info.value.status_code == 403
    assert session.updates == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_verify_memory_auth_accepts_valid_mcp_bearer_token(monkeypatch) -> None:
    client_id = uuid.uuid4()
    token_id = uuid.uuid4()
    session = FakeSession(
        {
            "token_id": token_id,
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": client_id,
            "client_key": "codex-remote",
            "allowed_scopes": ["read", "write"],
            "client_revoked_at": None,
        }
    )
    request = _request()
    monkeypatch.setattr(auth, "async_session", lambda: session)

    result = await auth.verify_memory_auth(request, api_key=None, authorization="Bearer raw-token")

    assert result == "raw-token"
    assert request.state.tenant_id == "tenant-a"
    assert request.state.auth_mode == "mcp_oauth"
    assert request.state.mcp_client_key == "codex-remote"
    assert request.state.mcp_allowed_scopes == ["read"]
    assert request.state.mcp_token_resource is None
    assert request.state.auth_context.tenant_id == "tenant-a"
    assert request.state.auth_context.auth_mode == "mcp_oauth"
    assert request.state.auth_context.client_id == client_id
    assert request.state.auth_context.client_key == "codex-remote"
    assert request.state.auth_context.scopes == ("read",)
    assert request.state.auth_context.capabilities == frozenset({"read"})
    assert request.state.auth_context.token_hash_reference == auth.hash_secret("raw-token")
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_memory_auth_rejects_wrong_mcp_resource(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "token_resource": "https://api.test/api/v1",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "allowed_scopes": ["read"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(_request(), api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "resource" in exc_info.value.detail
    assert session.updates == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_verify_memory_auth_fails_closed_on_malformed_mcp_scopes(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "allowed_scopes": {"read": True},
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(_request(), api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "scopes" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_memory_auth_fails_closed_on_unsupported_mcp_scope(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read", "unknown:scope"],
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "allowed_scopes": ["read", "unknown:scope"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(_request(), api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "unsupported scope" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_memory_auth_rejects_expired_mcp_bearer_token(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "allowed_scopes": ["read"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(_request(), api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "expired" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_memory_auth_rejects_bearer_token_when_client_revoked(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "allowed_scopes": ["read"],
            "client_revoked_at": datetime.now(timezone.utc),
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(_request(), api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "revoked" in exc_info.value.detail


@pytest.mark.asyncio
async def test_verify_capture_write_auth_accepts_scoped_extension_token(monkeypatch) -> None:
    client_id = uuid.uuid4()
    token_id = uuid.uuid4()
    session = FakeSession(
        {
            "token_id": token_id,
            "tenant_id": "tenant-a",
            "token_scopes": ["capture:write", "capture:job:read"],
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": client_id,
            "client_key": "browser-extension:abc",
            "display_name": "Palace Capture Extension",
            "allowed_scopes": ["capture:write", "capture:job:read"],
            "client_revoked_at": None,
        }
    )
    request = _request()
    monkeypatch.setattr(auth, "async_session", lambda: session)

    result = await auth.verify_capture_write_auth(
        request,
        api_key=None,
        authorization="Bearer capture-token",
    )

    assert result == "capture-token"
    assert request.state.tenant_id == "tenant-a"
    assert request.state.auth_mode == "browser_extension"
    assert request.state.mcp_client_key == "browser-extension:abc"
    assert request.state.mcp_client_name == "Palace Capture Extension"
    assert request.state.mcp_allowed_scopes == ["capture:write", "capture:job:read"]
    assert request.state.auth_context.auth_mode == "browser_extension"
    assert request.state.auth_context.client_id == client_id
    assert request.state.auth_context.client_name == "Palace Capture Extension"
    assert request.state.auth_context.has_capability("capture:write")
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_capture_write_auth_rejects_job_read_only_extension_token(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["capture:job:read"],
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "browser-extension:abc",
            "display_name": "Palace Capture Extension",
            "allowed_scopes": ["capture:job:read"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_capture_write_auth(
            _request(),
            api_key=None,
            authorization="Bearer capture-token",
        )

    assert exc_info.value.status_code == 403
    assert "capture:write" in exc_info.value.detail
