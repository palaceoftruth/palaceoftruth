import asyncio
import uuid
from datetime import datetime, timezone
from typing import get_args

from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.models.palace import SourceRecord
from app.services import source_trust_summary
from app.services.source_trust_summary import (
    SourceTrustState,
    SourceTrustSummary,
    _SourceRecordRow,
    _trust_summary_for_item,
    map_retrieval_trust_class,
    source_trust_health_item_statement,
    source_record_batch_statement,
)


def _item(*, metadata=None, title: str = "Operator source", source_url: str | None = "https://example.test/source") -> Item:
    return Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="note",
        source_url=source_url,
        title=title,
        summary="summary",
        raw_content="raw body must not be projected",
        content_chunks=[{"text": "chunk text must not be projected"}],
        content_hash="hash-a",
        status="ready",
        metadata_=metadata or {},
    )


def _record(*, item_id: uuid.UUID, status: str = "active", source_uri: str | None = "https://example.test/source") -> SourceRecord:
    return SourceRecord(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        item_id=item_id,
        source_kind="note",
        source_uri=source_uri,
        source_version="version-a",
        content_hash="hash-a",
        status=status,
        failure_reason="extractor failed" if status == "failed" else None,
        metadata_={"stale_reason": "source changed upstream"} if status == "stale" else {},
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _summary(item: Item, record: SourceRecord | None = None, *, chunk_count: int = 0) -> SourceTrustSummary:
    source_row = _SourceRecordRow(record=record, chunk_count=chunk_count) if record is not None else None
    return _trust_summary_for_item(item_id=item.id, item=item, source_row=source_row)


def test_retrieval_trust_class_mapper_hides_internal_names() -> None:
    assert map_retrieval_trust_class("raw_source") == "source_backed"
    assert map_retrieval_trust_class("curated_memory") == "curated_memory"
    assert map_retrieval_trust_class("generated_synthesis") == "generated_unpromoted"
    assert map_retrieval_trust_class("low_support_generated") == "generated_unpromoted"
    assert map_retrieval_trust_class("stale_context") == "stale_source"
    assert map_retrieval_trust_class("broad_fallback") == "unknown"
    assert map_retrieval_trust_class("future_internal_class") == "unknown"


def test_source_backed_summary_is_compact_and_omits_body_and_chunk_preview() -> None:
    item = _item()
    record = _record(item_id=item.id)

    summary = _summary(item, record, chunk_count=2)

    assert summary.state == "source_backed"
    assert summary.source_record_id == record.id
    assert summary.source_status == "active"
    assert summary.chunk_count == 2
    assert summary.source_title == "Operator source"
    assert summary.source_url == "https://example.test/source"
    assert not hasattr(summary, "body")
    assert not hasattr(summary, "chunks")
    assert not hasattr(summary, "preview")


def test_public_trust_states_cover_policy_curated_generated_missing_stale_and_unknown() -> None:
    curated = _item(metadata={"memory_entry": {"scope": {"type": "agent", "key": "codex"}}})
    generated = _item(metadata={"wakeup_brief": {"scope_type": "tenant", "generation": 7}})
    policy_limited = _item(metadata={"memory_entry": {"metadata": {"policy_limited": True}}})
    missing = _item(metadata={})
    stale_item = _item()
    stale_record = _record(item_id=stale_item.id, status="stale")
    active_item = _item()
    active_record = _record(item_id=active_item.id)
    unknown_id = uuid.uuid4()

    observed_states = {
        _summary(curated).state,
        _summary(generated).state,
        _summary(policy_limited).state,
        _summary(missing).state,
    }
    stale_summary = _summary(stale_item, stale_record, chunk_count=3)
    observed_states.add(stale_summary.state)
    observed_states.add(_summary(active_item, active_record, chunk_count=1).state)
    observed_states.add(_trust_summary_for_item(item_id=unknown_id, item=None, source_row=None).state)

    assert observed_states == set(get_args(SourceTrustState))
    assert stale_summary.state == "stale_source"
    assert stale_summary.stale_reason == "source changed upstream"


def test_active_source_record_with_zero_chunks_is_source_missing() -> None:
    item = _item()
    record = _record(item_id=item.id)

    summary = _summary(item, record, chunk_count=0)

    assert summary.state == "source_missing"
    assert summary.warning == "source_record_has_no_chunks"
    assert summary.source_record_id == record.id


def test_failed_and_deleted_source_records_are_stale_source_warnings() -> None:
    failed_item = _item()
    deleted_item = _item()

    failed = _summary(failed_item, _record(item_id=failed_item.id, status="failed"), chunk_count=0)
    deleted = _summary(deleted_item, _record(item_id=deleted_item.id, status="deleted"), chunk_count=0)

    assert failed.state == "stale_source"
    assert failed.stale_reason == "extractor failed"
    assert failed.warning == "source_record_failed"
    assert deleted.state == "stale_source"
    assert deleted.warning == "source_record_deleted"


def test_batch_statement_counts_chunks_without_selecting_chunk_text_or_preview() -> None:
    statement = source_record_batch_statement(tenant_id="tenant-a", item_ids=(uuid.uuid4(), uuid.uuid4()))

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "LEFT OUTER JOIN source_chunks" in sql
    assert "count(source_chunks.id)" in sql
    assert "source_records.item_id IN" in sql
    assert "source_chunks.chunk_text" not in sql
    assert "preview" not in sql


def test_source_trust_health_item_statement_omits_raw_body_and_chunk_json() -> None:
    statement = source_trust_health_item_statement(tenant_id="tenant-a")

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "items.id" in sql
    assert "items.metadata" in sql
    assert "items.raw_content" not in sql
    assert "items.content_chunks" not in sql
    assert "source_chunks.chunk_text" not in sql


def test_get_source_trust_summaries_uses_one_item_query_and_one_source_record_query() -> None:
    item_a = _item()
    item_b = _item(metadata={"wakeup_brief": {"generation": 1}})
    record_a = _record(item_id=item_a.id)
    captured = {"scalars": 0, "execute": 0, "source_sql": ""}

    class _ScalarResult:
        def all(self):
            return [item_a, item_b]

    class _ExecuteResult:
        def all(self):
            return [(record_a, 1)]

    class _Session:
        async def scalars(self, statement):
            captured["scalars"] += 1
            return _ScalarResult()

        async def execute(self, statement):
            captured["execute"] += 1
            captured["source_sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ExecuteResult()

    summaries = asyncio.run(
        source_trust_summary.get_source_trust_summaries(
            _Session(),
            tenant_id="tenant-a",
            item_ids=[item_a.id, item_b.id],
        )
    )

    assert captured["scalars"] == 1
    assert captured["execute"] == 1
    assert "source_records.item_id IN" in captured["source_sql"]
    assert summaries[item_a.id].state == "source_backed"
    assert summaries[item_b.id].state == "generated_unpromoted"


def test_build_source_trust_health_summary_aggregates_counts_and_warning_labels() -> None:
    source_backed = _item()
    generated = _item(metadata={"wakeup_brief": {"generation": 1}})
    missing = _item()
    policy_limited = _item(metadata={"memory_entry": {"metadata": {"policy_limited": True}}})
    stale = _item()
    source_record = _record(item_id=source_backed.id)
    stale_record = _record(item_id=stale.id, status="failed")

    class _ScalarResult:
        def all(self):
            return [source_backed, generated, missing, policy_limited, stale]

    class _ExecuteResult:
        def __init__(self, rows) -> None:
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.item_sql = ""

        async def execute(self, _statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                self.item_sql = str(_statement.compile(dialect=postgresql.dialect()))
                return _ExecuteResult([
                    (item.id, item.metadata_)
                    for item in [source_backed, generated, missing, policy_limited, stale]
                ])
            return _ExecuteResult([(source_record, 2), (stale_record, 0)])

        async def scalars(self, _statement):
            return _ScalarResult()

    summary_session = _Session()
    summary = asyncio.run(
        source_trust_summary.build_source_trust_health_summary(summary_session, tenant_id="tenant-a")
    )

    warnings = {warning.warning for warning in summary.recent_warnings or []}
    assert summary.status == "ready"
    assert summary.total_contexts == 5
    assert summary.source_backed == 1
    assert summary.generated_unpromoted == 1
    assert summary.stale_missing == 2
    assert summary.policy_limited == 1
    assert "source_record_missing" in warnings
    assert "source_record_failed" in warnings
    assert "items.raw_content" not in summary_session.item_sql
    assert "items.content_chunks" not in summary_session.item_sql
