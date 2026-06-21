from datetime import datetime, timezone
import uuid

from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.models.palace import TemporalFact
from app.services import source_compiler
from app.services.source_compiler import (
    plan_claim_backfill,
    plan_source_backfill,
    project_claim_from_temporal_fact,
    project_item_source,
)


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


def _fact(
    *,
    tenant_id: str = "tenant-a",
    source_item_id: uuid.UUID | None = None,
    status: str = "active",
    metadata=None,
) -> TemporalFact:
    return TemporalFact(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source_item_id=source_item_id or uuid.uuid4(),
        fact_key="fact-key-a",
        source_fingerprint="source-fingerprint-a",
        subject="Palace",
        predicate="supports",
        object_text="claim diagnostics",
        confidence=0.8,
        status=status,
        metadata_json=metadata or {},
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


def test_project_claim_from_temporal_fact_is_deterministic_and_source_backed() -> None:
    source_item_id = uuid.uuid4()
    fact = _fact(source_item_id=source_item_id)

    first = project_claim_from_temporal_fact(fact)
    second = project_claim_from_temporal_fact(fact)

    assert first.claim_key == "temporal_fact:fact-key-a"
    assert first.claim_key == second.claim_key
    assert first.claim_text == "Palace supports claim diagnostics"
    assert first.claim_type == "fact"
    assert first.status == "active"
    assert first.support_role == "supports"
    assert first.source_item_id == source_item_id
    assert first.source_digest == "source-fingerprint-a"
    assert first.metadata["temporal_fact_id"] == str(fact.id)


def test_project_claim_from_temporal_fact_reports_conflicts_and_stale_sources() -> None:
    conflicted = project_claim_from_temporal_fact(
        _fact(metadata={"contradiction_sweep": {"conflict_count": 1, "conflicting_fact_ids": ["fact-b"]}})
    )
    stale = project_claim_from_temporal_fact(_fact(status="superseded"))

    assert conflicted.status == "conflicted"
    assert conflicted.support_role == "supports"
    assert stale.status == "stale"
    assert stale.support_role == "derived_from"


def test_claim_backfill_plan_is_tenant_bounded_and_requires_source_records() -> None:
    supported_item_id = uuid.uuid4()
    missing_item_id = uuid.uuid4()
    other_tenant_item_id = uuid.uuid4()
    supported_fact = _fact(source_item_id=supported_item_id)
    missing_fact = _fact(source_item_id=missing_item_id)
    other_tenant_fact = _fact(tenant_id="tenant-b", source_item_id=other_tenant_item_id)

    report, projections = plan_claim_backfill(
        [supported_fact, missing_fact, other_tenant_fact],
        tenant_id="tenant-a",
        dry_run=True,
        supported_source_item_ids={supported_item_id},
    )

    assert [projection.temporal_fact_id for projection in projections] == [supported_fact.id]
    assert report.facts_seen == 2
    assert report.claims_planned == 1
    assert report.claim_sources_planned == 1
    assert report.claims_upserted == 0
    assert report.claim_sources_upserted == 0
    assert report.unsupported_facts == 1


def test_claim_upsert_uses_claim_key_conflict_constraint() -> None:
    captured = {}

    class _Session:
        async def scalar(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return uuid.uuid4()

    projection = project_claim_from_temporal_fact(_fact())

    import asyncio

    asyncio.run(source_compiler._upsert_claim(_Session(), projection))

    assert "ON CONFLICT ON CONSTRAINT uq_claims_tenant_claim_key" in captured["sql"]
    assert "metadata" in captured["sql"]


def test_claim_source_upsert_uses_support_constraint() -> None:
    captured = {}

    class _Session:
        async def scalar(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return uuid.uuid4()

    projection = project_claim_from_temporal_fact(_fact())

    import asyncio

    asyncio.run(
        source_compiler._upsert_claim_source(
            _Session(),
            projection=projection,
            claim_id=uuid.uuid4(),
            source_record_id=uuid.uuid4(),
        )
    )

    assert "ON CONFLICT ON CONSTRAINT uq_claim_sources_support" in captured["sql"]
    assert "status" in captured["sql"]
    assert "source_span" in captured["sql"]


def test_claim_source_lookup_excludes_failed_and_deleted_source_records() -> None:
    captured = {}

    class _ScalarResult:
        def all(self):
            return []

    class _Session:
        async def scalars(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ScalarResult()

    import asyncio

    asyncio.run(
        source_compiler._latest_source_records_by_item_id(
            _Session(),
            tenant_id="tenant-a",
            item_ids={uuid.uuid4()},
        )
    )

    assert "source_records.status IN" in captured["sql"]


def test_mark_prior_claim_sources_stale_scopes_to_same_tenant_and_claim() -> None:
    captured = {}

    class _Session:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))

    import asyncio

    asyncio.run(
        source_compiler._mark_prior_claim_sources_stale(
            _Session(),
            tenant_id="tenant-a",
            claim_id=uuid.uuid4(),
            active_claim_source_id=uuid.uuid4(),
        )
    )

    assert "UPDATE claim_sources SET status=" in captured["sql"]
    assert "claim_sources.tenant_id = " in captured["sql"]
    assert "claim_sources.claim_id = " in captured["sql"]
    assert "claim_sources.id != " in captured["sql"]
    assert "claim_sources.status = " in captured["sql"]


def test_mark_unsupported_claim_stales_claim_and_current_sources() -> None:
    captured = {"updates": []}
    claim_id = uuid.uuid4()

    class _Session:
        async def scalar(self, statement):
            captured["select"] = str(statement.compile(dialect=postgresql.dialect()))
            return claim_id

        async def execute(self, statement):
            captured["updates"].append(str(statement.compile(dialect=postgresql.dialect())))

    projection = project_claim_from_temporal_fact(_fact())

    import asyncio

    asyncio.run(source_compiler._mark_unsupported_claim_stale(_Session(), projection=projection))

    assert "claims.claim_key = " in captured["select"]
    assert len(captured["updates"]) == 2
    assert "UPDATE claims SET status=" in captured["updates"][0]
    assert "claims.tenant_id = " in captured["updates"][0]
    assert "claims.status = " in captured["updates"][0]
    assert "UPDATE claim_sources SET status=" in captured["updates"][1]
    assert "claim_sources.tenant_id = " in captured["updates"][1]
    assert "claim_sources.claim_id = " in captured["updates"][1]
    assert "claim_sources.status = " in captured["updates"][1]
