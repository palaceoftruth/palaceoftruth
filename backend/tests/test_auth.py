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
        self.audit_events = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        if "from api_keys" in sql and sql.startswith("select"):
            return _Result(self.row)
        if "update api_keys set last_used_at" in sql:
            self.updates.append(params["id"])
            return _Result(None)
        if "update api_keys set key_hash" in sql:
            self.updates.append(params)
            return _Result(None)
        if "from mcp_oauth_access_tokens" in sql:
            return _Result(self.row)
        if "insert into mcp_request_audit_events" in sql:
            self.audit_events.append(params)
            return _Result(None)
        if "update mcp_oauth_access_tokens" in sql or "update mcp_clients" in sql:
            self.updates.append(params)
            return _Result(None)
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _request(*, host: str = "testserver", path: str = "/api/v1/memory/whoami") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": (host, 443),
            "path": path,
            "headers": [],
        }
    )


@pytest.mark.asyncio
async def test_verify_api_key_sets_tenant_and_updates_last_used(monkeypatch) -> None:
    key_id = uuid.uuid4()
    session = FakeSession(
        {
            "id": key_id,
            "tenant_id": "tenant-a",
            "scopes": ["read", "write"],
            "key_hash": auth.hash_secret("raw-key"),
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    request = _request(path="/api/v1/items")
    result = await auth.verify_api_key(request, api_key="raw-key")

    assert result == "raw-key"
    context = request.state.auth_context
    assert context.tenant_id == "tenant-a"
    assert context.auth_mode == "api_key"
    assert context.subject_id == str(key_id)
    assert context.token_hash_reference == auth.hash_secret("raw-key")
    # The stored grant, not a header, is what the principal carries.
    assert context.scopes == ("read", "write")
    assert context.capabilities == frozenset({"read", "write"})
    assert context.audit_metadata == {
        "api_key_id": str(key_id),
        "api_key_granted_scopes": ["read", "write"],
    }
    assert session.updates == [key_id]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_api_key_rejects_row_without_stored_scopes(monkeypatch) -> None:
    """A NULL or malformed scopes column must deny, not fall back to open."""
    session = FakeSession(
        {
            "id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "scopes": None,
            "key_hash": auth.hash_secret("raw-key"),
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(_request(), api_key="raw-key")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_verify_api_key_upgrades_legacy_hash_when_pepper_set(monkeypatch) -> None:
    key_id = uuid.uuid4()
    legacy_hash = auth._legacy_hash_secret("raw-key")
    session = FakeSession(
        {
            "id": key_id,
            "tenant_id": "tenant-a",
            "scopes": ["read"],
            "key_hash": legacy_hash,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")

    request = _request(path="/api/v1/capture")
    await auth.verify_api_key(request, api_key="raw-key")

    peppered = auth.hash_secret("raw-key")
    assert peppered.startswith(auth.PEPPERED_HASH_PREFIX)
    assert peppered != legacy_hash
    # The legacy row authenticated, then was rewritten in the new format.
    # D-10: the upgrade write is guarded by the legacy value it expects to
    # replace, so two concurrent upgrades of the same row can't race past
    # each other onto a stale WHERE id = :id.
    assert session.updates == [
        key_id,
        {"hash": peppered, "id": key_id, "legacy_hash": legacy_hash},
    ]


def test_legacy_hash_rejected_is_false_without_a_configured_cutoff(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")
    monkeypatch.setattr(auth.settings, "credential_legacy_hash_cutoff_at", "")
    legacy_hash = auth._legacy_hash_secret("raw-key")

    # D-08: with no cutoff configured, a legacy row is still accepted and
    # upgraded on use rather than rejected outright.
    assert auth.legacy_hash_rejected(legacy_hash) is False


def test_legacy_hash_rejected_is_false_before_the_configured_cutoff(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")
    monkeypatch.setattr(
        auth.settings,
        "credential_legacy_hash_cutoff_at",
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    )
    legacy_hash = auth._legacy_hash_secret("raw-key")

    assert auth.legacy_hash_rejected(legacy_hash) is False


def test_legacy_hash_rejected_is_true_after_the_configured_cutoff(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")
    monkeypatch.setattr(
        auth.settings,
        "credential_legacy_hash_cutoff_at",
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )
    legacy_hash = auth._legacy_hash_secret("raw-key")

    assert auth.legacy_hash_rejected(legacy_hash) is True


def test_legacy_hash_rejected_ignores_an_already_peppered_hash(monkeypatch) -> None:
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")
    monkeypatch.setattr(
        auth.settings,
        "credential_legacy_hash_cutoff_at",
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )
    peppered_hash = auth.hash_secret("raw-key")

    # Not a legacy hash at all, so the cutoff never applies to it.
    assert auth.legacy_hash_rejected(peppered_hash) is False


@pytest.mark.asyncio
async def test_verify_api_key_rejects_legacy_hash_past_cutoff(monkeypatch) -> None:
    key_id = uuid.uuid4()
    legacy_hash = auth._legacy_hash_secret("raw-key")
    session = FakeSession(
        {
            "id": key_id,
            "tenant_id": "tenant-a",
            "scopes": ["read"],
            "key_hash": legacy_hash,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")
    monkeypatch.setattr(
        auth.settings,
        "credential_legacy_hash_cutoff_at",
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(_request(), api_key="raw-key")

    assert exc_info.value.status_code == 403
    # Rejected outright, not upgraded — the legacy format is retired past cutoff.
    assert session.updates == []


def test_generate_webhook_signing_key_is_independent_random_material() -> None:
    # H-09: must not be derivable from any stored credential verifier, and
    # must differ between calls (per-job, not reused across jobs).
    first = auth.generate_webhook_signing_key()
    second = auth.generate_webhook_signing_key()

    assert first != second
    assert len(first) == 64  # secrets.token_hex(32)
    bytes.fromhex(first)  # raises if it isn't hex
    assert first != auth.hash_secret("raw-key")
    assert first != auth._legacy_hash_secret("raw-key")


@pytest.mark.asyncio
async def test_require_mcp_scope_requires_scope_header_for_api_key() -> None:
    request = _request()
    request.state.auth_mode = "api_key"
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("read",),
        capabilities=frozenset({"read"}),
    )

    dependency = auth.require_mcp_scope("write")

    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key", mcp_scope=None, mcp_scopes=None)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "API key missing write MCP scope header"


@pytest.mark.asyncio
async def test_require_mcp_scope_narrows_stored_grant_with_header() -> None:
    request = _request()
    request.state.tenant_id = "tenant-a"
    request.state.key_hash = "key-hash"
    request.state.auth_mode = "api_key"
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("read", "write", "write:workspace", "write:agent"),
        capabilities=frozenset({"read", "write", "write:workspace", "write:agent"}),
    )

    dependency = auth.require_mcp_scope("write")
    await dependency(request, _="raw-key", mcp_scope="write", mcp_scopes="write:workspace,read")

    # write:agent is in the grant but not in the header, so the call drops it.
    assert request.state.mcp_allowed_scopes == ["read", "write", "write:workspace"]
    assert request.state.auth_context.scopes == ("read", "write", "write:workspace")
    assert request.state.auth_context.has_capability("write")
    metadata = request.state.auth_context.audit_metadata
    assert metadata["api_key_granted_scopes"] == ["read", "write", "write:agent", "write:workspace"]
    assert metadata["mcp_requested_scopes"] == ["read", "write", "write:workspace"]
    assert metadata["mcp_effective_scopes"] == ["read", "write", "write:workspace"]


@pytest.mark.asyncio
async def test_require_mcp_scope_header_cannot_widen_stored_grant() -> None:
    """C-01: a header asking for more than the row holds must not grant it."""
    request = _request()
    request.state.tenant_id = "tenant-a"
    request.state.key_hash = "key-hash"
    request.state.auth_mode = "api_key"
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("read",),
        capabilities=frozenset({"read"}),
    )

    dependency = auth.require_mcp_scope("write")
    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key", mcp_scope="write", mcp_scopes="admin")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "API key missing write MCP scope"


@pytest.mark.asyncio
async def test_require_capability_rejects_admin_header_without_stored_admin() -> None:
    """I-01: admin still satisfies every check, but only from a stored grant."""
    request = _request()
    request.state.tenant_id = "tenant-a"
    request.state.key_hash = "key-hash"
    request.state.auth_mode = "api_key"
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("read",),
        capabilities=frozenset({"read"}),
    )

    dependency = auth.require_capability("write:workspace")
    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key", mcp_scope="admin", mcp_scopes=None)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_capability_accepts_stored_admin_grant() -> None:
    request = _request()
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("admin",),
        capabilities=frozenset({"admin"}),
    )
    request.state.tenant_id = "tenant-a"
    request.state.auth_mode = "api_key"
    request.state.key_hash = "key-hash"

    dependency = auth.require_capability("write:workspace")
    await dependency(request, _="raw-key", mcp_scope="admin", mcp_scopes=None)

    assert request.state.auth_context.scopes == ("admin",)
    assert request.state.auth_context.has_capability("write:workspace")


@pytest.mark.asyncio
async def test_require_api_capability_accepts_stored_api_key_grant() -> None:
    request = _request()
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("read", "write"),
        capabilities=frozenset({"read", "write"}),
    )
    request.state.tenant_id = "tenant-a"
    request.state.auth_mode = "api_key"
    request.state.key_hash = "key-hash"

    dependency = auth.require_api_capability("write")
    await dependency(request, _="raw-key")


@pytest.mark.asyncio
async def test_require_api_capability_rejects_api_key_missing_stored_scope() -> None:
    """H-01: this path used to return before any check for api_key mode."""
    request = _request()
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="api_key",
        token_hash_reference="key-hash",
        scopes=("read",),
        capabilities=frozenset({"read"}),
    )
    request.state.tenant_id = "tenant-a"
    request.state.auth_mode = "api_key"
    request.state.key_hash = "key-hash"

    dependency = auth.require_api_capability("write")
    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "API key missing write scope"


@pytest.mark.asyncio
async def test_require_api_capability_rejects_unknown_auth_mode() -> None:
    """L-01: an unset or unrecognized auth mode used to fall through to success."""
    request = _request()
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="future_mode",
        token_hash_reference="key-hash",
        scopes=("write",),
        capabilities=frozenset({"write"}),
    )
    request.state.tenant_id = "tenant-a"
    request.state.auth_mode = "future_mode"

    dependency = auth.require_api_capability("write")
    with pytest.raises(HTTPException) as exc_info:
        await dependency(request, _="raw-key")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_api_capability_rejects_bearer_missing_scope() -> None:
    client_id = uuid.uuid4()
    session = FakeSession(None)
    request = _request()
    request.state.auth_context = auth.AuthContext(
        tenant_id="tenant-a",
        auth_mode="mcp_oauth",
        client_id=client_id,
        client_key="codex-remote",
        token_hash_reference=auth.hash_secret("raw-token"),
        scopes=("read",),
        capabilities=frozenset({"read"}),
        resource="https://testserver/api/v1",
    )
    request.state.tenant_id = "tenant-a"
    request.state.auth_mode = "mcp_oauth"
    request.state.mcp_client_id = client_id
    request.state.mcp_client_key = "codex-remote"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(auth, "async_session", lambda: session)

    try:
        dependency = auth.require_api_capability("write")
        with pytest.raises(HTTPException) as exc_info:
            await dependency(request, _="raw-token")
    finally:
        monkeypatch.undo()

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "MCP bearer token missing write scope"
    assert session.audit_events[0]["operation"] == "oauth.route_capability"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["required_scope"] == "write"
    assert session.audit_events[0]["error_class"] == "insufficient_scope"


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
            "token_resource": "https://testserver/api/v1",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": client_id,
            "client_key": "codex-remote",
            "display_name": "Codex Remote",
            "allowed_scopes": ["read", "write"],
            "agent_scope_key": "iris",
            "allow_all_agent_scope_reads": True,
            "allow_tenant_shared_reads": True,
            "client_revoked_at": None,
        }
    )
    request = _request(path="/api/v1/items")
    monkeypatch.setattr(auth, "async_session", lambda: session)

    result = await auth.verify_memory_auth(request, api_key=None, authorization="Bearer raw-token")

    assert result == "raw-token"
    assert request.state.tenant_id == "tenant-a"
    assert request.state.auth_mode == "mcp_oauth"
    assert request.state.mcp_client_key == "codex-remote"
    assert request.state.mcp_allowed_scopes == ["read"]
    assert request.state.mcp_token_resource == "https://testserver/api/v1"
    assert request.state.auth_context.tenant_id == "tenant-a"
    assert request.state.auth_context.auth_mode == "mcp_oauth"
    assert request.state.auth_context.client_id == client_id
    assert request.state.auth_context.client_key == "codex-remote"
    assert request.state.auth_context.client_name == "Codex Remote"
    assert request.state.mcp_client_name == "Codex Remote"
    assert request.state.auth_context.agent_scope_key == "iris"
    assert request.state.auth_context.allow_all_agent_scope_reads is True
    assert request.state.auth_context.allow_tenant_shared_reads is True
    assert request.state.mcp_allow_tenant_shared_reads is True
    assert request.state.auth_context.scopes == ("read",)
    assert request.state.auth_context.capabilities == frozenset({"read"})
    assert request.state.auth_context.token_hash_reference == auth.hash_secret("raw-token")
    assert session.audit_events[0]["operation"] == "oauth.token_use"
    assert session.audit_events[0]["status"] == "success"
    assert session.audit_events[0]["client_name"] == "Codex Remote"
    assert "raw-token" not in session.audit_events[0]["params_summary"]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_memory_auth_rejects_null_resource_for_rest_api(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "token_resource": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "display_name": "Codex Remote",
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
    assert session.audit_events[0]["operation"] == "oauth.token_use"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_resource"
    assert "raw-token" not in session.audit_events[0]["params_summary"]
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_memory_auth_rejects_null_resource_for_mcp_validation(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "token_resource": None,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "display_name": "Codex Remote",
            "allowed_scopes": ["read"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    request = _request()
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(request, api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "resource" in exc_info.value.detail
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_memory_auth_accepts_paired_api_host_mcp_resource(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "token_resource": "https://api.palace.sarvent.cloud/mcp",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "hermes-karen",
            "display_name": "Hermes Karen",
            "allowed_scopes": ["read"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    request = _request(host="mcp.palace.sarvent.cloud", path="/mcp")
    result = await auth.verify_memory_auth(
        request,
        api_key=None,
        authorization="Bearer raw-token",
    )

    assert result == "raw-token"
    assert request.state.auth_context.resource == "https://api.palace.sarvent.cloud/mcp"
    assert session.audit_events[0]["status"] == "success"


def test_paired_service_resources_never_crosses_parent_domain() -> None:
    request = _request(host="mcp.palace.sarvent.cloud", path="/mcp")

    assert auth.paired_service_resources(request, "/mcp") == {
        "https://api.palace.sarvent.cloud/mcp",
        "https://mcp.palace.sarvent.cloud/mcp",
    }
    assert "https://api.attacker.example/mcp" not in auth.paired_service_resources(request, "/mcp")


def test_whoami_uses_rest_api_resource_boundary() -> None:
    request = _request(host="api.palace.sarvent.cloud", path="/api/v1/memory/whoami")

    assert auth._expected_token_resources(request) == {
        "https://api.palace.sarvent.cloud/api/v1",
        "https://mcp.palace.sarvent.cloud/api/v1",
    }
    assert not auth._is_mcp_resource_validation_request(request)


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
            "display_name": "Codex Remote",
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
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_resource"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_verify_memory_auth_rejects_api_resource_when_mcp_expected(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "token_resource": "https://testserver/api/v1",
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
        await auth.verify_memory_auth(
            _request(path="/mcp"),
            api_key=None,
            authorization="Bearer raw-token",
        )

    assert exc_info.value.status_code == 403
    assert "resource" in exc_info.value.detail
    assert session.updates == []
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_resource"
    assert session.commits == 1


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
async def test_verify_memory_auth_rejects_legacy_hash_past_cutoff(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["read"],
            "stored_token_hash": auth._legacy_hash_secret("raw-token"),
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "allowed_scopes": ["read"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)
    monkeypatch.setattr(auth.settings, "credential_pepper", "test-pepper")
    monkeypatch.setattr(
        auth.settings,
        "credential_legacy_hash_cutoff_at",
        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    )

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_memory_auth(_request(), api_key=None, authorization="Bearer raw-token")

    assert exc_info.value.status_code == 403
    assert "retired credential format" in exc_info.value.detail


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
            "token_resource": "https://testserver/api/v1",
            "client_revoked_at": None,
        }
    )
    request = _request(path="/api/v1/capture")
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
    assert session.audit_events[0]["operation"] == "oauth.token_use"
    assert session.audit_events[0]["required_scope"] == "capture:write"
    assert session.audit_events[0]["status"] == "success"
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
            "token_resource": "https://testserver/api/v1",
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_capture_write_auth(
            _request(path="/api/v1/capture"),
            api_key=None,
            authorization="Bearer capture-token",
        )

    assert exc_info.value.status_code == 403
    assert "capture:write" in exc_info.value.detail
    assert session.audit_events[0]["operation"] == "oauth.token_use"
    assert session.audit_events[0]["required_scope"] == "capture:write"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "insufficient_scope"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["api_key", "browser_session"])
async def test_capture_helpers_use_stored_api_or_browser_grant(monkeypatch, auth_mode: str) -> None:
    async def fake_verify_api_key(request, api_key):
        auth._attach_auth_context(
            request,
            auth._context_from_scopes(
                tenant_id="tenant-a",
                auth_mode=auth_mode,
                token_hash_reference="stored-grant",
                scopes=("capture:write", "capture:job:read"),
            ),
        )
        return "stored-credential"

    monkeypatch.setattr(auth, "verify_api_key", fake_verify_api_key)

    write_request = _request()
    read_request = _request()
    assert await auth.verify_capture_write_auth(write_request, api_key=None, authorization=None) == "stored-credential"
    assert await auth.verify_capture_job_read_auth(read_request, api_key=None, authorization=None) == "stored-credential"
    assert write_request.state.auth_mode == auth_mode
    assert read_request.state.auth_mode == auth_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["api_key", "browser_session"])
@pytest.mark.parametrize(
    ("helper", "stored_scopes", "required_scope"),
    [
        (auth.verify_capture_write_auth, ("write",), "capture:write"),
        (auth.verify_capture_job_read_auth, ("read",), "capture:job:read"),
    ],
)
async def test_capture_helpers_refuse_missing_stored_grants(
    monkeypatch, auth_mode: str, helper, stored_scopes: tuple[str, ...], required_scope: str
) -> None:
    async def fake_verify_api_key(request, api_key):
        auth._attach_auth_context(
            request,
            auth._context_from_scopes(
                tenant_id="tenant-a",
                auth_mode=auth_mode,
                token_hash_reference="stored-grant",
                scopes=stored_scopes,
            ),
        )
        return "stored-credential"

    monkeypatch.setattr(auth, "verify_api_key", fake_verify_api_key)

    with pytest.raises(HTTPException) as exc_info:
        await helper(_request(), api_key=None, authorization=None)

    assert exc_info.value.status_code == 403
    assert required_scope in exc_info.value.detail


@pytest.mark.asyncio
async def test_capture_helper_preserves_non_extension_oauth_mode(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["capture:write"],
            "token_resource": "https://testserver/api/v1",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "codex-remote",
            "display_name": "Codex Remote",
            "allowed_scopes": ["capture:write"],
            "client_revoked_at": None,
        }
    )
    request = _request(path="/api/v1/capture")
    monkeypatch.setattr(auth, "async_session", lambda: session)

    assert await auth.verify_capture_write_auth(
        request, api_key=None, authorization="Bearer capture-token"
    ) == "capture-token"
    assert request.state.auth_mode == "mcp_oauth"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("helper", "stored_scope", "required_scope", "path"),
    [
        (auth.verify_capture_write_auth, "capture:job:read", "capture:write", "/api/v1/capture"),
        (
            auth.verify_capture_job_read_auth,
            "capture:write",
            "capture:job:read",
            "/api/v1/capture/jobs/job-a",
        ),
    ],
)
@pytest.mark.parametrize("client_key", ["codex-remote", "browser-extension:abc"])
async def test_oauth_capture_helpers_refuse_missing_stored_grants(
    monkeypatch, helper, stored_scope: str, required_scope: str, path: str, client_key: str
) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": [stored_scope],
            "token_resource": "https://testserver/api/v1",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": client_key,
            "display_name": "Capture client",
            "allowed_scopes": [stored_scope],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await helper(_request(path=path), api_key=None, authorization="Bearer capture-token")

    assert exc_info.value.status_code == 403
    assert required_scope in exc_info.value.detail
    assert session.audit_events[0]["required_scope"] == required_scope
    assert session.audit_events[0]["status"] == "denied"


@pytest.mark.asyncio
async def test_capture_helper_rejects_bearer_token_for_another_resource(monkeypatch) -> None:
    session = FakeSession(
        {
            "token_id": uuid.uuid4(),
            "tenant_id": "tenant-a",
            "token_scopes": ["capture:write"],
            "token_resource": "https://elsewhere.test/api/v1",
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
            "token_revoked_at": None,
            "client_id": uuid.uuid4(),
            "client_key": "browser-extension:abc",
            "display_name": "Palace Capture Extension",
            "allowed_scopes": ["capture:write"],
            "client_revoked_at": None,
        }
    )
    monkeypatch.setattr(auth, "async_session", lambda: session)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_capture_write_auth(
            _request(path="/api/v1/capture"),
            api_key=None,
            authorization="Bearer capture-token",
        )

    assert exc_info.value.status_code == 403
    assert "resource is invalid" in exc_info.value.detail
