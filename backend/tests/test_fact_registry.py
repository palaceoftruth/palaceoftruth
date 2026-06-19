import asyncio
import uuid
from datetime import datetime, timezone

from app.models.item import Item
from app.models.palace import TemporalFact
from app.services.fact_registry import extract_fact_candidates, extract_temporal_facts, sweep_fact_registry_contradictions


class _ScalarRows:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _TupleRows:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeSession:
    def __init__(self, items, facts=None):
        self.items = list(items)
        self.facts = list(facts or [])
        self.commits = 0
        self.added = []

    async def execute(self, statement):
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if "FROM items" in compiled:
            return _ScalarRows(self.items)
        if "FROM temporal_facts" in compiled:
            if "JOIN items" in compiled:
                return _TupleRows([(fact, next(item.title for item in self.items if item.id == fact.source_item_id)) for fact in self.facts])
            return _ScalarRows(self.facts)
        raise AssertionError(f"unexpected query: {compiled}")

    def add(self, value):
        if isinstance(value, TemporalFact):
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()
            self.facts.append(value)
            self.added.append(value)

    async def commit(self):
        self.commits += 1


def _item(*, title: str, raw_content: str, metadata=None, updated_at=None) -> Item:
    timestamp = updated_at or datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    return Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        title=title,
        summary=None,
        raw_content=raw_content,
        metadata_=metadata or {},
        status="ready",
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_extract_fact_candidates_supports_explicit_lines_and_metadata() -> None:
    item = _item(
        title="Launch brief",
        raw_content="Fact: Launch plan | targets | May 2026 rollout | 2026-05-01\n- Fact: HQ | located_in | New York",
        metadata={
            "fact_registry": {
                "facts": [
                    {
                        "subject": "Roadmap",
                        "predicate": "owner",
                        "object": "Product",
                        "valid_from": "2026-04-01",
                    }
                ]
            }
        },
    )

    candidates = extract_fact_candidates(item)

    assert {(candidate.subject, candidate.predicate, candidate.object_text) for candidate in candidates} == {
        ("Launch plan", "targets", "May 2026 rollout"),
        ("HQ", "located_in", "New York"),
        ("Roadmap", "owner", "Product"),
    }


def test_extract_temporal_facts_creates_and_supersedes_rows() -> None:
    source_item = _item(
        title="Launch brief",
        raw_content="Fact: Launch plan | targets | May 2026 rollout",
    )
    stale_fact = TemporalFact(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_item_id=source_item.id,
        fact_key="obsolete",
        source_fingerprint="old",
        subject="Old plan",
        predicate="targets",
        object_text="April 2026 rollout",
        confidence=1.0,
        status="active",
        metadata_json={},
        extracted_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    session = FakeSession([source_item], facts=[stale_fact])

    result = asyncio.run(extract_temporal_facts(session, tenant_id="tenant-a"))

    assert result.items_scanned == 1
    assert result.created == 1
    assert result.superseded == 1
    assert len(session.added) == 1
    assert stale_fact.status == "superseded"
    assert session.commits == 1


def test_extract_temporal_facts_is_idempotent_for_unchanged_source() -> None:
    source_item = _item(
        title="Launch brief",
        raw_content="Fact: Launch plan | targets | May 2026 rollout",
    )
    seed_session = FakeSession([source_item])
    first = asyncio.run(extract_temporal_facts(seed_session, tenant_id="tenant-a"))

    session = FakeSession([source_item], facts=list(seed_session.facts))
    second = asyncio.run(extract_temporal_facts(session, tenant_id="tenant-a"))

    assert first.created == 1
    assert second.created == 0
    assert second.updated == 0
    assert second.superseded == 0
    assert second.unchanged == 1


def test_sweep_fact_registry_contradictions_flags_overlapping_conflicts() -> None:
    first_item = _item(title="Strategy A", raw_content="")
    second_item = _item(title="Strategy B", raw_content="")
    first_fact = TemporalFact(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_item_id=first_item.id,
        fact_key="first",
        source_fingerprint="source-a",
        subject="Roadmap",
        predicate="targets",
        object_text="May 2026 rollout",
        confidence=1.0,
        valid_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        valid_to=datetime(2026, 5, 31, tzinfo=timezone.utc),
        status="active",
        metadata_json={},
        extracted_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
    )
    second_fact = TemporalFact(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_item_id=second_item.id,
        fact_key="second",
        source_fingerprint="source-b",
        subject="roadmap",
        predicate="targets",
        object_text="June 2026 rollout",
        confidence=1.0,
        valid_from=datetime(2026, 5, 15, tzinfo=timezone.utc),
        valid_to=None,
        status="active",
        metadata_json={},
        extracted_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
    )
    session = FakeSession([first_item, second_item], facts=[first_fact, second_fact])

    result = asyncio.run(sweep_fact_registry_contradictions(session, tenant_id="tenant-a"))

    assert result.facts_scanned == 2
    assert result.contradictions == 1
    assert result.facts_flagged == 2
    assert result.facts_cleared == 0
    assert first_fact.metadata_json["contradiction_sweep"]["conflicting_fact_ids"] == [str(second_fact.id)]
    assert second_fact.metadata_json["contradiction_sweep"]["conflicting_fact_ids"] == [str(first_fact.id)]
    assert session.commits == 1


def test_sweep_fact_registry_contradictions_clears_stale_flags_when_windows_no_longer_overlap() -> None:
    first_item = _item(title="Strategy A", raw_content="")
    second_item = _item(title="Strategy B", raw_content="")
    first_fact = TemporalFact(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_item_id=first_item.id,
        fact_key="first",
        source_fingerprint="source-a",
        subject="Roadmap",
        predicate="targets",
        object_text="May 2026 rollout",
        confidence=1.0,
        valid_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        valid_to=datetime(2026, 5, 31, tzinfo=timezone.utc),
        status="active",
        metadata_json={"contradiction_sweep": {"conflict_count": 1, "conflicting_fact_ids": ["stale"]}},
        extracted_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
    )
    second_fact = TemporalFact(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_item_id=second_item.id,
        fact_key="second",
        source_fingerprint="source-b",
        subject="Roadmap",
        predicate="targets",
        object_text="June 2026 rollout",
        confidence=1.0,
        valid_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        valid_to=None,
        status="active",
        metadata_json={},
        extracted_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
    )
    session = FakeSession([first_item, second_item], facts=[first_fact, second_fact])

    result = asyncio.run(sweep_fact_registry_contradictions(session, tenant_id="tenant-a"))

    assert result.contradictions == 0
    assert result.facts_flagged == 0
    assert result.facts_cleared == 1
    assert "contradiction_sweep" not in first_fact.metadata_json
