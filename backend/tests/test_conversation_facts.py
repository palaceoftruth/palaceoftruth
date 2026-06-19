import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from app.models.item import Item
from app.services.conversation_facts import (
    CONVERSATION_FACT_SOURCE,
    backfill_conversation_facts,
    build_conversation_fact_entries,
    parse_conversation_turns,
)
from app.services.memory_entries import normalize_memory_entry


def _memory_item(*, raw_content: str, scope_type: str = "agent", scope_key: str | None = "codex") -> Item:
    now = datetime(2026, 5, 26, tzinfo=timezone.utc)
    item = Item(
        id=uuid.uuid4(),
        source_type="note",
        source_url="memory://session/demo",
        title="Session transcript",
        summary=None,
        raw_content=raw_content,
        content_chunks=[{"index": 0, "text": raw_content}],
        metadata_={
            "memory_entry": {
                "source": "codex-session",
                "scope": {"type": scope_type, "key": scope_key} if scope_key else {"type": scope_type},
            }
        },
        tags=["scope-agent", "agent-codex"],
        categories=[],
        tenant_id="default",
        status="ready",
        created_at=now,
        updated_at=now,
    )
    return item


def test_parse_conversation_turns_keeps_line_and_turn_offsets() -> None:
    turns = parse_conversation_turns(
        "**Andrew** (2026-05-26 9:03 PM): ship the palace plan\n"
        "with source spans\n"
        "**Codex** (2026-05-26 9:04 PM): acknowledged\n"
    )

    assert len(turns) == 2
    assert turns[0].speaker == "Andrew"
    assert turns[0].text == "ship the palace plan with source spans"
    assert turns[0].timestamp == "2026-05-26T21:03:00Z"
    assert turns[0].turn_index == 0
    assert turns[0].line_start == 1
    assert turns[0].line_end == 2


def test_build_conversation_fact_entries_preserves_scope_and_source_span() -> None:
    item = _memory_item(
        raw_content="User: latest status is PR ready\nAssistant: I will verify checks",
        scope_type="workspace",
        scope_key="palaceoftruth",
    )

    entries = build_conversation_fact_entries(item)

    assert len(entries) == 2
    entry = entries[0]
    assert entry.source == CONVERSATION_FACT_SOURCE
    assert entry.scope.type == "workspace"
    assert entry.scope.key == "palaceoftruth"
    assert entry.relationship_policy == "skip"
    assert entry.metadata is not None
    normalized = normalize_memory_entry(entry)
    fact_metadata = normalized.metadata["memory_entry"]["metadata"]["conversation_fact"]
    assert fact_metadata["advisory"] is True
    assert fact_metadata["source_item_id"] == str(item.id)
    assert fact_metadata["source_span"]["chunk_index"] == 0
    assert fact_metadata["source_span"]["line_start"] == 1
    assert fact_metadata["source_span"]["turn_index"] == 0


def test_build_conversation_fact_entries_is_idempotent_for_same_source_turn() -> None:
    item = _memory_item(raw_content="User: lock code is 9494")

    first = build_conversation_fact_entries(item)[0]
    item.updated_at = datetime(2026, 5, 27, tzinfo=timezone.utc)
    second = build_conversation_fact_entries(item)[0]

    assert first.idempotency_key == second.idempotency_key
    assert first.metadata["conversation_fact"]["source_fingerprint"] == second.metadata["conversation_fact"]["source_fingerprint"]


class _FakeScalarResult:
    def __init__(self, values) -> None:
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _FakeDb:
    def __init__(self, values) -> None:
        self.values = values
        self.rollback_count = 0

    async def execute(self, *_args, **_kwargs):
        return _FakeScalarResult(self.values)

    async def rollback(self):
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_backfill_conversation_facts_dry_run_filters_non_memory_items() -> None:
    memory_item = _memory_item(raw_content="User: remember the source-linked fact")
    plain_item = _memory_item(raw_content="User: should not be considered")
    plain_item.metadata_ = {}
    derived_item = _memory_item(raw_content="User: already derived")
    derived_item.metadata_["memory_entry"]["source"] = CONVERSATION_FACT_SOURCE
    derived_item.metadata_["memory_entry"]["metadata"] = {"conversation_fact": {"advisory": True}}

    result = await backfill_conversation_facts(
        _FakeDb([memory_item, plain_item, derived_item]),
        tenant_id="default",
        dry_run=True,
    )

    assert result.items_scanned == 3
    assert result.items_completed == 1
    assert result.items_skipped == 2
    assert result.items_failed == 0
    assert result.facts_discovered == 1
    assert result.facts_submitted == 0


@pytest.mark.asyncio
async def test_backfill_conversation_facts_clamps_workers_for_dry_run() -> None:
    items = [
        _memory_item(raw_content="User: first"),
        _memory_item(raw_content="User: second"),
    ]

    result = await backfill_conversation_facts(
        _FakeDb(items),
        tenant_id="default",
        dry_run=True,
        workers=99,
    )

    assert result.worker_count == 2
    assert result.items_completed == 2
    assert result.facts_discovered == 2


@pytest.mark.asyncio
async def test_backfill_conversation_facts_reports_partial_build_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    good_item = _memory_item(raw_content="User: keep this")
    broken_item = _memory_item(raw_content="User: break this")

    original_builder = build_conversation_fact_entries

    def fake_builder(item: Item, *, max_facts_per_item: int = 20):
        if item.id == broken_item.id:
            raise RuntimeError("parser exploded")
        return original_builder(item, max_facts_per_item=max_facts_per_item)

    monkeypatch.setattr("app.services.conversation_facts.build_conversation_fact_entries", fake_builder)

    result = await backfill_conversation_facts(
        _FakeDb([good_item, broken_item]),
        tenant_id="default",
        dry_run=True,
        workers=2,
    )

    assert result.items_completed == 1
    assert result.items_failed == 1
    assert result.facts_discovered == 1
    assert result.failures == (
        {
            "source_item_id": str(broken_item.id),
            "source_item_title": broken_item.title,
            "reason": "build_failed",
            "error": "parser exploded",
        },
    )


@pytest.mark.asyncio
async def test_backfill_conversation_facts_times_out_one_item(monkeypatch: pytest.MonkeyPatch) -> None:
    slow_item = _memory_item(raw_content="User: slow")
    fast_item = _memory_item(raw_content="User: fast")
    original_build_item = __import__(
        "app.services.conversation_facts",
        fromlist=["_build_entries_for_item"],
    )._build_entries_for_item

    async def fake_build_item(item: Item, *, max_facts_per_item: int):
        if item.id == slow_item.id:
            await asyncio.sleep(0.05)
        return await original_build_item(item, max_facts_per_item=max_facts_per_item)

    monkeypatch.setattr("app.services.conversation_facts._build_entries_for_item", fake_build_item)

    result = await backfill_conversation_facts(
        _FakeDb([slow_item, fast_item]),
        tenant_id="default",
        dry_run=True,
        workers=2,
        item_timeout_seconds=0.001,
    )

    assert result.items_completed == 1
    assert result.items_failed == 1
    assert result.failures[0]["source_item_id"] == str(slow_item.id)
    assert result.failures[0]["reason"] == "timeout"


@pytest.mark.asyncio
async def test_backfill_conversation_facts_continues_after_submit_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    first_item = _memory_item(raw_content="User: first\nAssistant: second")
    second_item = _memory_item(raw_content="User: second")
    db = _FakeDb([first_item, second_item])
    accepted_bodies: list[str] = []

    async def fake_accept(_db, *, body, signing_key):
        accepted_bodies.append(body.body)
        if body.body.startswith("Conversation fact\n\nSubject: Assistant\nPredicate: said\nObject: second"):
            raise RuntimeError("queue unavailable")
        return type("Acceptance", (), {"enqueue_requested": True})()

    monkeypatch.setattr("app.services.conversation_facts.accept_canonical_memory_entry", fake_accept)

    result = await backfill_conversation_facts(
        db,
        tenant_id="default",
        dry_run=False,
        workers=2,
    )

    assert db.rollback_count == 1
    assert result.items_completed == 1
    assert result.items_failed == 1
    assert result.facts_discovered == 3
    assert result.facts_submitted == 2
    assert result.facts_queued == 2
    assert len(accepted_bodies) == 3
    assert result.failures[0]["source_item_id"] == str(first_item.id)
    assert result.failures[0]["reason"] == "submit_failed"


@pytest.mark.asyncio
async def test_backfill_conversation_facts_counts_partial_submit_before_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_item = _memory_item(raw_content="User: first\nAssistant: second")
    second_item = _memory_item(raw_content="User: next")
    db = _FakeDb([first_item, second_item])
    accepted_bodies: list[str] = []

    async def fake_accept(_db, *, body, signing_key):
        accepted_bodies.append(body.body)
        if body.body.startswith("Conversation fact\n\nSubject: Assistant\nPredicate: said\nObject: second"):
            await asyncio.sleep(0.05)
        return type("Acceptance", (), {"enqueue_requested": True})()

    monkeypatch.setattr("app.services.conversation_facts.accept_canonical_memory_entry", fake_accept)

    result = await backfill_conversation_facts(
        db,
        tenant_id="default",
        dry_run=False,
        workers=2,
        item_timeout_seconds=0.001,
    )

    assert db.rollback_count == 1
    assert result.items_completed == 1
    assert result.items_failed == 1
    assert result.facts_discovered == 3
    assert result.facts_submitted == 2
    assert result.facts_queued == 2
    assert len(accepted_bodies) == 3
    assert result.failures[0]["source_item_id"] == str(first_item.id)
    assert result.failures[0]["reason"] == "timeout"
