import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import router
from app.database import get_db


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class _Session:
    def __init__(self, clients):
        self.clients = clients
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        row = next(
            (
                client
                for client in self.clients
                if client["tenant_id"] == params.get("tenant_id")
                and client["id"] == params.get("client_id")
            ),
            None,
        )
        if sql.lstrip().startswith("select"):
            return _Result([row] if row else [])
        if sql.lstrip().startswith("update"):
            if row is None:
                return _Result([])
            if params["enabled"] and "memory:promote_shared" not in row["allowed_scopes"]:
                row["allowed_scopes"].append("memory:promote_shared")
            if not params["enabled"]:
                row["allowed_scopes"] = [
                    scope for scope in row["allowed_scopes"] if scope != "memory:promote_shared"
                ]
            return _Result([row])
        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self):
        self.commits += 1


def _client(*, tenant_id="tenant-a", scopes=None, agent_scope_key="agent/codex", containment_mode="hermes_agent"):
    return {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "client_key": "codex",
        "display_name": "Codex",
        "allowed_scopes": list(scopes or ["read", "write", "write:agent", "admin"]),
        "metadata": {"owner": "test"},
        "agent_scope_key": agent_scope_key,
        "allow_all_agent_scope_reads": False,
        "allow_tenant_shared_reads": False,
        "allow_workspace_scope_reads": False,
        "containment_mode": containment_mode,
        "client_type": "service",
        "oauth_client_id": None,
        "redirect_uris": [],
        "allowed_resources": [],
        "authorization_code_enabled": False,
        "token_endpoint_auth_method": "client_secret_basic",
        "oauth_revoked_at": None,
        "oauth_token_ttl_seconds": 3600,
        "created_at": datetime.now(timezone.utc),
        "last_seen_at": None,
    }


def _app(session):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_operator_can_enable_disable_and_repeat_without_rotating_or_overwriting_grants():
    session = _Session([_client()])
    client = _app(session)
    url = f"/api/v1/admin/tenants/tenant-a/mcp-clients/{session.clients[0]['id']}/shared-memory-promotion"
    headers = {"X-Admin-Secret": "test-admin-secret"}

    enabled = client.patch(url, headers=headers, json={"enabled": True})
    assert enabled.status_code == 200
    assert enabled.json()["allowed_scopes"] == ["read", "write", "write:agent", "admin", "memory:promote_shared"]
    assert session.clients[0]["metadata"] == {"owner": "test"}

    repeated = client.patch(url, headers=headers, json={"enabled": True})
    assert repeated.status_code == 200
    assert repeated.json()["allowed_scopes"].count("memory:promote_shared") == 1

    disabled = client.patch(url, headers=headers, json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["allowed_scopes"] == ["read", "write", "write:agent", "admin"]
    assert session.commits == 3


def test_operator_enable_requires_canonical_binding_and_existing_write_grants():
    headers = {"X-Admin-Secret": "test-admin-secret"}
    for kwargs in (
        {"agent_scope_key": None, "containment_mode": "hermes_agent"},
        {"containment_mode": "standard"},
        {"scopes": ["read", "write:agent"]},
    ):
        session = _Session([_client(**kwargs)])
        client = _app(session)
        url = f"/api/v1/admin/tenants/tenant-a/mcp-clients/{session.clients[0]['id']}/shared-memory-promotion"
        response = client.patch(url, headers=headers, json={"enabled": True})
        assert response.status_code == 409
        assert "canonical bound agent" in response.json()["detail"]
        assert "memory:promote_shared" not in session.clients[0]["allowed_scopes"]


def test_operator_activation_is_tenant_scoped_and_requires_admin_authentication():
    session = _Session([_client()])
    client = _app(session)
    client_id = session.clients[0]["id"]
    url = f"/api/v1/admin/tenants/tenant-b/mcp-clients/{client_id}/shared-memory-promotion"

    assert client.patch(url, json={"enabled": True}).status_code == 403
    missing = client.patch(
        f"/api/v1/admin/tenants/tenant-a/mcp-clients/{uuid.uuid4()}/shared-memory-promotion",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={"enabled": True},
    )
    assert missing.status_code == 404
    assert client.patch(
        f"/api/v1/admin/tenants/tenant-a/mcp-clients/{client_id}/shared-memory-promotion",
        headers={"X-Admin-Secret": "test-admin-secret"},
        json={"enabled": True, "unexpected": True},
    ).status_code == 422
