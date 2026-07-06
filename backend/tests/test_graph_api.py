import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.api.graph import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db
from app.models.item import Item
from app.models.relationship import ItemRelationship
from app.services.graph_telemetry import ready_tenant_relationships_query


class _ListResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class GraphSession:
    def __init__(self, items, relationships, *, item_results=None) -> None:
        self.items = items
        self.item_results = list(item_results or [])
        self.relationships = relationships
        self.execute_sql: list[str] = []

    async def execute(self, statement):
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        self.execute_sql.append(sql)
        if "FROM item_relationships" in sql:
            return _ListResult(self.relationships)
        if "FROM items" in sql:
            if self.item_results:
                return _ListResult(self.item_results.pop(0))
            return _ListResult(self.items)
        raise AssertionError(f"Unexpected SQL: {sql}")


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
        self.updates: list[dict] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement).lower()
        params = params or {}
        if "from mcp_oauth_access_tokens" in sql:
            return _AuthResult(self.row)
        if "insert into mcp_request_audit_events" in sql:
            return _AuthResult(None)
        if "update mcp_oauth_access_tokens" in sql or "update mcp_clients" in sql:
            self.updates.append(params)
            return _AuthResult(None)
        raise AssertionError(f"Unexpected auth SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


def _oauth_token_row(*, scopes: list[str], resource: str | None = "https://testserver/api/v1"):
    return {
        "token_id": uuid.uuid4(),
        "tenant_id": "tenant-a",
        "token_scopes": scopes,
        "token_resource": resource,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "token_revoked_at": None,
        "client_id": uuid.uuid4(),
        "client_key": "codex-remote",
        "allowed_scopes": ["read", "write", "admin"],
        "client_revoked_at": None,
    }


def _item(item_id: uuid.UUID, title: str, *, status: str = "ready", tenant_id: str = "tenant-a") -> Item:
    return Item(
        id=item_id,
        source_type="note",
        title=title,
        tenant_id=tenant_id,
        status=status,
        tags=[],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _client(session: GraphSession) -> TestClient:
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


def _oauth_client(monkeypatch, session: GraphSession, auth_session: AuthSession) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield session

    monkeypatch.setattr(auth, "async_session", lambda: auth_session)
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_graph_response_surfaces_orphaned_ready_item_count() -> None:
    linked_source_id = uuid.uuid4()
    linked_target_id = uuid.uuid4()
    orphaned_id = uuid.uuid4()
    session = GraphSession(
        [
            _item(linked_source_id, "Linked source"),
            _item(linked_target_id, "Linked target"),
            _item(orphaned_id, "Unlinked memory object"),
        ],
        [
            ItemRelationship(
                id=uuid.uuid4(),
                source_item_id=linked_source_id,
                target_item_id=linked_target_id,
                relationship="related_to",
                confidence=0.9,
            )
        ],
    )
    client = _client(session)

    response = client.get("/api/v1/graph")

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"] == {"orphaned_ready_items": 1}
    assert [node["title"] for node in payload["nodes"]] == [
        "Linked source",
        "Linked target",
        "Unlinked memory object",
    ]
    assert payload["edges"] == [
        {
            "source": str(linked_source_id),
            "target": str(linked_target_id),
            "relationship": "related_to",
            "confidence": 0.9,
        }
    ]


def test_graph_accepts_oauth_bearer_read_scope(monkeypatch) -> None:
    item_id = uuid.uuid4()
    session = GraphSession([_item(item_id, "OAuth-visible node")], [])
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["read"])))

    response = client.get("/api/v1/graph", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 200
    assert response.json()["nodes"][0]["title"] == "OAuth-visible node"
    assert session.execute_sql


def test_graph_rejects_oauth_bearer_missing_read_scope(monkeypatch) -> None:
    session = GraphSession([_item(uuid.uuid4(), "Hidden node")], [])
    client = _oauth_client(monkeypatch, session, AuthSession(_oauth_token_row(scopes=["write"])))

    response = client.get("/api/v1/graph", headers={"Authorization": "Bearer raw-token"})

    assert response.status_code == 403
    assert response.json()["detail"] == "MCP bearer token missing read scope"
    assert session.execute_sql == []


def test_graph_response_honors_include_orphans_false() -> None:
    linked_source_id = uuid.uuid4()
    linked_target_id = uuid.uuid4()
    orphaned_id = uuid.uuid4()
    session = GraphSession(
        [
            _item(linked_source_id, "Linked source"),
            _item(linked_target_id, "Linked target"),
            _item(orphaned_id, "Unlinked memory object"),
        ],
        [
            ItemRelationship(
                id=uuid.uuid4(),
                source_item_id=linked_source_id,
                target_item_id=linked_target_id,
                relationship="related_to",
                confidence=0.9,
            )
        ],
    )
    client = _client(session)

    response = client.get("/api/v1/graph?include_orphans=false")

    assert response.status_code == 200
    payload = response.json()
    assert [node["id"] for node in payload["nodes"]] == [
        str(linked_source_id),
        str(linked_target_id),
    ]
    assert payload["meta"] == {"orphaned_ready_items": 1}


def test_graph_response_supports_bounded_focused_neighborhood() -> None:
    origin_id = uuid.uuid4()
    related_id = uuid.uuid4()
    session = GraphSession(
        [],
        [
            ItemRelationship(
                id=uuid.uuid4(),
                source_item_id=origin_id,
                target_item_id=related_id,
                relationship="expands_on",
                confidence=0.87,
            )
        ],
        item_results=[
            [_item(origin_id, "Origin")],
            [_item(related_id, "Related")],
        ],
    )
    client = _client(session)

    response = client.get(f"/api/v1/graph?item_id={origin_id}&node_limit=5&edge_limit=7")

    assert response.status_code == 200
    payload = response.json()
    assert [node["id"] for node in payload["nodes"]] == [str(origin_id), str(related_id)]
    assert payload["edges"][0]["target"] == str(related_id)
    assert "LIMIT 5" in session.execute_sql[0]
    assert "LIMIT 7" in session.execute_sql[1]


def test_relationship_query_excludes_failed_and_cross_tenant_endpoints() -> None:
    sql = str(
        ready_tenant_relationships_query("tenant-a").compile(
            compile_kwargs={"literal_binds": True}
        )
    ).lower()

    assert "from item_relationships" in sql
    assert sql.count("tenant_id = 'tenant-a'") == 2
    assert sql.count("status = 'ready'") == 2
