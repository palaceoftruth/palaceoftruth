import asyncio
import uuid
from types import SimpleNamespace

from app.services.relationships import RelationshipService
from app.services.relationship_telemetry import (
    relationship_telemetry_snapshot,
    reset_relationship_telemetry_for_tests,
)


class _FakeResult:
    def __init__(self, *, scalar_value=None, rows=None) -> None:
        self._scalar_value = scalar_value
        self._rows = rows or []

    def scalar_one(self):
        return self._scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def fetchall(self):
        return self._rows


class _FakeDB:
    def __init__(self, item, *, insert_scalar_value=1) -> None:
        self.item = item
        self.insert_scalar_value = insert_scalar_value
        self.execute_calls: list[tuple[str, dict | None]] = []
        self.committed = False

    async def execute(self, statement, params=None):
        self.execute_calls.append((str(statement), params))
        call_number = len(self.execute_calls)
        if call_number == 1:
            return _FakeResult(scalar_value=2)
        if call_number == 2:
            return _FakeResult(
                rows=[
                    SimpleNamespace(
                        id=uuid.uuid4(),
                        title="Candidate",
                        summary="Candidate summary",
                    )
                ]
            )
        return _FakeResult(scalar_value=self.insert_scalar_value)

    async def get(self, _model, _key):
        return self.item

    async def commit(self) -> None:
        self.committed = True


class _FakeLLM:
    async def classify_relationship(self, *_args):
        return ("related_to", 0.9)


def test_relationship_extraction_scopes_queries_to_item_tenant() -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
    )
    db = _FakeDB(item)
    service = RelationshipService(db, embedder=object(), llm=_FakeLLM())

    asyncio.run(service.find_relationships(item_id))

    assert db.execute_calls[0][1] == {"tenant_id": "tenant-a"}
    assert db.execute_calls[1][1] == {
        "item_id": str(item_id),
        "limit": 5,
        "tenant_id": "tenant-a",
        "embedding_profile_name": None,
        "embedding_dimensions": 1536,
    }
    upsert_sql, upsert_params = db.execute_calls[2]
    assert "WITH endpoints AS" in upsert_sql
    assert "FOR KEY SHARE OF src, dst" in upsert_sql
    assert "RETURNING 1" in upsert_sql
    assert upsert_params["source"] == str(item_id)
    assert upsert_params["tenant_id"] == "tenant-a"
    assert db.committed is True


def test_relationship_extraction_skips_insert_when_endpoint_disappears() -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
    )
    db = _FakeDB(item, insert_scalar_value=None)
    service = RelationshipService(db, embedder=object(), llm=_FakeLLM())

    asyncio.run(service.find_relationships(item_id, tenant_id="tenant-a"))

    assert len(db.execute_calls) == 3
    assert db.committed is True


def test_relationship_extraction_records_bounded_telemetry() -> None:
    reset_relationship_telemetry_for_tests()
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
    )
    db = _FakeDB(item)
    service = RelationshipService(db, embedder=object(), llm=_FakeLLM())

    asyncio.run(service.find_relationships(item_id, tenant_id="tenant-a"))

    snapshot = relationship_telemetry_snapshot()
    assert snapshot["extractions"] == [(("unknown", "valid", "false"), 1)]
    assert snapshot["edges"] == [(("unknown",), 1)]
    assert snapshot["retries"] == [(("unknown",), 0)]
