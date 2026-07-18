import uuid
import base64
import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
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
    def __init__(self, row=None, rows=None, token_rows=None, authorization_code_row=None) -> None:
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.token_rows = token_rows or []
        self.authorization_code_row = authorization_code_row
        self.tokens = []
        self.revoked = []
        self.audit_events = []
        self.authorization_interactions = []
        self.refresh_families = []
        self.refresh_tokens = []
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
        if "insert into mcp_oauth_refresh_token_families" in sql:
            family = {**params, "id": uuid.uuid4()}
            self.refresh_families.append(family)
            return _Result([{"id": family["id"]}])
        if "insert into mcp_oauth_refresh_tokens" in sql:
            self.refresh_tokens.append(params)
            return _Result([])
        if "insert into mcp_oauth_authorization_interactions" in sql:
            self.authorization_interactions.append(params)
            return _Result([])
        if "from mcp_oauth_authorization_codes" in sql:
            return _Result([] if self.authorization_code_row is None else [self.authorization_code_row])
        if "from mcp_oauth_refresh_tokens" in sql:
            return _Result([])
        if "update mcp_oauth_authorization_codes" in sql:
            if self.authorization_code_row is None or self.authorization_code_row.get("used_at") is not None:
                return _Result([])
            self.authorization_code_row["used_at"] = params["used_at"]
            return _Result([{"id": self.authorization_code_row["id"]}])
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
        if "update mcp_oauth_refresh_token" in sql:
            self.revoked.append(params)
            return _Result([])
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _client(session: FakeSession, monkeypatch, *, base_url: str = "https://testserver") -> TestClient:
    app = FastAPI()
    app.include_router(mcp_oauth.router, prefix="/api/v1")
    app.include_router(mcp_oauth.metadata_router)
    monkeypatch.setattr(mcp_oauth, "async_session", lambda: session)
    return TestClient(app, base_url=base_url)


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


def test_s256_pkce_verifier_validation_is_exact_and_fail_closed() -> None:
    verifier = "A" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")

    assert mcp_oauth._matches_s256_pkce_verifier(verifier=verifier, challenge=challenge) is True
    assert mcp_oauth._matches_s256_pkce_verifier(verifier="short", challenge=challenge) is False
    assert mcp_oauth._matches_s256_pkce_verifier(verifier=("A" * 42) + "!", challenge=challenge) is False
    assert mcp_oauth._matches_s256_pkce_verifier(verifier=verifier, challenge=challenge[:-1] + "A") is False


def test_mcp_oauth_authorization_code_exchange_is_pkce_bound_and_one_use(monkeypatch) -> None:
    verifier = "A" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    client_row = _client_row(client_type="confidential_web", authorization_code_enabled=True)
    code_row = {
        "id": uuid.uuid4(),
        "grant_id": uuid.uuid4(),
        "pkce_challenge": challenge,
        "redirect_uri": "https://nebulaios.example/callback",
        "client_id": client_row["id"],
        "resource": "https://testserver/mcp",
        "scopes": ["read"],
        "revoked_at": None,
        "used_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    session = FakeSession(client_row, authorization_code_row=code_row)
    client = _client(session, monkeypatch)
    payload = {
        "grant_type": "authorization_code",
        "client_id": "codex-remote",
        "client_secret": "client-secret",
        "code": "one-use-code",
        "code_verifier": verifier,
        "redirect_uri": "https://nebulaios.example/callback",
    }

    response = client.post("/api/v1/memory/mcp/oauth/token", data=payload)
    replay = client.post("/api/v1/memory/mcp/oauth/token", data=payload)

    assert response.status_code == 200
    assert response.json()["resource"] == "https://testserver/mcp"
    assert replay.status_code == 400
    assert replay.json()["detail"] == "invalid_grant"
    assert session.tokens[0]["delegated_grant_id"] == code_row["grant_id"]
    assert session.audit_events[0]["operation"] == "oauth.authorization_code_exchange"


def test_mcp_oauth_authorization_code_exchange_rejects_wrong_redirect_or_verifier(monkeypatch) -> None:
    verifier = "A" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    client_row = _client_row(client_type="confidential_web", authorization_code_enabled=True)
    code_row = {
        "id": uuid.uuid4(), "grant_id": uuid.uuid4(), "pkce_challenge": challenge,
        "redirect_uri": "https://nebulaios.example/callback", "client_id": client_row["id"],
        "resource": "https://testserver/mcp", "scopes": ["read"], "revoked_at": None,
        "used_at": None, "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    session = FakeSession(client_row, authorization_code_row=code_row)
    client = _client(session, monkeypatch)

    wrong_redirect = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={"grant_type": "authorization_code", "client_id": "codex-remote", "client_secret": "client-secret",
              "code": "one-use-code", "code_verifier": verifier, "redirect_uri": "https://wrong.example/callback"},
    )
    wrong_verifier = client.post(
        "/api/v1/memory/mcp/oauth/token",
        data={"grant_type": "authorization_code", "client_id": "codex-remote", "client_secret": "client-secret",
              "code": "one-use-code", "code_verifier": "B" * 43, "redirect_uri": "https://nebulaios.example/callback"},
    )

    assert wrong_redirect.status_code == 400
    assert wrong_redirect.json()["detail"] == "invalid_grant"
    assert wrong_verifier.status_code == 400
    assert wrong_verifier.json()["detail"] == "invalid_grant"
    assert code_row["used_at"] is None


def test_mcp_oauth_refresh_rotation_reuses_are_rejected_and_revoke_the_family(monkeypatch) -> None:
    client_row = _client_row()
    refresh_row = {
        "refresh_id": uuid.uuid4(),
        "used_at": None,
        "token_revoked_at": None,
        "token_expires_at": datetime.now(timezone.utc) + timedelta(days=1),
        "family_id": uuid.uuid4(),
        "family_revoked_at": None,
        "grant_id": uuid.uuid4(),
        "client_id": client_row["id"],
        "resource": "https://testserver/mcp",
        "scopes": ["read", "write"],
        "grant_revoked_at": None,
    }

    class RefreshSession(FakeSession):
        async def execute(self, statement, params=None):
            sql = str(statement).lower()
            if "from mcp_oauth_refresh_tokens r" in sql:
                return _Result([refresh_row])
            if "update mcp_oauth_refresh_tokens set used_at" in sql:
                if refresh_row["used_at"] is not None:
                    return _Result([])
                refresh_row["used_at"] = params["now"]
                return _Result([{"id": refresh_row["refresh_id"]}])
            return await super().execute(statement, params)

    session = RefreshSession(client_row)
    client = _client(session, monkeypatch)
    payload = {
        "grant_type": "refresh_token",
        "client_id": "codex-remote",
        "client_secret": "client-secret",
        "refresh_token": "opaque-refresh-token",
        "scope": "read",
    }

    rotated = client.post("/api/v1/memory/mcp/oauth/token", data=payload)
    replay = client.post("/api/v1/memory/mcp/oauth/token", data=payload)

    assert rotated.status_code == 200
    assert rotated.json()["scope"] == "read"
    assert rotated.json()["refresh_token"]
    assert replay.status_code == 400
    assert replay.json()["detail"] == "invalid_grant"
    assert any("update mcp_oauth_refresh_token_families set revoked_at" in sql.lower() for sql, _ in session.statements)
    assert any("update mcp_oauth_access_tokens set revoked_at" in sql.lower() for sql, _ in session.statements)


def test_mcp_oauth_authorize_creates_browser_and_csrf_bound_interaction(monkeypatch) -> None:
    client_row = _client_row(
        client_type="confidential_web",
        authorization_code_enabled=True,
        redirect_uris=["https://nebulaios.example/callback"],
        allowed_resources=["https://api.palace.sarvent.cloud/mcp"],
    )
    session = FakeSession(client_row)
    client = _client(session, monkeypatch, base_url="https://api.palace.sarvent.cloud")

    response = client.get(
        "/api/v1/memory/mcp/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "tenant-a:codex-remote",
            "redirect_uri": "https://nebulaios.example/callback",
            "resource": "https://api.palace.sarvent.cloud/mcp",
            "scope": "read",
            "state": "opaque-client-state",
            "code_challenge": "A" * 43,
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://palace.sarvent.cloud/oauth/consent?interaction_id=")
    assert "palace_oauth_consent_session" in response.headers["set-cookie"]
    assert "Domain=.palace.sarvent.cloud" in response.headers["set-cookie"]
    assert session.authorization_interactions[0]["tenant_id"] == "tenant-a"
    assert session.authorization_interactions[0]["state"] == "opaque-client-state"
    assert session.authorization_interactions[0]["browser_session_hash"]
    assert session.authorization_interactions[0]["csrf_token_hash"]


def test_mcp_oauth_authorize_rejects_unregistered_redirect_uri(monkeypatch) -> None:
    client_row = _client_row(
        client_type="confidential_web",
        authorization_code_enabled=True,
        redirect_uris=["https://nebulaios.example/callback"],
        allowed_resources=["https://testserver/mcp"],
    )
    client = _client(FakeSession(client_row), monkeypatch)

    response = client.get(
        "/api/v1/memory/mcp/oauth/authorize",
        params={
            "response_type": "code", "client_id": "tenant-a:codex-remote",
            "redirect_uri": "https://attacker.example/callback", "resource": "https://testserver/mcp",
            "code_challenge": "A" * 43, "code_challenge_method": "S256",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_request"


def test_mcp_oauth_consent_summary_requires_the_tenant_bound_browser_session(monkeypatch) -> None:
    browser_session = "browser-session"
    interaction = {
        "tenant_id": "tenant-a",
        "resource": "https://testserver/mcp",
        "scopes": ["read"],
        "agent_scope_keys": ["codex"],
        "workspace_scope_keys": ["palaceoftruth"],
        "browser_session_hash": hash_secret(browser_session),
        "decision": None,
        "consumed_at": None,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "client_key": "codex-remote",
        "display_name": "Codex Remote",
    }

    class ConsentSession(FakeSession):
        async def execute(self, statement, params=None):
            if "from mcp_oauth_authorization_interactions" in str(statement).lower():
                return _Result([interaction])
            return await super().execute(statement, params)

    session = ConsentSession(_client_row())
    client = _client(session, monkeypatch)

    async def browser_api_key(request: Request):
        request.state.tenant_id = "tenant-a"
        return "browser-key"

    client.app.dependency_overrides[mcp_oauth.verify_api_key] = browser_api_key
    response = client.get(
        "/api/v1/memory/mcp/oauth/authorize/interaction-id",
        headers={"X-API-Key": "browser-key"},
        cookies={"palace_oauth_consent_session": browser_session},
    )
    assert response.status_code == 200
    assert response.json() == {
        "client_name": "Codex Remote",
        "tenant_id": "tenant-a",
        "resource": "https://testserver/mcp",
        "scopes": ["read"],
        "agent_scope_keys": ["codex"],
        "workspace_scope_keys": ["palaceoftruth"],
        "expires_at": interaction["expires_at"].isoformat(),
    }

    missing_cookie = client.get(
        "/api/v1/memory/mcp/oauth/authorize/interaction-id",
        headers={"X-API-Key": "browser-key"},
    )
    assert missing_cookie.status_code == 400
    assert missing_cookie.json()["detail"] == "invalid_request"


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
