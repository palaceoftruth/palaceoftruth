from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.services.llm import LLMCompletionDiagnostics
from app.services.summary_backfill import (
    MAX_BATCH_SIZE,
    SummaryCandidate,
    _candidate_query,
    _eligible,
    _persist_summary,
    run_summary_backfill,
)


TENANT = "tenant-test"


def _item(**overrides) -> Item:
    values = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "source_type": "media",
        "title": "Captured item",
        "status": "ready",
        "summary": None,
        "raw_content": "Preserved source text for a concise summary.",
        "metadata_": {"kept": {"value": True}},
        "tags": ["keep-tag"],
        "categories": ["keep-category"],
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return Item(**values)


def _snapshot(item: Item) -> SummaryCandidate:
    raw_content = str(item.raw_content)
    return SummaryCandidate(
        item_id=item.id,
        tenant_id=item.tenant_id,
        source_type=item.source_type,
        raw_content=raw_content,
        source_content_hash=hashlib.sha256(raw_content.encode()).hexdigest(),
    )


class _Result:
    def __init__(self, item: Item | None):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


class _Session:
    def __init__(self, item: Item):
        self.item = item
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, _statement):
        return _Result(self.item)

    async def commit(self):
        self.commits += 1


def test_candidate_query_is_tenant_scoped_bounded_and_excludes_memory_items() -> None:
    statement = _candidate_query(
        tenant_id=TENANT,
        source_types=("media", "webpage"),
        limit=25,
        item_ids=(),
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "tenant-test" in sql
    assert "source_type IN ('media', 'webpage')" in sql
    assert "status = 'ready'" in sql
    assert "deleted_at IS NULL" in sql
    assert "LIMIT 25" in sql
    assert "ORDER BY items.created_at ASC, items.id ASC" in sql


def test_eligibility_fails_closed_for_every_out_of_scope_shape() -> None:
    assert _eligible(_item())
    assert _eligible(_item(source_type="webpage"))
    assert not _eligible(_item(source_type="note"))
    assert not _eligible(_item(status="failed"))
    assert not _eligible(_item(summary="already summarized"))
    assert not _eligible(_item(raw_content="  "))
    assert not _eligible(_item(deleted_at=datetime.now(timezone.utc)))


@pytest.mark.asyncio
async def test_dry_run_never_constructs_or_calls_an_llm() -> None:
    candidates = [_snapshot(_item()), _snapshot(_item(source_type="webpage"))]
    calls = []

    async def loader(**_kwargs):
        calls.append("load")
        return candidates

    async def counter(**_kwargs):
        calls.append("count")
        return 71

    report = await run_summary_backfill(
        tenant_id=TENANT,
        limit=25,
        write=False,
        candidate_loader=loader,
        candidate_counter=counter,
        llm=None,
    )

    assert report.selected == 2
    assert report.completed == 0
    assert report.remaining == 71
    assert calls == ["load", "count"]


@pytest.mark.asyncio
async def test_persist_summary_updates_only_blank_summary_and_compact_receipt() -> None:
    item = _item()
    original_tags = list(item.tags)
    original_categories = list(item.categories)
    session = _Session(item)
    diagnostics = LLMCompletionDiagnostics(
        requested_model="minimax/minimax-m2.7",
        actual_model="minimax/minimax-m2.7",
    )

    outcome = await _persist_summary(
        _snapshot(item),
        "A concise generated summary.",
        diagnostics=diagnostics,
        requested_model="minimax/minimax-m2.7",
        session_factory=lambda _tenant: session,
        now=lambda: datetime(2026, 8, 16, tzinfo=timezone.utc),
    )

    assert outcome == "completed"
    assert item.summary == "A concise generated summary."
    assert item.raw_content == "Preserved source text for a concise summary."
    assert item.tags == original_tags
    assert item.categories == original_categories
    assert item.metadata_["kept"] == {"value": True}
    assert item.metadata_["summary_backfill"] == {
        "schema_version": 1,
        "source": "backfill_missing_summaries",
        "completed_at": "2026-08-16T00:00:00+00:00",
        "requested_model": "minimax/minimax-m2.7",
        "source_content_hash": _snapshot(item).source_content_hash,
        "source_type": "media",
    }
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "concurrent_change",
    [
        {"summary": "A newer summary won the race."},
        {"raw_content": "Source content changed after candidate selection."},
    ],
)
async def test_persist_summary_skips_rows_changed_after_selection(concurrent_change) -> None:
    item = _item()
    candidate = _snapshot(item)
    for field, value in concurrent_change.items():
        setattr(item, field, value)
    session = _Session(item)

    outcome = await _persist_summary(
        candidate,
        "Generated from the old source.",
        diagnostics=LLMCompletionDiagnostics(),
        requested_model="minimax/minimax-m2.7",
        session_factory=lambda _tenant: session,
    )

    assert outcome == "skipped"
    assert session.commits == 0
    assert "summary_backfill" not in item.metadata_


@pytest.mark.asyncio
async def test_provider_failure_leaves_item_unchanged_and_reports_failure() -> None:
    item = _item()
    candidate = _snapshot(item)
    writes = []

    class FailingLlm:
        async def summarize(self, *_args, **_kwargs):
            raise RuntimeError("provider body must not be surfaced")

    async def loader(**_kwargs):
        return [candidate]

    async def counter(**_kwargs):
        return 1

    async def writer(*_args, **_kwargs):
        writes.append(True)
        return "completed"

    report = await run_summary_backfill(
        tenant_id=TENANT,
        limit=1,
        write=True,
        candidate_loader=loader,
        candidate_counter=counter,
        summary_writer=writer,
        llm=FailingLlm(),
    )

    assert report.failed == 1
    assert report.completed == 0
    assert report.remaining == 1
    assert writes == []
    assert item.summary is None


@pytest.mark.asyncio
async def test_bounds_and_source_types_fail_closed() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        await run_summary_backfill(tenant_id=" ", limit=1)
    with pytest.raises(ValueError, match="limit"):
        await run_summary_backfill(tenant_id=TENANT, limit=0)
    with pytest.raises(ValueError, match="batch_size"):
        await run_summary_backfill(tenant_id=TENANT, limit=1, batch_size=MAX_BATCH_SIZE + 1)
    with pytest.raises(ValueError, match="source_types"):
        await run_summary_backfill(tenant_id=TENANT, limit=1, source_types=("note",))
