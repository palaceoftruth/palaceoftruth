"""Bounded SAR-1083 relationship telemetry canary.

The contract is intentionally compiled into the application: callers cannot
change the fixture, tenant, scope, target, or mutation policy.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import engine
from app.embedding_profile import resolve_embedding_profile
from app.models.item import Item
from app.models.job import Job
from app.models.relationship import ItemRelationship
from app.schemas.memory import MemoryEntryRequest, MemoryScope
from app.services.embedder import EmbeddingService
from app.services.item_processing import process_prebuilt_item
from app.services.llm import LLMService
from app.services.memory import accept_canonical_memory_entry
from app.services.relationship_canary_contract import (
    CLASSIFICATION_SAMPLE_COUNTS,
    EXPECTED_ALIASES,
    FIXTURE_COMMIT,
    FIXTURE_ID,
    FIXTURE_SHA256,
    RelationshipCanaryContractError,
    SCOPE_PAYLOAD,
    TARGET_CLUSTER,
    TARGET_NAMESPACE,
    TASK_ID,
    TENANT_ID,
    empirical_p95,
    idempotency_key,
    load_validated_fixture,
    safety_report,
)
from app.services.relationship_telemetry import relationship_telemetry_snapshot
from app.services.relationships import RelationshipOperationResult, RelationshipService
from app.services.search import _embedding_search_plan

FIXTURE_CREATED_AT = datetime(2026, 7, 16, tzinfo=timezone.utc)
SCOPE = MemoryScope(**SCOPE_PAYLOAD)
SOURCE = "sar-1083-relationship-canary"
BASELINE_ID = "409e9e75-f66d-45e9-a7cb-e7b3a548dbad"
CASE_SAMPLE_COUNTS = CLASSIFICATION_SAMPLE_COUNTS
TOTAL_SAMPLE_COUNT = sum(CASE_SAMPLE_COUNTS.values())
_LOCK_KEY = f"{FIXTURE_ID}:live-run"


@asynccontextmanager
async def live_canary_lock():
    """Serialize the one-shot run across backend replicas and DB commits."""

    async with engine.connect() as connection:
        acquired = bool(
            (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": _LOCK_KEY},
                )
            ).scalar_one()
        )
        if not acquired:
            raise RelationshipCanaryContractError("another SAR-1083 canary execution is active")
        try:
            yield
        finally:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _LOCK_KEY},
            )


def _memory_request(case_id: str, record: dict[str, str]) -> MemoryEntryRequest:
    alias = record["alias"]
    return MemoryEntryRequest(
        tenant_id=TENANT_ID,
        title=record["title"],
        body=record["summary"],
        summary=record["summary"],
        source=SOURCE,
        created_at=FIXTURE_CREATED_AT,
        created_by_role="human-andrew",
        tags=[TASK_ID.lower(), FIXTURE_ID, case_id, alias, "synthetic", "retain"],
        scope=SCOPE,
        metadata={
            "relationship_canary": {
                "task_id": TASK_ID,
                "fixture_id": FIXTURE_ID,
                "fixture_sha256": FIXTURE_SHA256,
                "fixture_commit": FIXTURE_COMMIT,
                "case_id": case_id,
                "record_alias": alias,
                "synthetic": True,
                "retain": True,
            }
        },
        idempotency_key=idempotency_key(case_id, alias),
        fact_kind="observation",
        enable_ai_enrichment=False,
        relationship_policy="skip",
    )


async def _materialize_record(
    db: AsyncSession,
    *,
    request: MemoryEntryRequest,
    embedder: EmbeddingService,
    llm: LLMService,
) -> dict[str, Any]:
    accepted = await accept_canonical_memory_entry(db, body=request, signing_key=None)
    if accepted.source_item_id is None:
        raise RelationshipCanaryContractError("memory acceptance did not return a source item")
    item = await db.get(Item, accepted.source_item_id)
    if item is None:
        raise RelationshipCanaryContractError("accepted canary item is missing")
    if accepted.replayed and accepted.job.status != "completed":
        raise RelationshipCanaryContractError(
            "an incomplete canary record already exists; a new fixture/run version and deployment are required"
        )
    if not accepted.replayed and accepted.job.status in {"queued", "processing"}:
        result = await process_prebuilt_item(
            db,
            item=item,
            embedder=embedder,
            llm=llm,
            tenant_id=TENANT_ID,
            job=accepted.job,
            enable_ai_enrichment=False,
        )
        if result.status != "completed":
            raise RelationshipCanaryContractError("canary item processing did not complete")
        item = await db.get(Item, accepted.source_item_id)
    if accepted.job.status == "failed" or item is None or item.status != "ready" or item.deleted_at is not None:
        raise RelationshipCanaryContractError("canary item is not ready after acceptance")
    metadata = ((item.metadata_ or {}).get("memory_entry") or {}).get("metadata") or {}
    if metadata.get("relationship_canary") != request.metadata["relationship_canary"]:
        raise RelationshipCanaryContractError("existing canary item metadata does not match the fixture")
    if item.title != request.title or item.summary != request.summary or item.raw_content != request.body:
        raise RelationshipCanaryContractError("existing canary item content does not match the fixture")
    return {
        "alias": request.metadata["relationship_canary"]["record_alias"],
        "item_id": str(item.id),
        "job_id": str(accepted.job.id),
        "created": not accepted.replayed,
        "replayed": accepted.replayed,
        "status": item.status,
    }


async def _indexed_item_count(db: AsyncSession, item_ids: list[uuid.UUID]) -> int:
    plan = _embedding_search_plan(resolve_embedding_profile())
    profile_filter = ""
    params: dict[str, Any] = {"item_ids": [str(item_id) for item_id in item_ids]}
    if plan.profile_name is not None:
        profile_filter = "AND e.profile_name = :embedding_profile_name AND e.dimensions = :embedding_dimensions"
        params.update(
            {
                "embedding_profile_name": plan.profile_name,
                "embedding_dimensions": plan.dimensions,
            }
        )
    return int(
        (
            await db.execute(
                text(
                    f"""
                    SELECT COUNT(DISTINCT e.item_id)
                    FROM {plan.table_name} e
                    WHERE e.item_id = ANY(CAST(:item_ids AS uuid[]))
                      {profile_filter}
                    """
                ),
                params,
            )
        ).scalar_one()
    )


async def _pair_edge_count(
    db: AsyncSession,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ItemRelationship)
            .where(
                ItemRelationship.source_item_id == source_id,
                ItemRelationship.target_item_id == target_id,
            )
        )
        or 0
    )


def _operation_payload(
    case_id: str,
    result: RelationshipOperationResult,
    *,
    edge_count_before: int,
    edge_count_after: int,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "relationship": result.relationship,
        "confidence": result.confidence,
        "validation_outcome": result.validation_outcome,
        "provider": result.provider,
        "retry_provider": result.retry_provider,
        "fallback_used": result.fallback_used,
        "retry_count": result.retry_count,
        "duration_seconds": result.duration_seconds,
        "edge_persisted": result.edge_persisted,
        "edge_count_before": edge_count_before,
        "edge_count_after": edge_count_after,
        "edge_delta": edge_count_after - edge_count_before,
    }


def _case_passed(case_id: str, operation: dict[str, Any], expected: dict[str, Any]) -> bool:
    if case_id == "valid_related":
        return (
            operation["relationship"] in expected["allowed_relationships"]
            and operation["confidence"] >= expected["minimum_confidence"]
            and operation["edge_count_after"] >= expected["minimum_edges"]
            and operation["validation_outcome"] == expected["validation_outcome"]
        )
    if case_id == "empty_unrelated":
        return (
            operation["relationship"] == "none"
            and operation["validation_outcome"] == expected["validation_outcome"]
            and operation["edge_count_after"] <= expected["maximum_edges"]
        )
    observed_error = operation["validation_outcome"] in expected["validation_outcomes"]
    attribution_ok = (
        operation["retry_count"] > 0 or operation["fallback_used"]
        if observed_error and expected["require_retry_or_fallback_attribution_if_observed"]
        else True
    )
    return not operation["edge_persisted"] and attribution_ok


def _validate_runtime_gate(*, authorization_id: str, expected_app_version: str) -> str:
    live_version = settings.app_version or "0.1.0"
    if not settings.sar1083_relationship_canary_enabled:
        raise RelationshipCanaryContractError("SAR-1083 canary is disabled on this deployment")
    if (
        settings.deployment_cluster != TARGET_CLUSTER
        or settings.deployment_namespace != TARGET_NAMESPACE
    ):
        raise RelationshipCanaryContractError("deployment identity does not match the approved target")
    if (
        not settings.sar1083_relationship_canary_authorization_id
        or authorization_id != settings.sar1083_relationship_canary_authorization_id
    ):
        raise RelationshipCanaryContractError("authorization_id does not match the deployed approval")
    if expected_app_version.strip() != live_version:
        raise RelationshipCanaryContractError("expected app version does not match the live revision")
    return live_version


def _run_ledger(item: Item) -> dict[str, Any] | None:
    ledger = (item.metadata_ or {}).get("sar1083_canary_run")
    return ledger if isinstance(ledger, dict) else None


async def _write_run_ledger(
    db: AsyncSession,
    item: Item,
    *,
    status: str,
    authorization_id: str,
    report: dict[str, Any] | None = None,
    error_class: str | None = None,
) -> None:
    item.metadata_ = {
        **(item.metadata_ or {}),
        "sar1083_canary_run": {
            "status": status,
            "authorization_id": authorization_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "report": report,
            "error_class": error_class,
        },
    }
    await db.commit()


async def run_live_canary(
    db: AsyncSession,
    *,
    embedder: EmbeddingService,
    llm: LLMService,
    authorization_id: str,
    expected_app_version: str,
) -> dict[str, Any]:
    authorization_id = authorization_id.strip()
    if not authorization_id:
        raise RelationshipCanaryContractError("authorization_id is required")
    live_version = _validate_runtime_gate(
        authorization_id=authorization_id,
        expected_app_version=expected_app_version,
    )

    fixture = load_validated_fixture()
    started_at = datetime.now(timezone.utc)
    telemetry_before = relationship_telemetry_snapshot()

    # Fail closed if the dedicated tenant contains anything outside this fixture.
    expected_keys = {
        idempotency_key(case["id"], record["alias"])
        for case in fixture["cases"]
        for record in case["records"]
    }
    existing_keys = set(
        (
            await db.execute(
                select(Item.idempotency_key).where(
                    Item.tenant_id == TENANT_ID,
                    Item.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    if None in existing_keys or not existing_keys.issubset(expected_keys):
        raise RelationshipCanaryContractError("dedicated canary tenant contains non-fixture records")

    records: list[dict[str, Any]] = []
    alias_to_id: dict[str, uuid.UUID] = {}
    for case in fixture["cases"]:
        for record in case["records"]:
            materialized = await _materialize_record(
                db,
                request=_memory_request(case["id"], record),
                embedder=embedder,
                llm=llm,
            )
            records.append(materialized)
            alias_to_id[record["alias"]] = uuid.UUID(materialized["item_id"])

    item_ids = list(alias_to_id.values())
    indexed_count = await _indexed_item_count(db, item_ids)
    if indexed_count != 6:
        raise RelationshipCanaryContractError("not all canary items are indexed")

    control_item_id = alias_to_id[EXPECTED_ALIASES[0]]
    control_item = await db.get(Item, control_item_id)
    if control_item is None:
        raise RelationshipCanaryContractError("canary execution ledger item is missing")
    ledger = _run_ledger(control_item)
    if ledger is not None:
        if ledger.get("status") == "complete" and isinstance(ledger.get("report"), dict):
            replayed_report = dict(ledger["report"])
            replayed_report["replayed_execution"] = True
            replayed_report["replay_checked_at"] = datetime.now(timezone.utc).isoformat()
            return replayed_report
        raise RelationshipCanaryContractError(
            "a prior canary execution is incomplete; a new fixture/run version and deployment are required"
        )

    await _write_run_ledger(
        db,
        control_item,
        status="started",
        authorization_id=authorization_id,
    )
    try:
        relationship_service = RelationshipService(db, embedder=embedder, llm=llm)
        operations: list[dict[str, Any]] = []
        cases_report: list[dict[str, Any]] = []
        for case in fixture["cases"]:
            case_id = case["id"]
            source_id = alias_to_id[case["records"][0]["alias"]]
            target_id = alias_to_id[case["records"][1]["alias"]]
            expected = case.get("expected") or case["expected_observations"]
            case_operations: list[dict[str, Any]] = []
            for sample_index in range(1, CASE_SAMPLE_COUNTS[case_id] + 1):
                edge_count_before = await _pair_edge_count(db, source_id, target_id)
                if case_id == "valid_related":
                    result = await relationship_service.classify_candidate(
                        source_id,
                        target_id,
                        tenant_id=TENANT_ID,
                        # Persist at most the first accepted edge. Remaining
                        # samples are observation-only latency/quality probes.
                        persist=edge_count_before == 0,
                        allowed_relationships=case["expected"]["allowed_relationships"],
                    )
                else:
                    result = await relationship_service.classify_candidate(
                        source_id,
                        target_id,
                        tenant_id=TENANT_ID,
                        persist=False,
                    )
                edge_count_after = await _pair_edge_count(db, source_id, target_id)
                operation = _operation_payload(
                    case_id,
                    result,
                    edge_count_before=edge_count_before,
                    edge_count_after=edge_count_after,
                )
                operation["sample_index"] = sample_index
                operation["passed"] = _case_passed(case_id, operation, expected)
                operations.append(operation)
                case_operations.append(operation)

            final_edge_count = case_operations[-1]["edge_count_after"]
            case_passed = (
                any(operation["passed"] for operation in case_operations)
                and final_edge_count >= expected["minimum_edges"]
                if case_id == "valid_related"
                else all(operation["passed"] for operation in case_operations)
            )
            case_durations = [
                float(operation["duration_seconds"])
                for operation in case_operations
            ]
            cases_report.append(
                {
                    "case_id": case_id,
                    "passed": case_passed,
                    "sample_count": len(case_operations),
                    "p95_seconds": empirical_p95(case_durations),
                    "final_edge_count": final_edge_count,
                }
            )

        durations = [float(operation["duration_seconds"]) for operation in operations]
        if len(durations) != TOTAL_SAMPLE_COUNT:
            raise RelationshipCanaryContractError("canary latency sample floor was not met")
        telemetry_after = relationship_telemetry_snapshot()
        ended_at = datetime.now(timezone.utc)
        passed = all(case["passed"] for case in cases_report)
        report = {
            "task_id": TASK_ID,
            "mode": "write",
            "passed": passed,
            "replayed_execution": False,
            "fixture_id": FIXTURE_ID,
            "fixture_sha256": FIXTURE_SHA256,
            "fixture_commit": FIXTURE_COMMIT,
            "baseline_id": BASELINE_ID,
            "authorization_id": authorization_id,
            "target": {
                "cluster": TARGET_CLUSTER,
                "namespace": TARGET_NAMESPACE,
                "tenant_id": TENANT_ID,
                "scope": SCOPE.model_dump(mode="json"),
            },
            "live_revision": live_version,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "records": records,
            "record_count": len(records),
            "indexed_record_count": indexed_count,
            "created_count": sum(bool(record["created"]) for record in records),
            "replayed_count": sum(bool(record["replayed"]) for record in records),
            "cases": cases_report,
            "operations": operations,
            "latency": {
                "method": "empirical_nearest_rank",
                "population": "fixed_fixture_mix",
                "case_sample_counts": dict(CASE_SAMPLE_COUNTS),
                "unit": "seconds",
                "sample_count": len(durations),
                "samples": durations,
                "p95": empirical_p95(durations),
            },
            "telemetry_before": telemetry_before,
            "telemetry_after": telemetry_after,
            "safety": safety_report(),
        }
        control_item = await db.get(Item, control_item_id)
        if control_item is None:
            raise RelationshipCanaryContractError("canary execution ledger item disappeared")
        await _write_run_ledger(
            db,
            control_item,
            status="complete",
            authorization_id=authorization_id,
            report=report,
        )
        return report
    except Exception as exc:
        await db.rollback()
        try:
            control_item = await db.get(Item, control_item_id)
            if control_item is not None:
                await _write_run_ledger(
                    db,
                    control_item,
                    status="failed",
                    authorization_id=authorization_id,
                    error_class=exc.__class__.__name__,
                )
        except Exception:
            await db.rollback()
        if isinstance(exc, RelationshipCanaryContractError):
            raise
        raise RelationshipCanaryContractError(
            "live canary failed after execution started"
        ) from exc
