from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from app.models.item import Item
from app.services.memory_storage_quality import (
    ExistingMemoryAdjudicationSignals,
    StaleMemoryAdjudicationReport,
    assess_memory_storage_item,
    build_stale_memory_adjudication_fixture_pack,
    memory_storage_quality_report_to_json,
    render_memory_storage_quality_report,
    score_stale_memory_adjudication_fixture,
    summarize_memory_storage_quality,
)


def _memory_item(
    *,
    item_id: uuid.UUID | None = None,
    title: str = "Codex MCP recall contract",
    raw_content: str = "Codex should retrieve scoped Palace memory before local audit files.",
    summary: str | None = "Scoped Palace memory recall contract.",
    source_url: str | None = "memory://codex/source",
    tags: list[str] | None = None,
    metadata_extra: dict | None = None,
    idempotency_key: str = "k" * 64,
) -> Item:
    metadata = {
        "memory_entry": {
            "schema_version": 1,
            "source": "codex",
            "source_url": source_url,
            "created_at": datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc).isoformat(),
            "created_by_role": "assistant",
            "scope": {"type": "agent", "key": "codex"},
            "metadata": {"source_item_id": "item-1", "source_job_id": "job-1"},
            "idempotency_key": idempotency_key,
        }
    }
    if metadata_extra:
        metadata.update(metadata_extra)
    return Item(
        id=item_id or uuid.uuid4(),
        source_type="note",
        source_url=source_url,
        title=title,
        summary=summary,
        raw_content=raw_content,
        metadata_=metadata,
        tags=tags if tags is not None else ["codex-local-memory", "scope-agent", "agent-codex"],
        categories=[],
        tenant_id="tenant-a",
        status="ready",
        created_at=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc),
        idempotency_key=idempotency_key,
    )


def test_memory_storage_quality_accepts_well_labeled_scoped_memory() -> None:
    warnings = assess_memory_storage_item(_memory_item())

    assert warnings == ()


def test_memory_storage_quality_flags_ambiguous_tenant_shared_and_low_signal_metadata() -> None:
    item = _memory_item(
        title="Untitled",
        raw_content="Need this",
        summary=None,
        source_url=None,
        tags=[],
        metadata_extra={
            "memory_entry": {
                "schema_version": 1,
                "source": "",
                "scope": {"type": "tenant_shared", "key": "codex"},
            }
        },
        idempotency_key="",
    )

    codes = {warning.code for warning in assess_memory_storage_item(item)}

    assert {
        "ambiguous_tenant_shared_scope",
        "missing_created_at",
        "missing_source",
        "missing_source_url",
        "missing_item_idempotency_key",
        "missing_tags",
        "low_signal_title",
        "low_signal_memory",
    } <= codes


def test_memory_storage_quality_flags_incomplete_derived_artifacts() -> None:
    item = _memory_item(
        metadata_extra={
            "memory_dream": {
                "artifact_type": "palace-dream-summary",
                "claims_need_source": False,
            }
        },
    )

    codes = {warning.code for warning in assess_memory_storage_item(item)}

    assert {
        "missing_derived_category",
        "incomplete_derived_artifact_metadata",
        "derived_artifact_masquerades_as_truth",
    } <= codes


def test_memory_storage_quality_report_exposes_counts_and_sample_ids_only() -> None:
    private_body = "private raw memory body should not appear in reports"
    private_title = "Private operator title"
    meaningful_idempotency_key = "operator/import/source/path/line-123"
    item = _memory_item(
        title=private_title,
        raw_content=private_body,
        source_url=None,
        metadata_extra={"memory_entry": {"scope": {"type": "workspace"}}},
        idempotency_key=meaningful_idempotency_key,
    )

    report = summarize_memory_storage_quality(
        tenant_id="tenant-a",
        items=[item],
        duplicate_idempotency_keys=[
            {"idempotency_key": item.idempotency_key, "count": 2, "sample_item_ids": [str(item.id)]}
        ],
    )
    text_report = render_memory_storage_quality_report(report)
    json_report = memory_storage_quality_report_to_json(report)
    payload = json.loads(json_report)

    assert report.ok is False
    assert payload["warning_count"] > 0
    assert payload["sample_warnings"][0]["item_id"] == str(item.id)
    assert private_body not in text_report
    assert private_body not in json_report
    assert private_title not in text_report
    assert private_title not in json_report
    assert meaningful_idempotency_key not in text_report
    assert meaningful_idempotency_key not in json_report


def test_stale_memory_adjudication_fixture_scores_current_first_with_lineage() -> None:
    cases = build_stale_memory_adjudication_fixture_pack()

    findings = score_stale_memory_adjudication_fixture(cases)

    assert len(cases) == 3
    assert findings == ()


def test_stale_memory_adjudication_fixture_flags_stale_first_without_body_text() -> None:
    case = build_stale_memory_adjudication_fixture_pack()[0]
    stale_source = case.stale_source_ids[0]
    broken_case = type(case)(
        case_id=case.case_id,
        current_source_id=case.current_source_id,
        stale_source_ids=case.stale_source_ids,
        expected_superseded_links=case.expected_superseded_links,
        source_timestamps=case.source_timestamps,
        surface_rankings={
            **case.surface_rankings,
            "memory_retrieve": (stale_source, case.current_source_id),
        },
    )

    findings = score_stale_memory_adjudication_fixture([broken_case])

    assert [finding.code for finding in findings] == ["current_fact_not_first"]
    assert findings[0].source_ids == (stale_source, case.current_source_id)
    assert "current fact body" not in json.dumps([finding.to_dict() for finding in findings])


def test_memory_storage_quality_report_includes_adjudication_counts_only() -> None:
    private_body = "private current-memory body should not appear"
    private_title = "Private source title should not appear"
    item = _memory_item(title=private_title, raw_content=private_body)
    source_id = str(item.id)
    report = summarize_memory_storage_quality(
        tenant_id="tenant-a",
        items=[item],
        stale_memory_adjudication=StaleMemoryAdjudicationReport(
            tenant_id="tenant-a",
            fixture_case_count=3,
            fixture_findings=(),
            existing_data_signals=ExistingMemoryAdjudicationSignals(
                temporal_facts_by_status={"active": 2, "superseded": 1},
                contradiction_edges=1,
                contradiction_edges_with_derived_context=0,
                memory_dream_items=1,
                memory_dreams_with_source_support=1,
                memory_dreams_with_contradiction_metrics=0,
                wakeup_briefs=1,
                stale_wakeup_briefs=0,
                retrieval_hint_artifacts=4,
                retrieval_hint_source_items=2,
                sample_source_ids=(source_id,),
            ),
        ),
    )

    text_report = render_memory_storage_quality_report(report)
    json_report = memory_storage_quality_report_to_json(report)
    payload = json.loads(json_report)

    assert report.ok is False
    assert "stale-memory-adjudication tenant=tenant-a status=warn" in text_report
    assert "contradictions_not_reflected_in_derived_context" in text_report
    assert payload["stale_memory_adjudication"]["dry_run"] is True
    assert payload["stale_memory_adjudication"]["mutating"] is False
    assert payload["stale_memory_adjudication"]["no_mutation_contract"] == {
        "auto_fixes_canonical_memory": False,
        "deletes": False,
        "prints_raw_memory_bodies": False,
        "rewrites": False,
        "suppresses": False,
    }
    assert payload["stale_memory_adjudication"]["existing_data_signals"]["sample_source_ids"] == [
        source_id
    ]
    assert private_body not in text_report
    assert private_body not in json_report
    assert private_title not in text_report
    assert private_title not in json_report
