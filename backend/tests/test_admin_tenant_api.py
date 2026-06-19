import uuid
from datetime import datetime, timezone
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import router
from app.database import get_db


class _MappingResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def one(self):
        if len(self._rows) != 1:
            raise AssertionError(f"Expected exactly one row, got {len(self._rows)}")
        return self._rows[0]

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return _MappingResult(self._rows)

    def fetchall(self):
        return list(self._rows)


class FakeSession:
    def __init__(self, api_keys=None, audit_events=None) -> None:
        self.api_keys = list(api_keys or [])
        self.audit_events = list(audit_events or [])
        self.mcp_clients = []
        self.revoked_tokens = []
        self.commit_count = 0

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}

        if "from api_keys" in sql and "revoked_at is null" in sql and "limit 1" in sql:
            rows = [
                row
                for row in self.api_keys
                if row["tenant_id"] == params["tenant_id"] and row["revoked_at"] is None
            ]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return _Result(rows[:1])

        if "from api_keys" in sql and "order by created_at desc" in sql:
            rows = [row for row in self.api_keys if row["tenant_id"] == params["tenant_id"]]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return _Result(rows)

        if "insert into api_keys" in sql:
            row = {
                "id": uuid.uuid4(),
                "tenant_id": params["tenant_id"],
                "description": params["description"],
                "created_at": datetime.now(timezone.utc),
                "revoked_at": None,
                "last_used_at": None,
                "key_hash": params["key_hash"],
            }
            self.api_keys.append(row)
            return _Result([row])

        if "insert into api_key_audit_events" in sql:
            details = params.get("details") or "{}"
            if isinstance(details, str):
                details = json.loads(details)
            row = {
                "id": uuid.uuid4(),
                "tenant_id": params["tenant_id"],
                "api_key_id": params["api_key_id"],
                "event_type": params["event_type"],
                "actor_type": "admin",
                "decision": params["decision"],
                "details": details,
                "created_at": datetime.now(timezone.utc),
            }
            self.audit_events.append(row)
            return _Result([row])

        if "from api_key_audit_events" in sql:
            rows = [row for row in self.audit_events if row["tenant_id"] == params["tenant_id"]]
            rows.sort(key=lambda row: row["created_at"], reverse=True)
            return _Result(rows)

        if "insert into mcp_clients" in sql:
            row = next(
                (
                    client
                    for client in self.mcp_clients
                    if client["tenant_id"] == params["tenant_id"]
                    and client["client_key"] == params["client_key"]
                ),
                None,
            )
            if row is None:
                row = {
                    "id": uuid.uuid4(),
                    "tenant_id": params["tenant_id"],
                    "client_key": params["client_key"],
                    "display_name": params["display_name"],
                    "allowed_scopes": json.loads(params["allowed_scopes"]),
                    "metadata": json.loads(params["metadata"]),
                    "oauth_client_secret_hash": params["secret_hash"],
                    "oauth_revoked_at": None,
                    "oauth_token_ttl_seconds": params["token_ttl_seconds"],
                }
                self.mcp_clients.append(row)
            else:
                row.update(
                    {
                        "display_name": params["display_name"],
                        "allowed_scopes": json.loads(params["allowed_scopes"]),
                        "metadata": json.loads(params["metadata"]),
                        "oauth_client_secret_hash": params["secret_hash"],
                        "oauth_revoked_at": None,
                        "oauth_token_ttl_seconds": params["token_ttl_seconds"],
                    }
                )
            return _Result([row])

        if "update api_keys" in sql and "where tenant_id = :tenant_id and revoked_at is null" in sql:
            revoked = []
            for row in self.api_keys:
                if row["tenant_id"] == params["tenant_id"] and row["revoked_at"] is None:
                    row["revoked_at"] = datetime.now(timezone.utc)
                    revoked.append({"id": row["id"]})
            return _Result(revoked)

        if "update api_keys" in sql and "where tenant_id = :tenant_id and id = :key_id" in sql:
            key_id = params["key_id"]
            if isinstance(key_id, str):
                key_id = uuid.UUID(key_id)
            for row in self.api_keys:
                if row["tenant_id"] == params["tenant_id"] and row["id"] == key_id:
                    if row["revoked_at"] is None:
                        row["revoked_at"] = datetime.now(timezone.utc)
                    return _Result([row])
            return _Result([])

        if "update mcp_clients" in sql:
            for row in self.mcp_clients:
                if row["tenant_id"] == params["tenant_id"] and row["id"] == params["client_id"]:
                    row["oauth_revoked_at"] = row["oauth_revoked_at"] or datetime.now(timezone.utc)
                    return _Result([row])
            return _Result([])

        if "update mcp_oauth_access_tokens" in sql:
            self.revoked_tokens.append(params)
            return _Result([])

        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commit_count += 1

    async def get(self, *args, **kwargs):
        return None

    async def refresh(self, *args, **kwargs):
        return None


def _api_key_row(*, tenant_id: str, description: str | None, revoked: bool = False) -> dict:
    return {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "description": description,
        "created_at": datetime.now(timezone.utc),
        "revoked_at": datetime.now(timezone.utc) if revoked else None,
        "last_used_at": None,
        "key_hash": "existing-hash",
    }


def _client(session: FakeSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_admin_tenant_lifecycle_requires_admin_secret() -> None:
    client = _client(FakeSession())

    response = client.get("/api/v1/admin/tenants/tenant-a/api-keys")

    assert response.status_code == 403


def test_register_tenant_creates_new_key_when_missing() -> None:
    session = FakeSession()
    client = _client(session)

    response = client.post(
        "/api/v1/admin/tenants/register",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={"tenant_id": "tenant-a", "description": "ExampleOS tenant"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["created"] is True
    assert isinstance(body["api_key"], str) and len(body["api_key"]) == 64
    assert body["active_key_count"] == 1
    assert body["active_key"]["status"] == "active"
    assert body["active_key"]["last_used_at"] is None
    assert len(session.api_keys) == 1
    assert session.audit_events[0]["event_type"] == "register_created"
    assert session.audit_events[0]["api_key_id"] == session.api_keys[0]["id"]
    assert session.audit_events[0]["details"] == {"description_present": True}


def test_register_tenant_is_idempotent_when_active_key_exists() -> None:
    existing = _api_key_row(tenant_id="tenant-a", description="Existing tenant")
    session = FakeSession([existing])
    client = _client(session)

    response = client.post(
        "/api/v1/admin/tenants/register",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={"tenant_id": "tenant-a", "description": "ExampleOS tenant"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is False
    assert body["api_key"] is None
    assert body["active_key_count"] == 1
    assert body["active_key"]["id"] == str(existing["id"])
    assert len(session.api_keys) == 1
    assert session.audit_events[0]["event_type"] == "register_replay"
    assert session.audit_events[0]["decision"] == "reused_existing_active_key"


def test_list_tenant_api_keys_includes_active_and_revoked() -> None:
    active = _api_key_row(tenant_id="tenant-a", description="Active")
    revoked = _api_key_row(tenant_id="tenant-a", description="Old", revoked=True)
    session = FakeSession([active, revoked])
    client = _client(session)

    response = client.get(
        "/api/v1/admin/tenants/tenant-a/api-keys",
        headers={"X-Admin-Secret": "test-admin-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["active_key_count"] == 1
    assert {row["status"] for row in body["keys"]} == {"active", "revoked"}
    assert all("last_used_at" in row for row in body["keys"])


def test_list_tenant_api_key_audit_events_returns_secret_safe_events() -> None:
    key_id = uuid.uuid4()
    session = FakeSession(
        audit_events=[
            {
                "id": uuid.uuid4(),
                "tenant_id": "tenant-a",
                "api_key_id": key_id,
                "event_type": "rotate",
                "actor_type": "admin",
                "decision": "created_replacement_key",
                "details": {"revoked_count": 1},
                "created_at": datetime.now(timezone.utc),
            },
            {
                "id": uuid.uuid4(),
                "tenant_id": "tenant-b",
                "api_key_id": uuid.uuid4(),
                "event_type": "register_created",
                "actor_type": "admin",
                "decision": "created_new_active_key",
                "details": {},
                "created_at": datetime.now(timezone.utc),
            },
        ]
    )
    client = _client(session)

    response = client.get(
        "/api/v1/admin/tenants/tenant-a/api-keys/audit",
        headers={"X-Admin-Secret": "test-admin-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert len(body["events"]) == 1
    assert body["events"][0]["api_key_id"] == str(key_id)
    assert body["events"][0]["event_type"] == "rotate"
    assert "api_key" not in body["events"][0]["details"]


def test_register_mcp_oauth_client_returns_secret_once_and_hashes_storage() -> None:
    session = FakeSession()
    client = _client(session)

    response = client.post(
        "/api/v1/admin/tenants/tenant-a/mcp-clients/register",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={
            "client_key": "codex-remote",
            "display_name": "Codex remote MCP",
            "allowed_scopes": ["read", "write"],
            "metadata": {"owner": "codex"},
            "token_ttl_seconds": 1800,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["client"]["client_key"] == "codex-remote"
    assert body["client"]["allowed_scopes"] == ["read", "write"]
    assert isinstance(body["client_secret"], str) and len(body["client_secret"]) > 30
    stored = session.mcp_clients[0]
    assert stored["oauth_client_secret_hash"] != body["client_secret"]
    assert stored["oauth_token_ttl_seconds"] == 1800


def test_revoke_mcp_oauth_client_revokes_tokens_for_tenant_client() -> None:
    client_id = uuid.uuid4()
    row = {
        "id": client_id,
        "tenant_id": "tenant-a",
        "client_key": "codex-remote",
        "display_name": "Codex remote MCP",
        "allowed_scopes": ["read"],
        "metadata": {},
        "oauth_client_secret_hash": "hash",
        "oauth_revoked_at": None,
        "oauth_token_ttl_seconds": 3600,
    }
    session = FakeSession()
    session.mcp_clients.append(row)
    client = _client(session)

    response = client.post(
        f"/api/v1/admin/tenants/tenant-a/mcp-clients/{client_id}/revoke",
        headers={"X-Admin-Secret": "test-admin-secret"},
    )

    assert response.status_code == 200
    assert response.json()["client"]["revoked_at"] is not None
    assert session.revoked_tokens == [{"tenant_id": "tenant-a", "client_id": client_id}]


def test_list_tenant_api_keys_normalizes_path_tenant_id() -> None:
    active = _api_key_row(tenant_id="tenant-a", description="Active")
    session = FakeSession([active])
    client = _client(session)

    response = client.get(
        "/api/v1/admin/tenants/%20tenant-a%20/api-keys",
        headers={"X-Admin-Secret": "test-admin-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["active_key_count"] == 1


def test_rotate_tenant_api_key_revokes_existing_active_keys() -> None:
    active = _api_key_row(tenant_id="tenant-a", description="Old active")
    already_revoked = _api_key_row(tenant_id="tenant-a", description="Old revoked", revoked=True)
    session = FakeSession([active, already_revoked])
    client = _client(session)

    response = client.post(
        "/api/v1/admin/tenants/tenant-a/api-keys/rotate",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={"description": "Rotated key", "revoke_existing": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["api_key"], str) and len(body["api_key"]) == 64
    assert body["tenant_id"] == "tenant-a"
    assert body["revoked_count"] == 1
    assert body["active_key"]["description"] == "Rotated key"
    assert body["active_key"]["status"] == "active"
    assert sum(1 for row in session.api_keys if row["tenant_id"] == "tenant-a" and row["revoked_at"] is None) == 1
    assert session.audit_events[0]["event_type"] == "rotate"
    assert session.audit_events[0]["details"]["revoked_count"] == 1


def test_rotate_tenant_api_key_rejects_blank_path_tenant_id() -> None:
    session = FakeSession()
    client = _client(session)

    response = client.post(
        "/api/v1/admin/tenants/%20/api-keys/rotate",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={"description": "Rotated key"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "tenant_id must not be blank"
    assert session.api_keys == []


def test_revoke_tenant_api_key_marks_key_revoked() -> None:
    active = _api_key_row(tenant_id="tenant-a", description="Active")
    session = FakeSession([active])
    client = _client(session)

    response = client.post(
        f"/api/v1/admin/tenants/tenant-a/api-keys/{active['id']}/revoke",
        headers={"X-Admin-Secret": "test-admin-secret"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["revoked"] is True
    assert body["key"]["status"] == "revoked"
    assert session.api_keys[0]["revoked_at"] is not None
    assert session.audit_events[0]["event_type"] == "revoke"
    assert session.audit_events[0]["api_key_id"] == active["id"]


def test_revoke_tenant_api_key_404s_for_tenant_mismatch() -> None:
    key = _api_key_row(tenant_id="tenant-b", description="Other tenant")
    session = FakeSession([key])
    client = _client(session)

    response = client.post(
        f"/api/v1/admin/tenants/tenant-a/api-keys/{key['id']}/revoke",
        headers={"X-Admin-Secret": "test-admin-secret"},
    )

    assert response.status_code == 404
