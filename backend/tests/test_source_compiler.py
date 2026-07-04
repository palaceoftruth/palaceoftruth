from datetime import datetime, timezone
import uuid

from sqlalchemy.dialects import postgresql

from app.models.item import Item
from app.models.palace import Claim, ClaimSource, TemporalFact
from app.services import source_compiler
from app.services.source_compiler import (
    ClaimSourceSupportSummary,
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
    assert report.source_records_marked_stale == 0
    assert report.claim_sources_marked_stale == 0
    assert report.claims_marked_stale == 0


def test_source_projection_is_idempotent_for_same_item_version() -> None:
    item = _item(content_hash=None, chunks=["same text"])

    first = project_item_source(item)
    second = project_item_source(item)

    assert first.source_version == second.source_version
    assert first.content_hash == second.content_hash
    assert first.chunks[0].chunk_digest == second.chunks[0].chunk_digest


def test_source_backfill_dry_run_and_live_rerun_are_idempotent() -> None:
    item = _item(chunks=[{"index": 0, "text": "stable source", "token_count": 2}])
    source_record_id = uuid.uuid4()
    source_chunk_id = uuid.uuid4()
    captured = {"record_upserts": 0, "chunk_upserts": 0, "stale_updates": 0, "commits": 0}

    class _ScalarResult:
        def all(self):
            return [item]

    class _ExecuteResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        async def scalars(self, statement):
            return _ScalarResult()

        async def scalar(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if "INSERT INTO source_records" in sql:
                captured["record_upserts"] += 1
                assert "ON CONFLICT ON CONSTRAINT uq_source_records_tenant_item_version" in sql
                return source_record_id
            if "INSERT INTO source_chunks" in sql:
                captured["chunk_upserts"] += 1
                assert "ON CONFLICT ON CONSTRAINT uq_source_chunks_tenant_record_index" in sql
                return source_chunk_id
            raise AssertionError(sql)

        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            assert "UPDATE source_records SET status=" in sql
            captured["stale_updates"] += 1
            return _ExecuteResult()

        async def commit(self):
            captured["commits"] += 1

    import asyncio

    dry_run = asyncio.run(
        source_compiler.backfill_source_records_and_chunks(
            _Session(),
            tenant_id="tenant-a",
            item_ids=[item.id],
            dry_run=True,
        )
    )
    first_live = asyncio.run(
        source_compiler.backfill_source_records_and_chunks(
            _Session(),
            tenant_id="tenant-a",
            item_ids=[item.id],
            dry_run=False,
        )
    )
    second_live = asyncio.run(
        source_compiler.backfill_source_records_and_chunks(
            _Session(),
            tenant_id="tenant-a",
            item_ids=[item.id],
            dry_run=False,
        )
    )

    assert dry_run.records_upserted == 0
    assert dry_run.chunks_upserted == 0
    assert first_live.records_upserted == second_live.records_upserted == 1
    assert first_live.chunks_upserted == second_live.chunks_upserted == 1
    assert captured == {"record_upserts": 2, "chunk_upserts": 2, "stale_updates": 2, "commits": 2}


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
    stale_record_id = uuid.uuid4()
    projection = project_item_source(_item())
    active_record_id = uuid.uuid4()

    class _ScalarResult:
        def all(self):
            return [stale_record_id]

    class _ExecuteResult:
        def scalars(self):
            return _ScalarResult()

    class _Session:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ExecuteResult()

    import asyncio

    result = asyncio.run(
        source_compiler._mark_prior_source_records_stale(
            _Session(),
            projection=projection,
            active_record_id=active_record_id,
        )
    )

    assert result == (stale_record_id,)
    assert "UPDATE source_records SET status=" in captured["sql"]
    assert "source_records.tenant_id = " in captured["sql"]
    assert "source_records.item_id = " in captured["sql"]
    assert "source_records.id != " in captured["sql"]


def test_failed_projection_stales_prior_active_records() -> None:
    captured = {}
    stale_record_id = uuid.uuid4()
    projection = project_item_source(_item(status="failed"))

    class _ScalarResult:
        def all(self):
            return [stale_record_id]

    class _ExecuteResult:
        def scalars(self):
            return _ScalarResult()

    class _Session:
        async def execute(self, statement):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ExecuteResult()

    import asyncio

    result = asyncio.run(
        source_compiler._mark_prior_source_records_stale(
            _Session(),
            projection=projection,
            active_record_id=uuid.uuid4(),
        )
    )

    assert result == (stale_record_id,)
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
    captured = {"sql": []}

    class _Session:
        async def scalar(self, statement):
            captured["sql"].append(str(statement.compile(dialect=postgresql.dialect())))
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

    assert len(captured["sql"]) == 1
    assert "ON CONFLICT ON CONSTRAINT uq_claim_sources_support" in captured["sql"][0]
    assert "source_chunk_id" in captured["sql"][0]
    assert "status" in captured["sql"][0]
    assert "source_span" in captured["sql"][0]


def test_claim_source_upsert_resolves_exact_chunk_link_from_span_digest() -> None:
    captured = {"sql": []}
    chunk_id = uuid.uuid4()
    claim_source_id = uuid.uuid4()

    class _Session:
        async def scalar(self, statement):
            captured["sql"].append(str(statement.compile(dialect=postgresql.dialect())))
            return chunk_id if len(captured["sql"]) == 1 else claim_source_id

    fact = _fact()
    projection = source_compiler.ClaimProjection(
        tenant_id=fact.tenant_id,
        temporal_fact_id=fact.id,
        source_item_id=fact.source_item_id,
        claim_key="decision:source-backed-wakeup",
        claim_text="Use source-backed wakeup decisions first",
        claim_type="decision",
        confidence=0.9,
        status="active",
        support_role="supports",
        source_digest="chunk-digest-a",
        source_span={"source_chunk_digest": "chunk-digest-a"},
        metadata={"compiler": "decision_claim_support_test"},
    )

    import asyncio

    result = asyncio.run(
        source_compiler._upsert_claim_source(
            _Session(),
            projection=projection,
            claim_id=uuid.uuid4(),
            source_record_id=uuid.uuid4(),
        )
    )

    assert result == claim_source_id
    assert "source_chunks.chunk_digest = " in captured["sql"][0]
    assert "source_chunk_id" in captured["sql"][1]


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


def test_source_record_invalidation_marks_current_decision_dependencies_stale() -> None:
    source_record_id = uuid.uuid4()
    claim_source_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    captured = {"sql": []}

    class _ScalarResult:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _ExecuteResult:
        def __init__(self, *, rows=None, scalars=None):
            self._rows = rows or []
            self._scalars = scalars or []

        def all(self):
            return self._rows

        def scalars(self):
            return _ScalarResult(self._scalars)

    class _Session:
        async def execute(self, statement):
            captured["sql"].append(str(statement.compile(dialect=postgresql.dialect())))
            if len(captured["sql"]) == 1:
                return _ExecuteResult(rows=[(claim_source_id, claim_id)])
            if len(captured["sql"]) == 2:
                return _ExecuteResult(scalars=[claim_source_id])
            return _ExecuteResult(scalars=[claim_id])

    import asyncio

    report = asyncio.run(
        source_compiler._invalidate_decision_claims_for_source_records(
            _Session(),
            tenant_id="tenant-a",
            source_record_ids=(source_record_id,),
        )
    )

    assert report.source_records_seen == 1
    assert report.claim_sources_marked_stale == 1
    assert report.claims_marked_stale == 1
    assert "JOIN claims" in captured["sql"][0]
    assert "claim_sources.tenant_id = " in captured["sql"][0]
    assert "claims.tenant_id = " in captured["sql"][0]
    assert "claims.claim_type = " in captured["sql"][0]
    assert "claims.status = " in captured["sql"][0]
    assert "UPDATE claim_sources SET status=" in captured["sql"][1]
    assert "UPDATE claims SET status=" in captured["sql"][2]


def test_source_record_invalidation_noops_when_dependency_absent() -> None:
    captured = {"sql": []}

    class _ExecuteResult:
        def all(self):
            return []

    class _Session:
        async def execute(self, statement):
            captured["sql"].append(str(statement.compile(dialect=postgresql.dialect())))
            return _ExecuteResult()

    import asyncio

    report = asyncio.run(
        source_compiler._invalidate_decision_claims_for_source_records(
            _Session(),
            tenant_id="tenant-a",
            source_record_ids=(uuid.uuid4(),),
        )
    )

    assert report.source_records_seen == 1
    assert report.claim_sources_marked_stale == 0
    assert report.claims_marked_stale == 0
    assert len(captured["sql"]) == 1


def test_source_record_invalidation_noops_when_no_changed_source_records() -> None:
    class _Session:
        async def execute(self, statement):
            raise AssertionError("no query should run without stale source records")

    import asyncio

    report = asyncio.run(
        source_compiler._invalidate_decision_claims_for_source_records(
            _Session(),
            tenant_id="tenant-a",
            source_record_ids=(),
        )
    )

    assert report.source_records_seen == 0
    assert report.claim_sources_marked_stale == 0
    assert report.claims_marked_stale == 0


def test_claim_support_state_reports_source_backed_weak_stale_missing_and_unpromoted() -> None:
    current_source = ClaimSourceSupportSummary(
        id=uuid.uuid4(),
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        source_item_id=uuid.uuid4(),
        source_record_status="active",
        support_role="supports",
        status="current",
        source_digest="digest-a",
        source_span={},
    )
    weak_source = ClaimSourceSupportSummary(
        id=uuid.uuid4(),
        source_record_id=uuid.uuid4(),
        source_chunk_id=None,
        source_item_id=uuid.uuid4(),
        source_record_status="active",
        support_role="supports",
        status="current",
        source_digest="digest-b",
        source_span={},
    )
    stale_source = ClaimSourceSupportSummary(
        id=uuid.uuid4(),
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        source_item_id=uuid.uuid4(),
        source_record_status="stale",
        support_role="supports",
        status="current",
        source_digest="digest-c",
        source_span={},
    )

    active_claim = Claim(id=uuid.uuid4(), tenant_id="tenant-a", claim_key="decision:a", claim_text="A", claim_type="decision", status="active")
    draft_claim = Claim(id=uuid.uuid4(), tenant_id="tenant-a", claim_key="decision:b", claim_text="B", claim_type="decision", status="draft")

    assert source_compiler._claim_support_state(active_claim, (current_source,)) == ("source_backed", None)
    assert source_compiler._claim_support_state(active_claim, (weak_source,)) == (
        "weak_source_support",
        "claim_source_lacks_exact_chunk",
    )
    assert source_compiler._claim_support_state(active_claim, (stale_source,)) == (
        "stale_source",
        "claim_source_not_current",
    )
    assert source_compiler._claim_support_state(active_claim, ()) == ("source_missing", "claim_has_no_source_support")
    assert source_compiler._claim_support_state(draft_claim, (current_source,)) == (
        "generated_unpromoted",
        "claim_not_promoted",
    )


def test_claim_support_report_filters_to_decision_claims_and_keeps_empty_sources_visible() -> None:
    decision_claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:source-backed-wakeup",
        claim_text="Use source-backed wakeup decisions first",
        claim_type="decision",
        confidence=0.9,
        status="active",
        metadata_={"review_role": "operator"},
    )
    captured = {"claim_sql": "", "source_sql": ""}

    class _ScalarResult:
        def all(self):
            return [decision_claim]

    class _ExecuteResult:
        def all(self):
            return []

    class _Session:
        async def scalars(self, statement):
            captured["claim_sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ScalarResult()

        async def execute(self, statement):
            captured["source_sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ExecuteResult()

    import asyncio

    report = asyncio.run(
        source_compiler.get_claim_support_report(
            _Session(),
            tenant_id="tenant-a",
            claim_type="decision",
            status="active",
            limit=25,
        )
    )

    assert "claims.claim_type = " in captured["claim_sql"]
    assert "claims.status = " in captured["claim_sql"]
    assert "claim_sources.tenant_id = " in captured["source_sql"]
    assert "source_records.tenant_id = " in captured["source_sql"]
    assert report.claims[0].claim_type == "decision"
    assert report.claims[0].support_state == "source_missing"
    assert report.claims[0].warning == "claim_has_no_source_support"


def test_claim_support_report_defaults_to_decisions_and_redacts_metadata_and_span() -> None:
    source_item_id = uuid.uuid4()
    claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:compact-support",
        claim_text="Expose compact support only",
        claim_type="decision",
        confidence=1.0,
        status="active",
        metadata_={
            "review_role": "operator",
            "task_id": "SAR-936",
            "body": "raw body must not leak",
            "chunk_text": "chunk body must not leak",
        },
    )
    claim_source = ClaimSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_id=claim.id,
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        support_role="supports",
        status="current",
        source_digest="digest-a",
        source_span={
            "source_chunk_digest": "digest-a",
            "page": 4,
            "preview": "source preview must not leak",
            "text": "raw span text must not leak",
        },
    )
    captured = {"claim_sql": ""}

    class _ScalarResult:
        def all(self):
            return [claim]

    class _ExecuteResult:
        def all(self):
            return [(claim_source, source_item_id, "active")]

    class _Session:
        async def scalars(self, statement):
            captured["claim_sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return _ScalarResult()

        async def execute(self, statement):
            return _ExecuteResult()

    import asyncio

    report = asyncio.run(source_compiler.get_claim_support_report(_Session(), tenant_id="tenant-a"))

    payload = report.claims[0]
    assert "claims.claim_type = " in captured["claim_sql"]
    assert payload.claim_type == "decision"
    assert payload.support_state == "source_backed"
    assert payload.metadata == {"review_role": "operator", "task_id": "SAR-936"}
    assert payload.sources[0].source_span == {"source_chunk_digest": "digest-a", "page": 4}


def test_answer_audit_report_maps_states_and_redacts_policy_metadata() -> None:
    promoted_source = ClaimSourceSupportSummary(
        id=uuid.uuid4(),
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        source_item_id=uuid.uuid4(),
        source_record_status="active",
        support_role="supports",
        status="current",
        source_digest="digest-a",
        source_span={"source_chunk_digest": "digest-a", "text": "raw source must not leak"},
    )
    promoted_claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:curated",
        claim_text="Use reviewed source-backed decisions.",
        claim_type="decision",
        confidence=0.95,
        status="active",
        metadata_={
            "review_action": "promote",
            "reviewed_by": "operator-a",
            "policy_limited": True,
            "policy_reason": "workspace scope only",
            "body": "raw claim body metadata must not leak",
        },
    )
    missing_claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:missing",
        claim_text="Missing support should stay visible.",
        claim_type="decision",
        confidence=0.5,
        status="active",
        metadata_={},
    )

    class _ScalarResult:
        def all(self):
            return [promoted_claim, missing_claim]

    class _ExecuteResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Session:
        def __init__(self) -> None:
            self.source_calls = 0

        async def scalars(self, statement):
            return _ScalarResult()

        async def execute(self, statement):
            self.source_calls += 1
            if self.source_calls == 1:
                return _ExecuteResult([(promoted_source, promoted_source.source_item_id, "active")])
            return _ExecuteResult([])

    import asyncio

    report = asyncio.run(source_compiler.get_answer_audit_report(_Session(), tenant_id="tenant-a"))

    assert report.audit_scope == "decision_claims"
    assert report.items[0].audit_state == "policy_limited"
    assert report.items[0].promotion_status == "promoted"
    assert report.items[0].metadata["policy_reason"] == "workspace scope only"
    assert report.items[0].sources[0].source_span == {"source_chunk_digest": "digest-a"}
    assert report.items[1].audit_state == "missing"
    assert report.items[1].warning == "claim_has_no_source_support"
    assert "raw" not in str(report)


def test_answer_audit_state_distinguishes_required_support_labels() -> None:
    base = {
        "id": uuid.uuid4(),
        "claim_key": "decision:audit-state",
        "claim_text": "Audit state should stay explicit.",
        "claim_type": "decision",
        "confidence": 1.0,
        "warning": None,
        "sources": (),
    }

    def claim(*, status: str, support_state: str, metadata: dict | None = None):
        return source_compiler.ClaimSupportSummary(status=status, support_state=support_state, metadata=metadata or {}, **base)

    assert source_compiler._answer_audit_state(claim(status="active", support_state="source_backed")) == "source_backed"
    assert source_compiler._answer_audit_state(
        claim(status="active", support_state="source_backed", metadata={"review_action": "promote"})
    ) == "curated"
    assert source_compiler._answer_audit_state(claim(status="draft", support_state="generated_unpromoted")) == "generated_unpromoted"
    assert source_compiler._answer_audit_state(claim(status="stale", support_state="stale_source")) == "stale"
    assert source_compiler._answer_audit_state(claim(status="active", support_state="source_missing")) == "missing"
    assert source_compiler._answer_audit_state(
        claim(status="active", support_state="source_backed", metadata={"policy_limited": True})
    ) == "policy_limited"


def test_review_decision_claim_promotes_only_with_exact_current_source_support() -> None:
    claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:promotion",
        claim_text="Use source-backed decisions at wakeup",
        claim_type="decision",
        confidence=0.91,
        status="draft",
        metadata_={"task_id": "SAR-937"},
    )
    claim_source = ClaimSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_id=claim.id,
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        support_role="supports",
        status="current",
        source_digest="digest-a",
        source_span={"source_chunk_digest": "digest-a", "text": "do not leak"},
    )
    source_item_id = uuid.uuid4()
    captured = {"updates": []}

    class _ScalarResult:
        def all(self):
            return [claim]

    class _ExecuteResult:
        def __init__(self, *, rows=None) -> None:
            self._rows = rows or []

        def all(self):
            return self._rows

    class _Session:
        async def scalar(self, statement):
            captured["claim_sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return claim

        async def scalars(self, statement):
            return _ScalarResult()

        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if sql.startswith("SELECT claim_sources"):
                return _ExecuteResult(rows=[(claim_source, source_item_id, "active")])
            captured["updates"].append(sql)
            return _ExecuteResult()

        async def commit(self):
            captured["committed"] = True

    import asyncio

    reviewed = asyncio.run(
        source_compiler.review_decision_claim(
            _Session(),
            tenant_id="tenant-a",
            claim_id=claim.id,
            action="promote",
            reviewed_by="operator-a",
            rationale="Reviewed source support.",
        )
    )

    assert "claims.claim_type = " in captured["claim_sql"]
    assert captured["committed"] is True
    assert len(captured["updates"]) == 1
    assert "UPDATE claims SET" in captured["updates"][0]
    assert reviewed.status == "active"
    assert reviewed.support_state == "source_backed"
    assert reviewed.metadata["reviewed_by"] == "operator-a"
    assert reviewed.metadata["review_action"] == "promote"
    assert reviewed.metadata["operator_reviews"][0]["previous_status"] == "draft"
    assert reviewed.sources[0].source_span == {"source_chunk_digest": "digest-a"}


def test_review_decision_claim_blocks_promotion_without_exact_source_support() -> None:
    claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:weak-support",
        claim_text="Weak support is not enough to promote",
        claim_type="decision",
        confidence=0.7,
        status="draft",
        metadata_={},
    )
    weak_source = ClaimSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_id=claim.id,
        source_record_id=uuid.uuid4(),
        source_chunk_id=None,
        support_role="supports",
        status="current",
        source_digest="digest-a",
        source_span={},
    )

    class _ExecuteResult:
        def all(self):
            return [(weak_source, uuid.uuid4(), "active")]

    class _Session:
        async def scalar(self, _statement):
            return claim

        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if sql.startswith("UPDATE claims"):
                raise AssertionError("promotion failure should not update the claim")
            return _ExecuteResult()

    import asyncio

    try:
        asyncio.run(
            source_compiler.review_decision_claim(
                _Session(),
                tenant_id="tenant-a",
                claim_id=claim.id,
                action="promote",
                reviewed_by="operator-a",
            )
        )
    except source_compiler.ClaimReviewError as exc:
        assert exc.code == "source_support_required"
    else:
        raise AssertionError("Expected weak source support to block promotion")


def test_review_decision_claim_blocks_promotion_with_current_contradiction() -> None:
    claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:conflicted-support",
        claim_text="Contradicted claims cannot become authority",
        claim_type="decision",
        confidence=0.9,
        status="draft",
        metadata_={},
    )
    supporting_source = ClaimSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_id=claim.id,
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        support_role="supports",
        status="current",
        source_digest="digest-a",
        source_span={},
    )
    contradicting_source = ClaimSource(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_id=claim.id,
        source_record_id=uuid.uuid4(),
        source_chunk_id=uuid.uuid4(),
        support_role="contradicts",
        status="current",
        source_digest="digest-b",
        source_span={},
    )

    class _ExecuteResult:
        def all(self):
            return [
                (supporting_source, uuid.uuid4(), "active"),
                (contradicting_source, uuid.uuid4(), "active"),
            ]

    class _Session:
        async def scalar(self, _statement):
            return claim

        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if sql.startswith("UPDATE claims"):
                raise AssertionError("conflicted support should block promotion before update")
            return _ExecuteResult()

    import asyncio

    try:
        asyncio.run(
            source_compiler.review_decision_claim(
                _Session(),
                tenant_id="tenant-a",
                claim_id=claim.id,
                action="promote",
                reviewed_by="operator-a",
            )
        )
    except source_compiler.ClaimReviewError as exc:
        assert exc.code == "source_support_required"
    else:
        raise AssertionError("Expected current contradiction to block promotion")


def test_review_decision_claim_rejects_without_deleting_source_support() -> None:
    claim = Claim(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        claim_key="decision:reject",
        claim_text="Reject this generated decision",
        claim_type="decision",
        confidence=0.4,
        status="draft",
        metadata_={},
    )
    captured = {"updates": []}

    class _ExecuteResult:
        def all(self):
            return []

    class _Session:
        async def scalar(self, _statement):
            return claim

        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if sql.startswith("UPDATE claims"):
                captured["updates"].append(sql)
            return _ExecuteResult()

        async def commit(self):
            captured["committed"] = True

    import asyncio

    reviewed = asyncio.run(
        source_compiler.review_decision_claim(
            _Session(),
            tenant_id="tenant-a",
            claim_id=claim.id,
            action="reject",
            reviewed_by="operator-a",
            rationale="Generated claim is not reliable.",
        )
    )

    assert reviewed.status == "rejected"
    assert reviewed.support_state == "not_authoritative"
    assert len(captured["updates"]) == 1
    assert "claim_sources" not in captured["updates"][0]
    assert captured["committed"] is True


def test_reviewed_claim_metadata_clears_stale_summary_rationale() -> None:
    metadata = source_compiler._reviewed_claim_metadata(
        existing_metadata={"review_rationale": "old rationale"},
        action="demote",
        previous_status="active",
        new_status="draft",
        reviewed_by="operator-a",
        review_role="operator",
        rationale=None,
    )

    assert "review_rationale" not in metadata
    assert metadata["operator_reviews"][0]["action"] == "demote"
    assert "rationale" not in metadata["operator_reviews"][0]
