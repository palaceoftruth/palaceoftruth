import asyncio
import uuid
from types import SimpleNamespace

import pytest

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


class _DetailedFakeLLM:
    def __init__(self, *, relationship="related_to", confidence=0.9, outcome="valid") -> None:
        self.relationship = relationship
        self.confidence = confidence
        self.outcome = outcome

    async def classify_relationship_detailed(self, *_args):
        return SimpleNamespace(
            relationship=self.relationship,
            confidence=self.confidence,
            provider="openrouter",
            retry_provider="openrouter",
            validation_outcome=self.outcome,
            fallback_used=False,
            retry_count=0,
        )


class _ExactPairDB:
    def __init__(self, source, target) -> None:
        self.items = {source.id: source, target.id: target}
        self.execute_calls: list[tuple[str, dict | None]] = []
        self.committed = False

    async def get(self, _model, key):
        return self.items.get(key)

    async def execute(self, statement, params=None):
        self.execute_calls.append((str(statement), params))
        return _FakeResult(scalar_value=1)

    async def commit(self) -> None:
        self.committed = True


def test_relationship_extraction_scopes_queries_to_item_tenant() -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
        content_hash="source-content-hash",
        metadata_={},
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
    marker = item.metadata_["_palace_relationship_extraction"]
    assert marker["version"] == "1"
    assert marker["content_hash"] == "source-content-hash"
    assert marker["candidate_count"] == 1
    assert db.committed is True


def test_relationship_extraction_skips_insert_when_endpoint_disappears() -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
        content_hash="source-content-hash",
        metadata_={},
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
        content_hash="source-content-hash",
        metadata_={},
    )
    db = _FakeDB(item)
    service = RelationshipService(db, embedder=object(), llm=_FakeLLM())

    asyncio.run(service.find_relationships(item_id, tenant_id="tenant-a"))

    snapshot = relationship_telemetry_snapshot()
    assert snapshot["extractions"] == [(("unknown", "valid", "false"), 1)]
    assert snapshot["edges"] == [(("unknown",), 1)]
    assert snapshot["retries"] == [(("unknown",), 0)]


def test_relationship_extraction_marks_successful_no_match_attempt() -> None:
    item_id = uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
        content_hash="source-content-hash",
        metadata_={"memory_entry": {"scope_type": "workspace"}},
    )
    db = _FakeDB(item)
    service = RelationshipService(
        db,
        embedder=object(),
        llm=_DetailedFakeLLM(relationship="none", confidence=0.0, outcome="empty"),
    )

    asyncio.run(service.find_relationships(item_id, tenant_id="tenant-a"))

    assert len(db.execute_calls) == 2
    assert item.metadata_["memory_entry"] == {"scope_type": "workspace"}
    marker = item.metadata_["_palace_relationship_extraction"]
    assert marker["version"] == "1"
    assert marker["content_hash"] == "source-content-hash"
    assert marker["candidate_count"] == 1
    assert marker["completed_at"].endswith("+00:00")
    assert db.committed is True


def test_exact_candidate_classification_persists_only_an_allowed_ready_pair() -> None:
    reset_relationship_telemetry_for_tests()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        title="Source",
        summary="Source summary",
        tenant_id="sar-1083-canary",
        status="ready",
        deleted_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        title="Target",
        summary="Target summary",
        tenant_id="sar-1083-canary",
        status="ready",
        deleted_at=None,
    )
    db = _ExactPairDB(source, target)
    service = RelationshipService(db, embedder=object(), llm=_DetailedFakeLLM())

    result = asyncio.run(
        service.classify_candidate(
            source.id,
            target.id,
            tenant_id="sar-1083-canary",
            allowed_relationships={"related_to", "expands_on", "example_of"},
        )
    )

    assert result.relationship == "related_to"
    assert result.edge_persisted is True
    assert len(db.execute_calls) == 1
    assert db.execute_calls[0][1]["source"] == str(source.id)
    assert db.execute_calls[0][1]["target"] == str(target.id)
    assert db.committed is True


def test_exact_candidate_observation_never_persists_an_edge() -> None:
    reset_relationship_telemetry_for_tests()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        title="Source",
        summary="Source summary",
        tenant_id="sar-1083-canary",
        status="ready",
        deleted_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        title="Target",
        summary="Target summary",
        tenant_id="sar-1083-canary",
        status="ready",
        deleted_at=None,
    )
    db = _ExactPairDB(source, target)
    service = RelationshipService(
        db,
        embedder=object(),
        llm=_DetailedFakeLLM(relationship="none", confidence=0.0, outcome="empty"),
    )

    result = asyncio.run(
        service.classify_candidate(
            source.id,
            target.id,
            tenant_id="sar-1083-canary",
            persist=False,
        )
    )

    assert result.validation_outcome == "empty"
    assert result.edge_persisted is False
    assert db.execute_calls == []
    assert db.committed is True


@pytest.mark.parametrize(
    ("confidence", "expected_persisted"),
    [(0.69, False), (0.7, True)],
)
def test_extraction_threshold_is_configurable_at_boundary(
    monkeypatch,
    confidence: float,
    expected_persisted: bool,
) -> None:
    source = SimpleNamespace(
        id=uuid.uuid4(),
        title="Source",
        summary="Source summary",
        tenant_id="tenant-a",
        status="ready",
        deleted_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        title="Target",
        summary="Target summary",
        tenant_id="tenant-a",
        status="ready",
        deleted_at=None,
    )
    db = _ExactPairDB(source, target)
    service = RelationshipService(
        db,
        embedder=object(),
        llm=_DetailedFakeLLM(relationship="related_to", confidence=confidence),
    )
    monkeypatch.setattr(
        "app.services.relationships.settings.relationship_extraction_min_confidence",
        0.7,
    )

    result = asyncio.run(
        service.classify_candidate(
            source.id,
            target.id,
            tenant_id="tenant-a",
        )
    )

    assert result.relationship == "related_to"
    assert result.confidence == confidence
    assert result.validation_outcome == "valid"
    assert result.persistence_min_confidence == 0.7
    assert result.persistence_threshold_rejected is (not expected_persisted)
    assert result.edge_persisted is expected_persisted
    assert len(db.execute_calls) == int(expected_persisted)
