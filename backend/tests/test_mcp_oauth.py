import uuid
import base64
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import mcp_oauth
from app.auth import hash_secret
from app.mcp_scopes import ALL_MCP_OPERATION_SCOPES


class _MappingRows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return _MappingRows(self._rows)


class FakeSession:
    def __init__(self, row=None, rows=None, token_rows=None) -> None:
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.token_rows = token_rows or []
        self.tokens = []
        self.revoked = []
        self.audit_events = []
        self.statements = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        self.statements.append((str(statement), params))
        if "from mcp_clients" in sql:
            rows = [
                row
                for row in self.rows
                if row["client_key"] == params["client_key"]
                and (params.get("tenant_id") is None or row["tenant_id"] == params["tenant_id"])
            ]
            return _Result(rows[:2])
        if "insert into mcp_oauth_access_tokens" in sql:
            self.tokens.append(params)
            return _Result([])
        if "insert into mcp_request_audit_events" in sql:
            self.audit_events.append(params)
            return _Result([])
        if "from mcp_oauth_access_tokens" in sql:
            rows = [
                row
                for row in self.token_rows
                if row["token_hash"] == params["token_hash"] and row["tenant_id"] == params["tenant_id"]
            ]
            return _Result(rows[:1])
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
    return TestClient(app, base_url="https://testserver")


def _client_row(**overrides) -> dict:
    row = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "client_key": "codex-remote",
        "display_name": "Codex Remote",
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
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    assert body["scope"] == "read"
    assert body["resource"] == "https://testserver/mcp"
    assert session.tokens[0]["tenant_id"] == "tenant-a"
    assert session.tokens[0]["scopes"] == '["read"]'
    assert session.tokens[0]["resource"] == "https://testserver/mcp"
    assert session.tokens[0]["expires_at"] > datetime.now(timezone.utc)
    assert session.audit_events[0]["operation"] == "oauth.token_issue"
    assert session.audit_events[0]["status"] == "success"
    assert session.audit_events[0]["client_name"] == "Codex Remote"
    params_summary = session.audit_events[0]["params_summary"]
    assert "client-secret" not in params_summary
    assert "raw-token" not in params_summary
    assert '"resource_kind": "mcp"' in params_summary
    client_lookup_sql = next(sql for sql, _ in session.statements if "FROM mcp_clients" in sql)
    assert "CAST(:tenant_id AS text) IS NULL" in client_lookup_sql


def test_mcp_oauth_token_endpoint_accepts_tenant_qualified_client_id(monkeypatch) -> None:
    tenant_a = _client_row(tenant_id="tenant-a", oauth_client_secret_hash=hash_secret("wrong-secret"))
    tenant_b = _client_row(tenant_id="tenant-b", oauth_client_secret_hash=hash_secret("client-secret"))
    session = FakeSession(rows=[tenant_a, tenant_b])
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "tenant-b:codex-remote",
            "client_secret": "client-secret",
            "scope": "read",
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 200
    assert session.tokens[0]["tenant_id"] == "tenant-b"


def test_mcp_oauth_token_endpoint_rejects_ambiguous_bare_client_id(monkeypatch) -> None:
    session = FakeSession(rows=[_client_row(tenant_id="tenant-a"), _client_row(tenant_id="tenant-b")])
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 401
    assert session.tokens == []


def test_mcp_oauth_token_endpoint_rejects_missing_or_wrong_resource(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    missing = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
        },
    )
    wrong = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "resource": "https://testserver/wrong",
        },
    )

    assert missing.status_code == 400
    assert wrong.status_code == 400
    assert session.tokens == []
    assert [event["status"] for event in session.audit_events] == ["denied", "denied"]
    assert [event["error_class"] for event in session.audit_events] == ["invalid_resource", "invalid_resource"]
    assert all(event["operation"] == "oauth.token_issue" for event in session.audit_events)
    assert "client-secret" not in session.audit_events[0]["params_summary"]
    assert "client-secret" not in session.audit_events[1]["params_summary"]


def test_mcp_oauth_token_endpoint_rejects_invalid_secret(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "wrong",
            "resource": "https://testserver/mcp",
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
        data={"grant_type": "client_credentials", "resource": "https://testserver/mcp"},
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
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 403
    assert session.tokens == []
    assert session.audit_events[0]["operation"] == "oauth.token_issue"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_scope"


def test_mcp_oauth_token_endpoint_fails_closed_on_unsupported_scope_row(monkeypatch) -> None:
    session = FakeSession(_client_row(allowed_scopes=["read", "unknown:scope"]))
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 403
    assert session.tokens == []
    assert session.audit_events[0]["operation"] == "oauth.token_issue"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_scope"


def test_mcp_oauth_token_endpoint_audits_unsupported_requested_scope(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "scope": "admin",
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 400
    assert session.tokens == []
    assert session.audit_events[0]["operation"] == "oauth.token_issue"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_scope"
    assert "client-secret" not in session.audit_events[0]["params_summary"]


def test_mcp_oauth_token_endpoint_audits_invalid_ttl(monkeypatch) -> None:
    session = FakeSession(_client_row(oauth_token_ttl_seconds=0))
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "resource": "https://testserver/mcp",
        },
    )

    assert response.status_code == 403
    assert session.tokens == []
    assert session.audit_events[0]["operation"] == "oauth.token_issue"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "invalid_ttl"
    assert "client-secret" not in session.audit_events[0]["params_summary"]


def test_mcp_oauth_revoke_is_idempotent(monkeypatch) -> None:
    client_row = _client_row()
    session = FakeSession(client_row)
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/revoke",
        data={"token": "raw-token", "client_id": "codex-remote", "client_secret": "client-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {"revoked": True}
    assert session.revoked == [
        {
            "token_hash": hash_secret("raw-token"),
            "tenant_id": "tenant-a",
            "client_id": client_row["id"],
        }
    ]
    revoke_sql = next(sql for sql, _ in session.statements if "UPDATE mcp_oauth_access_tokens" in sql)
    assert "tenant_id = :tenant_id" in revoke_sql
    assert "client_id = :client_id" in revoke_sql
    assert session.audit_events[0]["operation"] == "oauth.token_revoke"
    assert session.audit_events[0]["status"] == "success"
    assert "raw-token" not in session.audit_events[0]["params_summary"]


def test_mcp_oauth_revoke_scopes_to_authenticated_tenant_client(monkeypatch) -> None:
    tenant_a = _client_row(tenant_id="tenant-a", oauth_client_secret_hash=hash_secret("wrong-secret"))
    tenant_b = _client_row(tenant_id="tenant-b", oauth_client_secret_hash=hash_secret("client-secret"))
    session = FakeSession(rows=[tenant_a, tenant_b])
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/revoke",
        data={"token": "raw-token", "client_id": "tenant-b:codex-remote", "client_secret": "client-secret"},
    )

    assert response.status_code == 200
    assert session.revoked == [
        {
            "token_hash": hash_secret("raw-token"),
            "tenant_id": "tenant-b",
            "client_id": tenant_b["id"],
        }
    ]


def test_mcp_oauth_introspection_reports_active_token(monkeypatch) -> None:
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(days=1)
    session = FakeSession(
        _client_row(),
        token_rows=[
            {
                "tenant_id": "tenant-a",
                "token_hash": hash_secret("raw-token"),
                "token_scopes": ["read", "write"],
                "token_resource": "https://testserver/api/v1",
                "issued_at": issued_at,
                "expires_at": expires_at,
                "token_revoked_at": None,
                "client_key": "codex-remote",
                "client_revoked_at": None,
            }
        ],
    )
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/introspect",
        data={"token": "raw-token", "client_id": "codex-remote", "client_secret": "client-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "active": True,
        "client_id": "codex-remote",
        "scope": "read write",
        "token_type": "Bearer",
        "exp": int(expires_at.timestamp()),
        "iat": int(issued_at.timestamp()),
        "aud": "https://testserver/api/v1",
        "iss": "https://testserver/api/v1/memory/mcp/oauth",
    }
    assert session.audit_events[0]["operation"] == "oauth.token_introspect"
    assert session.audit_events[0]["status"] == "success"
    assert "raw-token" not in session.audit_events[0]["params_summary"]
    assert '"active": true' in session.audit_events[0]["params_summary"]


def test_mcp_oauth_introspection_hides_inactive_token_details(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.post(
        "/api/v1/memory/mcp/oauth/introspect",
        data={"token": "missing-token", "client_id": "codex-remote", "client_secret": "client-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "active": False,
        "client_id": None,
        "scope": None,
        "token_type": None,
        "exp": None,
        "iat": None,
        "aud": None,
        "iss": None,
    }
    assert session.audit_events[0]["operation"] == "oauth.token_introspect"
    assert session.audit_events[0]["status"] == "denied"
    assert session.audit_events[0]["error_class"] == "inactive_token"
    assert "missing-token" not in session.audit_events[0]["params_summary"]


def test_mcp_oauth_protected_resource_metadata_lists_scopes(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    body = response.json()
    assert body["resource"].endswith("/mcp")
    assert body["resource_name"] == "Palace MCP"
    assert body["bearer_methods_supported"] == ["header"]
    assert body["scopes_supported"] == list(ALL_MCP_OPERATION_SCOPES)
    assert {scope["value"] for scope in body["scope_catalog"]} == set(ALL_MCP_OPERATION_SCOPES)
    assert any(scope["description"] for scope in body["scope_catalog"] if scope["value"] == "capture:write")


def test_mcp_oauth_protected_resource_metadata_supports_rfc_path(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.get("/.well-known/oauth-protected-resource/mcp")

    assert response.status_code == 200
    assert response.json()["resource"] == "https://testserver/mcp"


def test_palace_api_oauth_protected_resource_metadata_lists_api_resource(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.get("/.well-known/oauth-protected-resource/api/v1")

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://testserver/api/v1"
    assert body["resource_name"] == "Palace API"
    assert body["authorization_servers"] == ["https://testserver/api/v1/memory/mcp/oauth"]
    assert body["scopes_supported"] == list(ALL_MCP_OPERATION_SCOPES)


def test_mcp_oauth_authorization_server_metadata(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    body = response.json()
    assert body["issuer"] == "https://testserver/api/v1/memory/mcp/oauth"
    assert body["token_endpoint"] == "https://testserver/api/v1/memory/mcp/oauth/token"
    assert body["revocation_endpoint"] == "https://testserver/api/v1/memory/mcp/oauth/revoke"
    assert body["introspection_endpoint"] == "https://testserver/api/v1/memory/mcp/oauth/introspect"
    assert body["grant_types_supported"] == ["client_credentials"]
    assert body["token_endpoint_auth_methods_supported"] == ["client_secret_basic", "client_secret_post"]
    assert body["code_challenge_methods_supported"] == []


def test_mcp_oauth_authorization_server_metadata_supports_issuer_well_known_path(monkeypatch) -> None:
    session = FakeSession(_client_row())
    client = _client(session, monkeypatch)

    response = client.get("/.well-known/oauth-authorization-server/api/v1/memory/mcp/oauth")

    assert response.status_code == 200
    assert response.json()["issuer"] == "https://testserver/api/v1/memory/mcp/oauth"


def test_mcp_oauth_metadata_forces_https_resource_for_proxied_http(monkeypatch) -> None:
    session = FakeSession(_client_row())
    app = FastAPI()
    app.include_router(mcp_oauth.router, prefix="/api/v1")
    app.include_router(mcp_oauth.metadata_router)
    monkeypatch.setattr(mcp_oauth, "async_session", lambda: session)
    client = TestClient(app, base_url="http://api.palace.sarvent.cloud")

    metadata = client.get("/.well-known/oauth-protected-resource")
    token = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "resource": "https://api.palace.sarvent.cloud/mcp",
        },
    )

    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "https://api.palace.sarvent.cloud/mcp"
    assert metadata.json()["authorization_servers"] == ["https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth"]
    assert token.status_code == 200
    assert token.json()["resource"] == "https://api.palace.sarvent.cloud/mcp"

    api_metadata = client.get("/.well-known/oauth-protected-resource/api/v1")
    api_token = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "codex-remote",
            "client_secret": "client-secret",
            "resource": "https://api.palace.sarvent.cloud/api/v1",
        },
    )
    assert api_metadata.status_code == 200
    assert api_metadata.json()["resource"] == "https://api.palace.sarvent.cloud/api/v1"
    assert api_token.status_code == 200
    assert api_token.json()["resource"] == "https://api.palace.sarvent.cloud/api/v1"
