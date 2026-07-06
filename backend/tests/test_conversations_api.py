import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.conversations import router
from app.auth import AuthContext, verify_memory_auth
from app.database import get_db


class MappingResult:
    def __init__(self, *, rows=None, row=None) -> None:
        self.rows = rows or []
        self.row = row

    def mappings(self):
        return self

    def one(self):
        if self.row is None:
            raise AssertionError("Expected row to exist")
        return self.row

    def one_or_none(self):
        return self.row

    def all(self):
        return self.rows


class RowCountResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class StatefulConversationSession:
    def __init__(self, *, tenant_id: str = "tenant-a") -> None:
        self.tenant_id = tenant_id
        self.conversations: dict[uuid.UUID, dict] = {}
        self.messages: dict[uuid.UUID, list[dict]] = {}
        self.commits = 0
        self.message_query_scoped = False

    async def execute(self, statement, params=None):
        sql = str(statement)

        if "INSERT INTO conversations" in sql:
            conv_id = uuid.uuid4()
            now = datetime.now(timezone.utc)
            row = {
                "id": conv_id,
                "title": params["title"],
                "tenant_id": params["tenant_id"],
                "created_at": now,
                "updated_at": now,
            }
            self.conversations[conv_id] = row
            self.messages[conv_id] = []
            return MappingResult(
                row={
                    "id": conv_id,
                    "title": row["title"],
                    "created_at": now,
                    "updated_at": now,
                }
            )

        if "ORDER BY updated_at DESC" in sql:
            rows = sorted(
                (
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in self.conversations.values()
                    if row["tenant_id"] == params["tenant_id"]
                ),
                key=lambda row: row["updated_at"],
                reverse=True,
            )
            return MappingResult(rows=rows)

        if "DELETE FROM conversations" in sql:
            row = self.conversations.get(params["id"])
            if row is None or row["tenant_id"] != params["tenant_id"]:
                return RowCountResult(0)
            del self.conversations[params["id"]]
            self.messages.pop(params["id"], None)
            return RowCountResult(1)

        if "FROM conversations" in sql and "WHERE id = :id AND tenant_id = :tenant_id" in sql and "RETURNING" not in sql:
            row = self.conversations.get(params["id"])
            if row is None or row["tenant_id"] != params["tenant_id"]:
                return MappingResult(row=None)
            return MappingResult(
                row={
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )

        if "FROM conversation_messages" in sql:
            assert "tenant_id = :tenant_id" in sql
            self.message_query_scoped = True
            row = self.conversations.get(params["id"])
            if row is None or row["tenant_id"] != params["tenant_id"]:
                return MappingResult(rows=[])
            return MappingResult(rows=list(self.messages.get(params["id"], [])))

        if "INSERT INTO conversation_messages" in sql:
            msg_id = uuid.uuid4()
            created_at = datetime.now(timezone.utc)
            self.messages[params["conv_id"]].append(
                {
                    "id": msg_id,
                    "conversation_id": params["conv_id"],
                    "role": params["role"],
                    "content": params["content"],
                    "created_at": created_at,
                }
            )
            return MappingResult(rows=[])

        if "UPDATE conversations SET updated_at = now()" in sql and "RETURNING" not in sql:
            row = self.conversations.get(params["id"])
            if row is not None and row["tenant_id"] == params["tenant_id"]:
                row["updated_at"] = datetime.now(timezone.utc)
            return MappingResult(rows=[])

        if "UPDATE conversations SET title = :title, updated_at = now()" in sql:
            row = self.conversations.get(params["id"])
            if row is None or row["tenant_id"] != params["tenant_id"]:
                return MappingResult(row=None)
            row["title"] = params["title"]
            row["updated_at"] = datetime.now(timezone.utc)
            return MappingResult(
                row={
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )

        raise AssertionError(f"Unexpected SQL: {sql}")

    async def commit(self) -> None:
        self.commits += 1


class NeverCalledSession:
    async def execute(self, statement, params=None):
        raise AssertionError("DB execute should not run for request validation failures")

    async def commit(self) -> None:
        raise AssertionError("DB commit should not run for request validation failures")


def _client(session, *, tenant_id: str = "tenant-a") -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield session

    async def override_verify(request: Request):
        request.state.auth_context = AuthContext(tenant_id=tenant_id, auth_mode="api_key", token_hash_reference="key-hash")
        request.state.tenant_id = tenant_id
        request.state.key_hash = "key-hash"
        request.state.auth_mode = "api_key"
        return "raw-key"

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[verify_memory_auth] = override_verify
    return TestClient(app)


def test_conversation_crud_round_trip_and_tenant_scoped_messages() -> None:
    session = StatefulConversationSession()
    client = _client(session)

    create = client.post("/api/v1/conversations", json={"title": "  Launch War Room  "})
    assert create.status_code == 201
    created = create.json()
    conv_id = created["id"]
    assert created["title"] == "Launch War Room"

    append = client.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={
            "messages": [
                {"role": "user", "content": "  What changed?  "},
                {"role": "assistant", "content": "  The palace indexed the launch brief.  "},
            ]
        },
    )
    assert append.status_code == 200
    appended = append.json()
    assert [message["content"] for message in appended["messages"]] == [
        "What changed?",
        "The palace indexed the launch brief.",
    ]

    update = client.patch(f"/api/v1/conversations/{conv_id}", json={"title": "  Palace Control Tower  "})
    assert update.status_code == 200
    assert update.json()["title"] == "Palace Control Tower"

    listed = client.get("/api/v1/conversations")
    assert listed.status_code == 200
    assert listed.json()[0]["title"] == "Palace Control Tower"

    fetched = client.get(f"/api/v1/conversations/{conv_id}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Palace Control Tower"
    assert session.message_query_scoped is True

    deleted = client.delete(f"/api/v1/conversations/{conv_id}")
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/conversations/{conv_id}").status_code == 404


def test_conversation_validation_rejects_blank_titles_and_invalid_messages() -> None:
    client = _client(NeverCalledSession())

    create = client.post("/api/v1/conversations", json={"title": "   "})
    assert create.status_code == 422

    update = client.patch(f"/api/v1/conversations/{uuid.uuid4()}", json={"title": "   "})
    assert update.status_code == 422

    empty_messages = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"messages": []},
    )
    assert empty_messages.status_code == 422

    invalid_role = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"messages": [{"role": "system", "content": "Nope"}]},
    )
    assert invalid_role.status_code == 422

    blank_content = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"messages": [{"role": "user", "content": "   "}]},
    )
    assert blank_content.status_code == 422


def test_append_messages_returns_404_for_missing_conversation() -> None:
    client = _client(StatefulConversationSession())

    response = client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/messages",
        json={"messages": [{"role": "user", "content": "Status?"}]},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Conversation not found"}
