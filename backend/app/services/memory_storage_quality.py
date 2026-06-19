"""Report-only memory storage quality checks."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item
from app.models.palace import RetrievalHintArtifact, TemporalFact
from app.models.relationship import ItemRelationship

MEMORY_DERIVED_METADATA_KEYS = (
    "memory_dream",
    "diary_rollup",
    "wakeup_brief",
    "retrieval_hint",
)
LOW_SIGNAL_TITLES = {"memory", "note", "untitled", "summary"}
LOW_SIGNAL_BODY_MAX_CHARS = 32
ADJUDICATION_FIXTURE_NAMESPACE = "palace-memory-adjudication"
CURRENT_FIRST_SURFACES = ("memory_retrieve", "wakeup_context", "dream_hygiene")


@dataclass(frozen=True)
class MemoryStorageQualityWarning:
    code: str
    item_id: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "item_id": self.item_id,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MemoryStorageQualityReport:
    tenant_id: str
    item_count: int
    warning_count: int
    warnings_by_code: dict[str, int]
    sample_warnings: tuple[MemoryStorageQualityWarning, ...]
    duplicate_idempotency_keys: tuple[dict[str, Any], ...] = ()
    stale_memory_adjudication: "StaleMemoryAdjudicationReport | None" = None

    @property
    def ok(self) -> bool:
        return (
            self.warning_count == 0
            and not self.duplicate_idempotency_keys
            and (self.stale_memory_adjudication is None or self.stale_memory_adjudication.ok)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "ok": self.ok,
            "item_count": self.item_count,
            "warning_count": self.warning_count,
            "warnings_by_code": dict(sorted(self.warnings_by_code.items())),
            "sample_warnings": [warning.to_dict() for warning in self.sample_warnings],
            "duplicate_idempotency_keys": list(self.duplicate_idempotency_keys),
            "stale_memory_adjudication": (
                self.stale_memory_adjudication.to_dict()
                if self.stale_memory_adjudication is not None
                else None
            ),
        }


@dataclass(frozen=True)
class StaleMemoryAdjudicationCase:
    case_id: str
    current_source_id: str
    stale_source_ids: tuple[str, ...]
    expected_superseded_links: tuple[str, ...]
    surface_rankings: dict[str, tuple[str, ...]]
    source_timestamps: dict[str, str]
    dream_claims_need_source: bool = True


@dataclass(frozen=True)
class StaleMemoryAdjudicationFinding:
    code: str
    case_id: str
    detail: str
    source_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "code": self.code,
            "detail": self.detail,
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True)
class ExistingMemoryAdjudicationSignals:
    temporal_facts_by_status: dict[str, int]
    contradiction_edges: int
    contradiction_edges_with_derived_context: int
    memory_dream_items: int
    memory_dreams_with_source_support: int
    memory_dreams_with_contradiction_metrics: int
    wakeup_briefs: int
    stale_wakeup_briefs: int
    retrieval_hint_artifacts: int
    retrieval_hint_source_items: int
    sample_source_ids: tuple[str, ...] = ()

    @property
    def warning_codes(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if sum(self.temporal_facts_by_status.values()) == 0:
            warnings.append("missing_temporal_fact_coverage")
        if self.contradiction_edges and self.contradiction_edges_with_derived_context == 0:
            warnings.append("contradictions_not_reflected_in_derived_context")
        if self.memory_dream_items and self.memory_dreams_with_contradiction_metrics == 0:
            warnings.append("memory_dreams_missing_contradiction_metrics")
        if self.wakeup_briefs and self.stale_wakeup_briefs:
            warnings.append("stale_wakeup_briefs_present")
        if self.retrieval_hint_artifacts == 0:
            warnings.append("missing_retrieval_hint_artifacts")
        return tuple(warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "temporal_facts_by_status": dict(sorted(self.temporal_facts_by_status.items())),
            "contradiction_edges": self.contradiction_edges,
            "contradiction_edges_with_derived_context": self.contradiction_edges_with_derived_context,
            "memory_dream_items": self.memory_dream_items,
            "memory_dreams_with_source_support": self.memory_dreams_with_source_support,
            "memory_dreams_with_contradiction_metrics": self.memory_dreams_with_contradiction_metrics,
            "wakeup_briefs": self.wakeup_briefs,
            "stale_wakeup_briefs": self.stale_wakeup_briefs,
            "retrieval_hint_artifacts": self.retrieval_hint_artifacts,
            "retrieval_hint_source_items": self.retrieval_hint_source_items,
            "sample_source_ids": list(self.sample_source_ids),
            "warning_codes": list(self.warning_codes),
        }


@dataclass(frozen=True)
class StaleMemoryAdjudicationReport:
    tenant_id: str
    fixture_case_count: int
    fixture_findings: tuple[StaleMemoryAdjudicationFinding, ...]
    existing_data_signals: ExistingMemoryAdjudicationSignals | None = None

    @property
    def ok(self) -> bool:
        existing_warnings = (
            self.existing_data_signals.warning_codes if self.existing_data_signals is not None else ()
        )
        return not self.fixture_findings and not existing_warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "ok": self.ok,
            "dry_run": True,
            "mutating": False,
            "fixture_case_count": self.fixture_case_count,
            "fixture_findings": [finding.to_dict() for finding in self.fixture_findings],
            "existing_data_signals": (
                self.existing_data_signals.to_dict() if self.existing_data_signals is not None else None
            ),
            "no_mutation_contract": {
                "deletes": False,
                "rewrites": False,
                "suppresses": False,
                "auto_fixes_canonical_memory": False,
                "prints_raw_memory_bodies": False,
            },
        }


def assess_memory_storage_item(item: Item) -> tuple[MemoryStorageQualityWarning, ...]:
    """Assess one memory-like item without mutating it or exposing body text."""

    metadata = item.metadata_ or {}
    memory_entry = metadata.get("memory_entry")
    warnings: list[MemoryStorageQualityWarning] = []

    def add(code: str, detail: str) -> None:
        warnings.append(
            MemoryStorageQualityWarning(
                code=code,
                item_id=str(item.id),
                detail=detail,
            )
        )

    if not isinstance(memory_entry, dict):
        add("missing_memory_entry_metadata", "note item has no memory_entry metadata")
        memory_entry = {}

    scope = memory_entry.get("scope")
    if not isinstance(scope, dict):
        add("missing_scope", "memory_entry scope is missing or malformed")
    else:
        scope_type = scope.get("type")
        scope_key = scope.get("key")
        if scope_type not in {"agent", "workspace", "session", "tenant_shared"}:
            add("invalid_scope_type", "memory_entry scope type is missing or unsupported")
        elif scope_type == "tenant_shared" and scope_key:
            add("ambiguous_tenant_shared_scope", "tenant_shared memory must not carry a scope key")
        elif scope_type != "tenant_shared" and not isinstance(scope_key, str):
            add("missing_scope_key", f"{scope_type} memory requires a stable scope key")

        expected_scope_tag = f"scope-{scope_type}" if isinstance(scope_type, str) else None
        if expected_scope_tag and expected_scope_tag not in (item.tags or []):
            add("missing_scope_tag", f"tags do not include {expected_scope_tag}")
        if isinstance(scope_key, str):
            expected_key_tag = f"{scope_type}-{scope_key}"
            if expected_key_tag not in (item.tags or []):
                add("missing_scope_key_tag", f"tags do not include {expected_key_tag}")

    if not memory_entry.get("source"):
        add("missing_source", "memory_entry source is missing")
    if not memory_entry.get("created_at"):
        add("missing_created_at", "memory_entry created_at is missing")
    if not item.source_url and not memory_entry.get("source_url"):
        add("missing_source_url", "memory has no source_url for provenance")

    metadata_idempotency = memory_entry.get("idempotency_key")
    if not item.idempotency_key:
        add("missing_item_idempotency_key", "item idempotency_key is missing")
    elif metadata_idempotency and metadata_idempotency != item.idempotency_key:
        add("idempotency_key_mismatch", "item and memory_entry idempotency keys differ")
    elif not metadata_idempotency:
        add("missing_metadata_idempotency_key", "memory_entry idempotency_key is missing")

    if item.title.strip().lower() in LOW_SIGNAL_TITLES:
        add("low_signal_title", "title is too generic for reliable recall")
    if not item.tags:
        add("missing_tags", "memory has no tags to support scoped retrieval")
    if not item.summary and len((item.raw_content or "").strip()) <= LOW_SIGNAL_BODY_MAX_CHARS:
        add("low_signal_memory", "memory has no summary and very short body")

    derived_keys = [key for key in MEMORY_DERIVED_METADATA_KEYS if isinstance(metadata.get(key), dict)]
    if derived_keys:
        _assess_derived_artifact(item, memory_entry, derived_keys, add)
    elif isinstance(memory_entry.get("metadata"), dict):
        nested = memory_entry["metadata"]
        nested_derived_keys = [key for key in MEMORY_DERIVED_METADATA_KEYS if isinstance(nested.get(key), dict)]
        if nested_derived_keys:
            add(
                "unlabeled_derived_artifact",
                "derived metadata is nested under memory_entry.metadata but missing a top-level derived marker",
            )

    return tuple(warnings)


def summarize_memory_storage_quality(
    *,
    tenant_id: str,
    items: Iterable[Item],
    duplicate_idempotency_keys: Iterable[dict[str, Any]] = (),
    sample_limit: int = 25,
    stale_memory_adjudication: StaleMemoryAdjudicationReport | None = None,
) -> MemoryStorageQualityReport:
    item_list = list(items)
    warnings = [warning for item in item_list for warning in assess_memory_storage_item(item)]
    counts = Counter(warning.code for warning in warnings)
    duplicate_rows = tuple(_sanitize_duplicate_row(row) for row in duplicate_idempotency_keys)
    return MemoryStorageQualityReport(
        tenant_id=tenant_id,
        item_count=len(item_list),
        warning_count=len(warnings),
        warnings_by_code=dict(counts),
        sample_warnings=tuple(warnings[: max(sample_limit, 0)]),
        duplicate_idempotency_keys=duplicate_rows,
        stale_memory_adjudication=stale_memory_adjudication,
    )


async def run_memory_storage_quality_report(
    db: AsyncSession,
    *,
    tenant_id: str,
    limit: int = 500,
    sample_limit: int = 25,
    include_adjudication: bool = False,
) -> MemoryStorageQualityReport:
    bounded_limit = max(1, min(limit, 5000))
    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.source_type == "note")
            .where(Item.deleted_at.is_(None))
            .order_by(Item.created_at.desc(), Item.id.desc())
            .limit(bounded_limit)
        )
    ).scalars().all()
    duplicate_rows = await _list_duplicate_idempotency_keys(db, tenant_id=tenant_id)
    adjudication = (
        await run_stale_memory_adjudication_report(db, tenant_id=tenant_id)
        if include_adjudication
        else None
    )
    return summarize_memory_storage_quality(
        tenant_id=tenant_id,
        items=rows,
        duplicate_idempotency_keys=duplicate_rows,
        sample_limit=sample_limit,
        stale_memory_adjudication=adjudication,
    )


def render_memory_storage_quality_report(report: MemoryStorageQualityReport) -> str:
    lines = [
        f"memory-storage-quality tenant={report.tenant_id} status={'pass' if report.ok else 'warn'}",
        f"items={report.item_count} warnings={report.warning_count} duplicate_idempotency_keys={len(report.duplicate_idempotency_keys)}",
    ]
    for code, count in sorted(report.warnings_by_code.items()):
        lines.append(f"[warn] {code}: {count}")
    for duplicate in report.duplicate_idempotency_keys[:10]:
        lines.append(
            "[warn] duplicate_idempotency_key: "
            f"fingerprint={duplicate['idempotency_key_fingerprint']} "
            f"count={duplicate['count']} sample_item_ids={duplicate['sample_item_ids']}"
        )
    for warning in report.sample_warnings:
        lines.append(f"[sample] {warning.code}: item={warning.item_id} detail={warning.detail}")
    if report.stale_memory_adjudication is not None:
        lines.extend(render_stale_memory_adjudication_report(report.stale_memory_adjudication))
    return "\n".join(lines)


def memory_storage_quality_report_to_json(report: MemoryStorageQualityReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def build_stale_memory_adjudication_fixture_pack() -> tuple[StaleMemoryAdjudicationCase, ...]:
    """Build synthetic stale/current memory cases without using production content."""

    def source_id(case_id: str, label: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"{ADJUDICATION_FIXTURE_NAMESPACE}:{case_id}:{label}"))

    updated_policy_current = source_id("updated-policy", "current")
    updated_policy_stale = source_id("updated-policy", "stale")
    implicit_conflict_current = source_id("implicit-conflict", "current")
    implicit_conflict_stale = source_id("implicit-conflict", "stale")
    downstream_policy_current = source_id("downstream-policy", "current")
    downstream_policy_stale = source_id("downstream-policy", "stale")
    return (
        StaleMemoryAdjudicationCase(
            case_id="updated-policy",
            current_source_id=updated_policy_current,
            stale_source_ids=(updated_policy_stale,),
            expected_superseded_links=(updated_policy_stale,),
            source_timestamps={
                updated_policy_stale: "2026-03-01T00:00:00+00:00",
                updated_policy_current: "2026-05-01T00:00:00+00:00",
            },
            surface_rankings={
                "memory_retrieve": (updated_policy_current, updated_policy_stale),
                "wakeup_context": (updated_policy_current, updated_policy_stale),
                "dream_hygiene": (updated_policy_current, updated_policy_stale),
            },
        ),
        StaleMemoryAdjudicationCase(
            case_id="implicit-conflict",
            current_source_id=implicit_conflict_current,
            stale_source_ids=(implicit_conflict_stale,),
            expected_superseded_links=(implicit_conflict_stale,),
            source_timestamps={
                implicit_conflict_stale: "2026-04-10T00:00:00+00:00",
                implicit_conflict_current: "2026-05-05T00:00:00+00:00",
            },
            surface_rankings={
                "memory_retrieve": (implicit_conflict_current, implicit_conflict_stale),
                "wakeup_context": (implicit_conflict_current, implicit_conflict_stale),
                "dream_hygiene": (implicit_conflict_current, implicit_conflict_stale),
            },
        ),
        StaleMemoryAdjudicationCase(
            case_id="stale-premise-query",
            current_source_id=downstream_policy_current,
            stale_source_ids=(downstream_policy_stale,),
            expected_superseded_links=(downstream_policy_stale,),
            source_timestamps={
                downstream_policy_stale: "2026-02-20T00:00:00+00:00",
                downstream_policy_current: "2026-05-09T00:00:00+00:00",
            },
            surface_rankings={
                "memory_retrieve": (downstream_policy_current, downstream_policy_stale),
                "wakeup_context": (downstream_policy_current, downstream_policy_stale),
                "dream_hygiene": (downstream_policy_current, downstream_policy_stale),
            },
        ),
    )


def score_stale_memory_adjudication_fixture(
    cases: Iterable[StaleMemoryAdjudicationCase],
) -> tuple[StaleMemoryAdjudicationFinding, ...]:
    findings: list[StaleMemoryAdjudicationFinding] = []
    for case in cases:
        stale_ids = set(case.stale_source_ids)
        for surface in CURRENT_FIRST_SURFACES:
            ranking = case.surface_rankings.get(surface, ())
            if not ranking:
                findings.append(
                    StaleMemoryAdjudicationFinding(
                        code="missing_surface_evidence",
                        case_id=case.case_id,
                        detail=f"{surface} did not return source ids",
                        source_ids=(case.current_source_id, *case.stale_source_ids),
                    )
                )
                continue
            if ranking[0] != case.current_source_id:
                findings.append(
                    StaleMemoryAdjudicationFinding(
                        code="current_fact_not_first",
                        case_id=case.case_id,
                        detail=f"{surface} did not rank the current source first",
                        source_ids=ranking[:3],
                    )
                )
            if stale_ids and not stale_ids.intersection(ranking):
                findings.append(
                    StaleMemoryAdjudicationFinding(
                        code="missing_superseded_source_lineage",
                        case_id=case.case_id,
                        detail=f"{surface} did not preserve superseded source lineage",
                        source_ids=(case.current_source_id, *case.stale_source_ids),
                    )
                )
        missing_links = tuple(
            source_id
            for source_id in case.expected_superseded_links
            if source_id not in case.stale_source_ids
        )
        if missing_links:
            findings.append(
                StaleMemoryAdjudicationFinding(
                    code="missing_expected_supersession_link",
                    case_id=case.case_id,
                    detail="expected superseded links are not represented in stale source ids",
                    source_ids=missing_links,
                )
            )
        if not case.dream_claims_need_source:
            findings.append(
                StaleMemoryAdjudicationFinding(
                    code="dream_artifact_masquerades_as_truth",
                    case_id=case.case_id,
                    detail="dream hygiene surface must require source support",
                    source_ids=(case.current_source_id,),
                )
            )
    return tuple(findings)


async def run_stale_memory_adjudication_report(
    db: AsyncSession,
    *,
    tenant_id: str,
    include_existing_data: bool = True,
) -> StaleMemoryAdjudicationReport:
    cases = build_stale_memory_adjudication_fixture_pack()
    existing_signals = (
        await collect_existing_memory_adjudication_signals(db, tenant_id=tenant_id)
        if include_existing_data
        else None
    )
    return StaleMemoryAdjudicationReport(
        tenant_id=tenant_id,
        fixture_case_count=len(cases),
        fixture_findings=score_stale_memory_adjudication_fixture(cases),
        existing_data_signals=existing_signals,
    )


async def collect_existing_memory_adjudication_signals(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> ExistingMemoryAdjudicationSignals:
    temporal_rows = (
        await db.execute(
            select(TemporalFact.status, func.count(TemporalFact.id))
            .where(TemporalFact.tenant_id == tenant_id)
            .group_by(TemporalFact.status)
        )
    ).all()
    temporal_counts = {str(status): int(count) for status, count in temporal_rows}

    contradiction_rows = await _list_contradiction_edges(db, tenant_id=tenant_id)
    contradiction_edges = len(contradiction_rows)
    derived_contradiction_edges = sum(
        1
        for _relationship, source, target in contradiction_rows
        if _is_derived_memory_context(source) or _is_derived_memory_context(target)
    )
    sample_ids: list[str] = []
    for _relationship, source, target in contradiction_rows[:5]:
        sample_ids.extend([str(source.id), str(target.id)])

    dream_rows = await _list_derived_items(db, tenant_id=tenant_id, metadata_key="memory_dream")
    wakeup_rows = await _list_derived_items(db, tenant_id=tenant_id, metadata_key="wakeup_brief")
    retrieval_hint_rows = (
        await db.execute(
            select(
                func.count(RetrievalHintArtifact.id),
                func.count(func.distinct(RetrievalHintArtifact.source_item_id)),
            ).where(RetrievalHintArtifact.tenant_id == tenant_id)
        )
    ).one()
    return ExistingMemoryAdjudicationSignals(
        temporal_facts_by_status=temporal_counts,
        contradiction_edges=contradiction_edges,
        contradiction_edges_with_derived_context=derived_contradiction_edges,
        memory_dream_items=len(dream_rows),
        memory_dreams_with_source_support=sum(1 for item in dream_rows if _dream_claims_need_source(item)),
        memory_dreams_with_contradiction_metrics=sum(1 for item in dream_rows if _dream_has_contradiction_metrics(item)),
        wakeup_briefs=len(wakeup_rows),
        stale_wakeup_briefs=sum(1 for item in wakeup_rows if _is_stale_wakeup_brief(item)),
        retrieval_hint_artifacts=int(retrieval_hint_rows[0] or 0),
        retrieval_hint_source_items=int(retrieval_hint_rows[1] or 0),
        sample_source_ids=tuple(dict.fromkeys(sample_ids)),
    )


def render_stale_memory_adjudication_report(report: StaleMemoryAdjudicationReport) -> list[str]:
    lines = [
        "stale-memory-adjudication "
        f"tenant={report.tenant_id} status={'pass' if report.ok else 'warn'} "
        f"fixture_cases={report.fixture_case_count} fixture_findings={len(report.fixture_findings)}"
    ]
    for finding in report.fixture_findings:
        lines.append(
            f"[warn] {finding.code}: case={finding.case_id} "
            f"source_ids={list(finding.source_ids)} detail={finding.detail}"
        )
    signals = report.existing_data_signals
    if signals is None:
        return lines
    lines.append(
        "existing-data "
        f"temporal_facts={sum(signals.temporal_facts_by_status.values())} "
        f"contradiction_edges={signals.contradiction_edges} "
        f"derived_contradiction_edges={signals.contradiction_edges_with_derived_context} "
        f"memory_dreams={signals.memory_dream_items} "
        f"dreams_with_source_support={signals.memory_dreams_with_source_support} "
        f"dreams_with_contradiction_metrics={signals.memory_dreams_with_contradiction_metrics} "
        f"wakeup_briefs={signals.wakeup_briefs} "
        f"stale_wakeup_briefs={signals.stale_wakeup_briefs} "
        f"retrieval_hint_artifacts={signals.retrieval_hint_artifacts}"
    )
    for code in signals.warning_codes:
        lines.append(f"[warn] {code}")
    if signals.sample_source_ids:
        lines.append(f"[sample] contradiction_source_ids={list(signals.sample_source_ids)}")
    return lines


def _sanitize_duplicate_row(row: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(row)
    raw_key = sanitized.pop("idempotency_key", None)
    if raw_key is not None and "idempotency_key_fingerprint" not in sanitized:
        sanitized["idempotency_key_fingerprint"] = hashlib.sha256(str(raw_key).encode()).hexdigest()
    return sanitized


def _assess_derived_artifact(
    item: Item,
    memory_entry: dict[str, Any],
    derived_keys: list[str],
    add,
) -> None:
    if len(derived_keys) > 1:
        add("ambiguous_derived_artifact", f"multiple derived metadata markers present: {', '.join(derived_keys)}")
    if "memory_dream" in derived_keys and "memory-dream" not in (item.categories or []):
        add("missing_derived_category", "memory_dream artifact is missing memory-dream category")

    if "memory_dream" in derived_keys:
        dream = item.metadata_["memory_dream"]
        required_fields = (
            "artifact_type",
            "schema_version",
            "source_item_ids",
            "source_digests",
            "source_job_ids",
            "palace_generation",
            "generated_at",
            "prompt_version",
            "model_version",
            "confidence",
            "claims_need_source",
        )
        missing = [field for field in required_fields if field not in dream]
        if missing:
            add("incomplete_derived_artifact_metadata", f"memory_dream is missing {', '.join(missing)}")
        if dream.get("claims_need_source") is not True:
            add("derived_artifact_masquerades_as_truth", "memory_dream must set claims_need_source=true")
        nested = memory_entry.get("metadata")
        nested_dream = nested.get("memory_dream") if isinstance(nested, dict) else None
        if not isinstance(nested_dream, dict) or nested_dream.get("artifact_type") != dream.get("artifact_type"):
            add("derived_artifact_missing_entry_label", "memory_entry.metadata does not label the derived artifact type")


async def _list_duplicate_idempotency_keys(db: AsyncSession, *, tenant_id: str) -> tuple[dict[str, Any], ...]:
    duplicate_keys = (
        await db.execute(
            select(Item.idempotency_key, func.count(Item.id))
            .where(Item.tenant_id == tenant_id)
            .where(Item.source_type == "note")
            .where(Item.deleted_at.is_(None))
            .where(Item.idempotency_key.is_not(None))
            .group_by(Item.idempotency_key)
            .having(func.count(Item.id) > 1)
            .order_by(func.count(Item.id).desc(), Item.idempotency_key.asc())
            .limit(25)
        )
    ).all()
    duplicates: list[dict[str, Any]] = []
    for idempotency_key, count in duplicate_keys:
        item_ids = (
            await db.execute(
                select(Item.id)
                .where(Item.tenant_id == tenant_id)
                .where(Item.idempotency_key == idempotency_key)
                .order_by(Item.created_at.desc(), Item.id.desc())
                .limit(5)
            )
        ).scalars().all()
        duplicates.append(
            {
                "idempotency_key_fingerprint": hashlib.sha256(str(idempotency_key).encode()).hexdigest(),
                "count": int(count),
                "sample_item_ids": [str(item_id) for item_id in item_ids],
            }
        )
    return tuple(duplicates)


async def _list_contradiction_edges(
    db: AsyncSession,
    *,
    tenant_id: str,
) -> tuple[tuple[ItemRelationship, Item, Item], ...]:
    source_item = aliased(Item)
    target_item = aliased(Item)
    rows = (
        await db.execute(
            select(ItemRelationship, source_item, target_item)
            .join(source_item, source_item.id == ItemRelationship.source_item_id)
            .join(target_item, target_item.id == ItemRelationship.target_item_id)
            .where(source_item.tenant_id == tenant_id)
            .where(target_item.tenant_id == tenant_id)
            .where(source_item.deleted_at.is_(None))
            .where(target_item.deleted_at.is_(None))
            .where(ItemRelationship.relationship == "contradicts")
            .order_by(ItemRelationship.created_at.desc(), ItemRelationship.id.desc())
            .limit(1000)
        )
    ).all()
    return tuple(rows)


async def _list_derived_items(
    db: AsyncSession,
    *,
    tenant_id: str,
    metadata_key: str,
) -> tuple[Item, ...]:
    rows = (
        await db.execute(
            select(Item)
            .where(Item.tenant_id == tenant_id)
            .where(Item.deleted_at.is_(None))
            .where(Item.metadata_.has_key(metadata_key))  # noqa: W601
            .order_by(Item.created_at.desc(), Item.id.desc())
            .limit(1000)
        )
    ).scalars().all()
    return tuple(rows)


def _is_derived_memory_context(item: Item) -> bool:
    metadata = item.metadata_ or {}
    memory_entry = metadata.get("memory_entry")
    nested_metadata = memory_entry.get("metadata") if isinstance(memory_entry, dict) else None
    tags = set(item.tags or [])
    return (
        any(isinstance(metadata.get(key), dict) for key in MEMORY_DERIVED_METADATA_KEYS)
        or (isinstance(nested_metadata, dict) and any(isinstance(nested_metadata.get(key), dict) for key in MEMORY_DERIVED_METADATA_KEYS))
        or bool(tags.intersection({"memory-dream", "wakeup-brief", "codex-local-memory"}))
    )


def _dream_claims_need_source(item: Item) -> bool:
    dream = (item.metadata_ or {}).get("memory_dream")
    return isinstance(dream, dict) and dream.get("claims_need_source") is True


def _dream_has_contradiction_metrics(item: Item) -> bool:
    dream = (item.metadata_ or {}).get("memory_dream")
    if not isinstance(dream, dict):
        return False
    for key in ("contradictions", "contradiction_count", "contradiction_metrics", "hygiene_metrics"):
        value = dream.get(key)
        if isinstance(value, int) and value > 0:
            return True
        if isinstance(value, dict) and any(_metric_value_is_positive(metric) for metric in value.values()):
            return True
    return False


def _is_stale_wakeup_brief(item: Item) -> bool:
    wakeup = (item.metadata_ or {}).get("wakeup_brief")
    if not isinstance(wakeup, dict):
        return False
    freshness = wakeup.get("freshness")
    if isinstance(freshness, dict) and freshness.get("stale") is True:
        return True
    if wakeup.get("stale") is True:
        return True
    expires_at = wakeup.get("expires_at")
    if isinstance(expires_at, str):
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.astimezone(timezone.utc) < datetime.now(timezone.utc)
    return False


def _metric_value_is_positive(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return False
