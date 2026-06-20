from datetime import datetime, timezone
import uuid

from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.services import source_compiler
from app.services.source_compiler import plan_source_backfill, project_item_source


def _item(
    *,
    tenant_id: str = "tenant-a",
    status: str = "ready",
    deleted_at=None,
    chunks=None,
    content_hash: str | None = "hash-a",
    metadata=None,
) -> Item:
    return Item(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source_type="note",
        source_url="https://example.test/source",
        title="Source",
        raw_content="hello world",
        content_chunks=chunks if chunks is not None else [{"index": 0, "text": "hello world", "token_count": 2}],
        content_hash=content_hash,
        status=status,
        deleted_at=deleted_at,
        metadata_=metadata or {},
    )


def test_project_item_source_materializes_active_chunks() -> None:
    projection = project_item_source(
        _item(chunks=[{"index": 2, "text": " useful chunk ", "span": {"page": 3}, "token_count": "7"}])
    )

    assert projection.status == "active"
    assert projection.source_kind == "note"
    assert projection.source_uri == "https://example.test/source"
    assert projection.chunks[0].chunk_index == 2
    assert projection.chunks[0].chunk_text == "useful chunk"
    assert projection.chunks[0].span == {"page": 3}
    assert projection.chunks[0].token_count == 7


def test_project_item_source_failed_and_deleted_records_do_not_feed_chunks() -> None:
    failed = project_item_source(_item(status="failed", metadata={"failure_reason": "extractor unavailable"}))
    deleted = project_item_source(_item(status="deleted", deleted_at=datetime.now(timezone.utc)))

    assert failed.status == "failed"
    assert failed.failure_reason == "extractor unavailable"
    assert failed.chunks == ()
    assert deleted.status == "deleted"
    assert deleted.chunks == ()


def test_backfill_plan_is_tenant_bounded_and_dry_run_safe() -> None:
    tenant_item = _item(tenant_id="tenant-a")
    other_item = _item(tenant_id="tenant-b")

    report, projections = plan_source_backfill([tenant_item, other_item], tenant_id="tenant-a", dry_run=True)

    assert [projection.item_id for projection in projections] == [tenant_item.id]
    assert report.records_planned == 1
    assert report.chunks_planned == 1
    assert report.records_upserted == 0
    assert report.chunks_upserted == 0
    assert report.skipped_items == 1


def test_source_projection_is_idempotent_for_same_item_version() -> None:
    item = _item(content_hash=None, chunks=["same text"])

    first = project_item_source(item)
    second = project_item_source(item)

    assert first.source_version == second.source_version
    assert first.content_hash == second.content_hash
    assert first.chunks[0].chunk_digest == second.chunks[0].chunk_digest


def test_source_version_changes_when_chunk_projection_changes() -> None:
    item = _item(content_hash="same-source-content", chunks=[{"index": 0, "text": "alpha"}, {"index": 1, "text": "beta"}])
    original = project_item_source(item)

    item.content_chunks = [{"index": 0, "text": "alpha"}]
    repaired = project_item_source(item)

    assert original.content_hash == repaired.content_hash
    assert original.source_version != repaired.source_version


def test_duplicate_chunk_text_keeps_distinct_chunk_digests() -> None:
    projection = project_item_source(_item(chunks=[{"index": 0, "text": "repeat"}, {"index": 1, "text": "repeat"}]))

    assert len(projection.chunks) == 2
    assert projection.chunks[0].chunk_digest != projection.chunks[1].chunk_digest


def test_source_record_upsert_uses_mapped_metadata_attribute(monkeypatch) -> None:
    captured = {}

    class _Session:
        async def scalar(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return uuid.uuid4()

    projection = project_item_source(_item())

    import asyncio

    asyncio.run(source_compiler._upsert_source_record(_Session(), projection))

    assert "ON CONFLICT ON CONSTRAINT uq_source_records_tenant_item_version" in captured["sql"]
    assert "metadata" in captured["sql"]


def test_mark_prior_source_records_stale_scopes_to_same_tenant_and_item() -> None:
    captured = {}
    projection = project_item_source(_item())
    active_record_id = uuid.uuid4()

    class _Session:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))

    import asyncio

    asyncio.run(
        source_compiler._mark_prior_source_records_stale(
            _Session(),
            projection=projection,
            active_record_id=active_record_id,
        )
    )

    assert "UPDATE source_records SET status=" in captured["sql"]
    assert "source_records.tenant_id = " in captured["sql"]
    assert "source_records.item_id = " in captured["sql"]
    assert "source_records.id != " in captured["sql"]


def test_failed_projection_stales_prior_active_records() -> None:
    captured = {}
    projection = project_item_source(_item(status="failed"))

    class _Session:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))

    import asyncio

    asyncio.run(
        source_compiler._mark_prior_source_records_stale(
            _Session(),
            projection=projection,
            active_record_id=uuid.uuid4(),
        )
    )

    assert projection.status == "failed"
    assert "UPDATE source_records SET status=" in captured["sql"]
