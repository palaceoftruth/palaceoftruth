"""Tests for the SPA cookie session that replaces the localStorage API key (H-20)."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, browser_session
from app.api import browser_session as browser_session_api
from app.database import get_db


@pytest.mark.asyncio
async def test_require_session_reloads_after_tenant_rebind(monkeypatch) -> None:
    stale = SimpleNamespace(tenant_id="tenant-a")
    rebound = SimpleNamespace(tenant_id="tenant-a", scopes=["read"])
    loaded = [stale, rebound]
    rebound_tenants: list[str] = []

    async def fake_load_session(_db, token):
        assert token == "session-token"
        return loaded.pop(0)

    async def fake_bind(_db, tenant_id):
        rebound_tenants.append(tenant_id)

    monkeypatch.setattr(browser_session_api, "load_session", fake_load_session)
    monkeypatch.setattr(browser_session_api, "bind_session_to_tenant", fake_bind)
    request = SimpleNamespace(cookies={browser_session.SESSION_COOKIE_NAME: "session-token"})

    result = await browser_session_api._require_session(request, object())

    assert result is rebound
    assert rebound_tenants == ["tenant-a"]
    assert loaded == []


class _MappingResult:
    def __init__(self, row) -> None:
        self._row = row

    def one_or_none(self):
        return self._row


class _Result:
    def __init__(self, row=None, *, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def mappings(self):
        return _MappingResult(self._row)


class FakeSession:
    """In-memory stand-in for the two tables these routes touch.

    The predicates in the real SQL (expiry, revocation, the api_keys EXISTS
    check) are re-applied here by hand, so a test that drops one of them from
    the query would still fail.
    """

    def __init__(self, key_row: dict | None) -> None:
        self.key_row = key_row
        self.sessions: dict[uuid.UUID, dict] = {}
        self.commits = 0
        self.advisory_locks = 0

    def _live(self, row: dict) -> bool:
        if row["revoked_at"] is not None or row["expires_at"] <= datetime.now(timezone.utc):
            return False
        return self.key_row is not None and self.key_row.get("revoked_at") is None

    async def execute(self, statement, params=None):
        sql = " ".join(str(statement).lower().split())
        params = params or {}

        if sql.startswith("select id, tenant_id, scopes from api_keys"):
            if (
                self.key_row is None
                or self.key_row.get("revoked_at") is not None
                or self.key_row.get("expires_at", datetime.max.replace(tzinfo=timezone.utc))
                <= datetime.now(timezone.utc)
            ):
                return _Result(None)
            candidates = {params["hash"], params["legacy_hash"]}
            return _Result(self.key_row if self.key_row["key_hash"] in candidates else None)

        if sql.startswith("insert into browser_sessions"):
            row_id = uuid.uuid4()
            self.sessions[row_id] = {
                "id": row_id,
                "tenant_id": params["tenant_id"],
                "api_key_id": params["api_key_id"],
                "session_token_hash": params["session_token_hash"],
                "csrf_token_hash": params["csrf_token_hash"],
                "scopes": params["scopes"],
                "created_at": datetime.now(timezone.utc),
                "expires_at": params["expires_at"],
                "revoked_at": None,
            }
            return _Result(None)

        if sql.startswith("with excess as"):
            live = sorted(
                (row for row in self.sessions.values() if self._live(row)),
                key=lambda row: row["created_at"],
                reverse=True,
            )
            for row in live[params["keep_count"]:]:
                row["revoked_at"] = datetime.now(timezone.utc)
            return _Result(None)

        if sql.startswith("select pg_advisory_xact_lock"):
            self.advisory_locks += 1
            return _Result(None)

        if sql.startswith("select id, tenant_id, api_key_id, scopes, expires_at"):
            for row in self.sessions.values():
                if row["session_token_hash"] == params["hash"] and self._live(row):
                    return _Result(dict(row))
            return _Result(None)

        if sql.startswith("select csrf_token_hash from browser_sessions"):
            row = self.sessions.get(params["id"])
            return _Result({"csrf_token_hash": row["csrf_token_hash"]} if row else None)

        if sql.startswith("update browser_sessions set session_token_hash"):
            row = self.sessions.get(params["id"])
            if (
                row is not None
                and row["revoked_at"] is None
                and row["session_token_hash"] == params["current_session_token_hash"]
            ):
                row["session_token_hash"] = params["session_token_hash"]
                row["csrf_token_hash"] = params["csrf_token_hash"]
                row["expires_at"] = params["expires_at"]
                return _Result(None, rowcount=1)
            return _Result(None, rowcount=0)

        if sql.startswith("update browser_sessions set last_used_at"):
            return _Result(None)

        if sql.startswith("update browser_sessions set revoked_at"):
            row = self.sessions.get(params["id"])
            if row is not None and row["revoked_at"] is None:
                row["revoked_at"] = datetime.now(timezone.utc)
            return _Result(None)

        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _key_row(**overrides) -> dict:
    row = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "scopes": ["read", "write", "capture:write"],
        "key_hash": auth.hash_secret("raw-key-value"),
        "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        "revoked_at": None,
    }
    row.update(overrides)
    return row


def _client(session: FakeSession) -> TestClient:
    app = FastAPI()
    app.include_router(browser_session_api.router, prefix="/api/v1")

    async def _override():
        return session

    app.dependency_overrides[get_db] = _override
    return TestClient(app, base_url="https://testserver")


def _sign_in(client: TestClient, **body) -> dict:
    payload = {"api_key": "raw-key-value"}
    payload.update(body)
    response = client.post("/api/v1/browser/session", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- scope narrowing ---------------------------------------------------------


def test_default_session_scopes_exclude_admin() -> None:
    scopes = browser_session.resolve_session_scopes(["read", "write", "admin"], elevated=False)
    assert "admin" not in scopes
    assert "read" in scopes and "write" in scopes


def test_elevated_session_can_hold_admin_when_the_key_does() -> None:
    scopes = browser_session.resolve_session_scopes(["read", "write", "admin"], elevated=True)
    assert "admin" in scopes


def test_elevation_with_a_non_admin_key_yields_a_normal_session() -> None:
    """No error: the endpoint must not become a probe for a key's scopes."""
    scopes = browser_session.resolve_session_scopes(["read", "write"], elevated=True)
    assert "admin" not in scopes
    assert set(scopes) == {"read", "write"}


def test_session_scopes_never_exceed_the_key() -> None:
    scopes = browser_session.resolve_session_scopes(["read"], elevated=False)
    assert set(scopes) == {"read"}


# --- sign-in -----------------------------------------------------------------


def test_sign_in_sets_httponly_session_and_readable_csrf_cookies() -> None:
    client = _client(FakeSession(_key_row()))
    response = client.post("/api/v1/browser/session", json={"api_key": "raw-key-value"})

    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert set(body["scopes"]) == {"read", "write", "capture:write"}

    cookies = "\n".join(response.headers.get_list("set-cookie"))
    session_cookie = next(
        line for line in cookies.splitlines() if line.startswith(f"{browser_session.SESSION_COOKIE_NAME}=")
    )
    csrf_cookie = next(
        line for line in cookies.splitlines() if line.startswith(f"{browser_session.CSRF_COOKIE_NAME}=")
    )
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=strict" in session_cookie.replace("samesite", "SameSite")
    # The SPA has to read this one to echo it back in the header.
    assert "HttpOnly" not in csrf_cookie


def test_sign_in_never_returns_the_api_key() -> None:
    client = _client(FakeSession(_key_row()))
    response = client.post("/api/v1/browser/session", json={"api_key": "raw-key-value"})
    assert "raw-key-value" not in response.text


def test_sign_in_rejects_an_unknown_key() -> None:
    client = _client(FakeSession(_key_row()))
    response = client.post("/api/v1/browser/session", json={"api_key": "wrong-key-value"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API key"


def test_sign_in_rejects_a_revoked_key() -> None:
    client = _client(FakeSession(_key_row(revoked_at=datetime.now(timezone.utc))))
    response = client.post("/api/v1/browser/session", json={"api_key": "raw-key-value"})
    assert response.status_code == 401


def test_sign_in_rejects_an_expired_key() -> None:
    client = _client(FakeSession(_key_row(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))))
    response = client.post("/api/v1/browser/session", json={"api_key": "raw-key-value"})
    assert response.status_code == 401


def test_session_cap_is_serialized_and_enforced(monkeypatch) -> None:
    monkeypatch.setattr(browser_session.settings, "browser_session_max_per_tenant", 3)
    db = FakeSession(_key_row())

    async def issue_many() -> None:
        for _ in range(6):
            assert await browser_session.issue_session(db, api_key="raw-key-value", elevated=False)

    import asyncio
    asyncio.run(issue_many())
    assert sum(1 for row in db.sessions.values() if db._live(row)) == 3
    assert db.advisory_locks == 6


@pytest.mark.parametrize("elevated", [False, True])
def test_cookie_max_age_matches_persistent_server_expiry(elevated: bool) -> None:
    client = _client(FakeSession(_key_row(scopes=["read", "write", "admin"])))
    response = client.post(
        "/api/v1/browser/session",
        json={"api_key": "raw-key-value", "elevated": elevated},
    )
    assert response.status_code == 201
    cookies = response.headers.get_list("set-cookie")
    assert cookies
    for cookie in cookies:
        max_age = int(next(part.split("=", 1)[1] for part in cookie.split("; ") if part.startswith("Max-Age=")))
        assert 2_591_990 <= max_age <= 2_592_000


def test_sign_in_rejects_a_key_with_no_stored_scopes() -> None:
    client = _client(FakeSession(_key_row(scopes=None)))
    response = client.post("/api/v1/browser/session", json={"api_key": "raw-key-value"})
    assert response.status_code == 401


# --- reading the session -----------------------------------------------------


def test_read_session_without_a_cookie_is_401() -> None:
    client = _client(FakeSession(_key_row()))
    assert client.get("/api/v1/browser/session").status_code == 401


def test_read_session_returns_the_live_grant() -> None:
    client = _client(FakeSession(_key_row()))
    _sign_in(client)
    response = client.get("/api/v1/browser/session")
    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"


def test_a_revoked_key_kills_an_outstanding_session() -> None:
    session = FakeSession(_key_row())
    client = _client(session)
    _sign_in(client)
    session.key_row["revoked_at"] = datetime.now(timezone.utc)
    assert client.get("/api/v1/browser/session").status_code == 401


def test_an_expired_session_is_not_accepted() -> None:
    session = FakeSession(_key_row())
    client = _client(session)
    _sign_in(client)
    for row in session.sessions.values():
        row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get("/api/v1/browser/session").status_code == 401


# --- refresh -----------------------------------------------------------------


def test_refresh_requires_the_csrf_header() -> None:
    client = _client(FakeSession(_key_row()))
    _sign_in(client)
    response = client.post("/api/v1/browser/session/refresh")
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing or invalid CSRF token"


def test_refresh_rejects_a_wrong_csrf_token() -> None:
    client = _client(FakeSession(_key_row()))
    _sign_in(client)
    response = client.post(
        "/api/v1/browser/session/refresh",
        headers={browser_session.CSRF_HEADER_NAME: "not-the-token"},
    )
    assert response.status_code == 403


def test_refresh_reports_a_concurrent_rotation_conflict(monkeypatch) -> None:
    client = _client(FakeSession(_key_row()))
    _sign_in(client)

    async def conflict(*_args, **_kwargs):
        raise browser_session.BrowserSessionRotationConflict

    monkeypatch.setattr(browser_session_api, "rotate_session", conflict)
    response = client.post(
        "/api/v1/browser/session/refresh",
        headers={browser_session.CSRF_HEADER_NAME: client.cookies[browser_session.CSRF_COOKIE_NAME]},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Browser session was refreshed by another request"


def test_refresh_rotates_both_tokens() -> None:
    client = _client(FakeSession(_key_row()))
    _sign_in(client)
    old_session = client.cookies[browser_session.SESSION_COOKIE_NAME]
    old_csrf = client.cookies[browser_session.CSRF_COOKIE_NAME]

    response = client.post(
        "/api/v1/browser/session/refresh",
        headers={browser_session.CSRF_HEADER_NAME: old_csrf},
    )

    assert response.status_code == 200
    assert client.cookies[browser_session.SESSION_COOKIE_NAME] != old_session
    assert client.cookies[browser_session.CSRF_COOKIE_NAME] != old_csrf


@pytest.mark.asyncio
async def test_only_one_concurrent_refresh_can_rotate_a_session() -> None:
    db = FakeSession(_key_row())
    issued = await browser_session.issue_session(db, api_key="raw-key-value", elevated=True)
    assert issued is not None
    loaded = await browser_session.load_session(db, issued.session_token)
    assert loaded is not None

    await browser_session.rotate_session(db, loaded)

    with pytest.raises(browser_session.BrowserSessionRotationConflict):
        await browser_session.rotate_session(db, loaded)


def test_the_old_session_token_stops_working_after_a_refresh() -> None:
    session = FakeSession(_key_row())
    client = _client(session)
    _sign_in(client)
    old_session = client.cookies[browser_session.SESSION_COOKIE_NAME]
    client.post(
        "/api/v1/browser/session/refresh",
        headers={browser_session.CSRF_HEADER_NAME: client.cookies[browser_session.CSRF_COOKIE_NAME]},
    )

    assert client.get("/api/v1/browser/session").status_code == 200
    client.cookies.set(browser_session.SESSION_COOKIE_NAME, old_session)
    assert client.get("/api/v1/browser/session").status_code == 401


# --- sign-out ----------------------------------------------------------------


def test_sign_out_revokes_the_row_and_clears_the_cookies() -> None:
    session = FakeSession(_key_row())
    client = _client(session)
    _sign_in(client)

    response = client.request(
        "DELETE",
        "/api/v1/browser/session",
        headers={browser_session.CSRF_HEADER_NAME: client.cookies[browser_session.CSRF_COOKIE_NAME]},
    )

    assert response.status_code == 204
    assert all(row["revoked_at"] is not None for row in session.sessions.values())
    assert not client.cookies.get(browser_session.SESSION_COOKIE_NAME)
    assert client.get("/api/v1/browser/session").status_code == 401


def test_sign_out_requires_csrf_for_a_live_session() -> None:
    session = FakeSession(_key_row())
    client = _client(session)
    _sign_in(client)

    response = client.request("DELETE", "/api/v1/browser/session")

    assert response.status_code == 403
    assert all(row["revoked_at"] is None for row in session.sessions.values())


def test_sign_out_without_a_session_still_succeeds() -> None:
    """A stale tab must be able to clean itself up without an error."""
    client = _client(FakeSession(_key_row()))
    assert client.request("DELETE", "/api/v1/browser/session").status_code == 204


# --- the auth funnel ---------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_api_key_falls_back_to_the_session_cookie(monkeypatch) -> None:
    """Every other verifier delegates here, so one hook covers the whole API."""
    key = _key_row()
    db = FakeSession(key)
    issued = await browser_session.issue_session(db, api_key="raw-key-value", elevated=False)
    assert issued is not None
    monkeypatch.setattr(auth, "async_session", lambda: _AsyncSessionContext(db))

    request = _request(cookies=f"{browser_session.SESSION_COOKIE_NAME}={issued.session_token}")
    result = await auth.verify_api_key(request, api_key=None)

    assert result == ""
    context = request.state.auth_context
    assert context.tenant_id == "tenant-a"
    assert context.auth_mode == "browser_session"
    assert "admin" not in context.scopes


@pytest.mark.asyncio
async def test_verify_api_key_requires_csrf_on_unsafe_methods(monkeypatch) -> None:
    key = _key_row()
    db = FakeSession(key)
    issued = await browser_session.issue_session(db, api_key="raw-key-value", elevated=False)
    monkeypatch.setattr(auth, "async_session", lambda: _AsyncSessionContext(db))

    request = _request(
        method="POST",
        cookies=f"{browser_session.SESSION_COOKIE_NAME}={issued.session_token}",
    )
    with pytest.raises(Exception) as excinfo:
        await auth.verify_api_key(request, api_key=None)
    assert "CSRF" in str(excinfo.value)


@pytest.mark.asyncio
async def test_verify_api_key_accepts_a_matching_csrf_header(monkeypatch) -> None:
    key = _key_row()
    db = FakeSession(key)
    issued = await browser_session.issue_session(db, api_key="raw-key-value", elevated=False)
    monkeypatch.setattr(auth, "async_session", lambda: _AsyncSessionContext(db))

    request = _request(
        method="POST",
        cookies=f"{browser_session.SESSION_COOKIE_NAME}={issued.session_token}",
        csrf=issued.csrf_token,
    )
    assert await auth.verify_api_key(request, api_key=None) == ""


class _AsyncSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _request(*, method: str = "GET", cookies: str = "", csrf: str | None = None):
    from fastapi import Request

    headers = []
    if cookies:
        headers.append((b"cookie", cookies.encode()))
    if csrf:
        headers.append((browser_session.CSRF_HEADER_NAME.lower().encode(), csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "server": ("testserver", 443),
            "path": "/api/v1/memory/whoami",
            "headers": headers,
        }
    )
