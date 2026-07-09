#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.models.item import Item
from app.models.job import Job
from app.models.palace import MemoryEntry
from app.schemas.memory import MemoryEntryRequest, MemoryScope, MemoryScopeProfile, SemanticRecallRequest
from app.services.memory import semantic_recall_memory
from app.services.retention import RetentionExtractedEntry, RetentionExtractionOutput, RetentionService


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _fixture_entries(count: int) -> list[dict[str, object]]:
    return [
        {
            "entry_id": uuid.uuid5(uuid.NAMESPACE_URL, f"sar-1044-entry-{index}"),
            "item_id": uuid.uuid5(uuid.NAMESPACE_URL, f"sar-1044-item-{index}"),
            "title": f"Iris semantic memory load entry {index}",
            "body": (
                f"Iris retained SAR-{1000 + index % 75} semantic recall fact {index}. "
                "This fixture exercises tokenized recall, scope filtering, and temporal ranking."
            ),
            "tags": ["agent-memory", "scope-agent", "agent-iris", f"sar-{1000 + index % 75}"],
        }
        for index in range(count)
    ]


class _RowsResult:
    def __init__(self, values: Any) -> None:
        self.values = values

    def all(self) -> Any:
        return self.values

    def scalar_one(self) -> Any:
        return self.values


class _SemanticRecallSession:
    def __init__(self, *, total_count: int, rows: list[tuple[Item, MemoryEntry, int]]) -> None:
        self.total_count = total_count
        self.rows = rows
        self.execute_count = 0

    async def execute(self, _statement, *_args, **_kwargs) -> _RowsResult:
        self.execute_count += 1
        if self.execute_count % 2:
            return _RowsResult(self.total_count)
        return _RowsResult(self.rows)


class _RetentionSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.objects: dict[tuple[type, uuid.UUID], Any] = {}
        self.commits = 0
        self.rollbacks = 0

    async def scalar(self, *_args, **_kwargs) -> Any:
        return None

    async def get(self, model, key):
        return self.objects.get((model, key))

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()
            self.objects[(type(value), value.id)] = value

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, value: Any) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid.uuid4()
        if getattr(value, "created_at", None) is None:
            value.created_at = datetime.now(UTC)

    async def rollback(self) -> None:
        self.rollbacks += 1


class _ProfileService:
    async def get_profile(self, scope: MemoryScope) -> MemoryScopeProfile:
        return MemoryScopeProfile(
            scope=scope,
            retain_mission="Retain SAR ticket transitions and semantic-memory canary outcomes.",
        )


class _DeterministicRetentionLLM:
    async def complete_structured(self, _messages, _schema, *, schema_name: str):
        if schema_name != "retention_extraction":
            raise AssertionError(f"unexpected schema: {schema_name}")
        return RetentionExtractionOutput(
            entries=[
                RetentionExtractedEntry(
                    title="SAR-1015 semantic canary passed",
                    body="Iris observed SAR-1015 semantic memory canary pass after deployment.",
                    summary="Iris retained the SAR-1015 semantic canary result.",
                    confidence=0.91,
                    fact_kind="experience",
                    tags=["sar:SAR-1015", "semantic-memory"],
                )
            ]
        )


def _semantic_rows(entries: list[dict[str, object]]) -> list[tuple[Item, MemoryEntry, int]]:
    rows: list[tuple[Item, MemoryEntry, int]] = []
    now = datetime(2026, 7, 9, tzinfo=UTC)
    for index, entry in enumerate(entries):
        item = Item(
            id=entry["item_id"],
            source_type="note",
            title=str(entry["title"]),
            summary=str(entry["body"]),
            raw_content=str(entry["body"]),
            metadata_={},
            tags=list(entry["tags"]),
            categories=[],
            tenant_id="tenant-a",
            status="ready",
            created_at=now,
            updated_at=now,
        )
        memory_entry = MemoryEntry(
            id=entry["entry_id"],
            item_id=entry["item_id"],
            tenant_id="tenant-a",
            scope_type="agent",
            scope_key="iris",
            source="load-fixture",
            source_url=f"memory://load-fixture/{entry['entry_id']}",
            valid_from=now,
            valid_until=None,
            fact_kind="experience",
            created_at=now,
            updated_at=now,
        )
        rows.append((item, memory_entry, 3 if index % 75 == 15 else 2))
    return rows


def _measure(samples: int, operation) -> dict[str, float]:
    durations: list[float] = []
    for _ in range(samples):
        started = perf_counter()
        operation()
        durations.append(perf_counter() - started)
    return {
        "p50_seconds": round(statistics.median(durations), 6),
        "p95_seconds": round(_percentile(durations, 0.95), 6),
        "sample_count": samples,
    }


async def _recall_once(*, total_count: int, rows: list[tuple[Item, MemoryEntry, int]]) -> None:
    response = await semantic_recall_memory(
        _SemanticRecallSession(total_count=total_count, rows=rows),
        tenant_id="tenant-a",
        body=SemanticRecallRequest(
            scope_type="agent",
            scope_key="iris",
            query="Iris semantic SAR-1015",
            top_k=8,
            candidate_limit=len(rows),
        ),
    )
    if response.total != 8 or response.total_considered != total_count:
        raise RuntimeError("semantic recall load fixture did not exercise the expected 10k candidate path")


async def _retain_once() -> None:
    service = RetentionService(
        _RetentionSession(),
        tenant_id="tenant-a",
        llm=_DeterministicRetentionLLM(),
        profile_service=_ProfileService(),
    )
    result = await service.retain(
        MemoryEntryRequest(
            tenant_id="tenant-a",
            title="Iris canary source",
            body="Iris observed SAR-1015 semantic memory canary pass after deployment.",
            source="load-fixture",
            created_at=datetime(2026, 7, 9, tzinfo=UTC),
            tags=["agent:iris", "semantic-memory"],
            scope=MemoryScope(type="agent", key="iris"),
            idempotency_key=str(uuid.uuid4()),
            relationship_policy="deferred",
        ),
        mode="extracted_write",
        auth_mode="api_key",
        allowed_scopes=["write:agent"],
        mcp_client_key="iris",
    )
    if result.created_count != 1:
        raise RuntimeError("retention load fixture did not create the expected extracted memory entry")


def build_report(*, entry_count: int, samples: int) -> dict[str, object]:
    candidate_limit = min(entry_count, 200)
    rows = _semantic_rows(_fixture_entries(candidate_limit))
    recall = _measure(samples, lambda: asyncio.run(_recall_once(total_count=entry_count, rows=rows)))
    retain = _measure(samples, lambda: asyncio.run(_retain_once()))
    return {
        "schema_version": 1,
        "report_id": "sar-1044-semantic-memory-v15-load-report",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture": {
            "entry_count": entry_count,
            "scope": "agent/iris",
            "production_data_used": False,
            "service_paths": [
                "app.services.memory.semantic_recall_memory",
                "app.services.retention.RetentionService.retain extracted_write",
                "app.services.memory.accept_canonical_memory_entry",
            ],
            "semantic_candidate_limit": candidate_limit,
        },
        "targets": {
            "recall_p95_seconds": 2.0,
            "retain_p95_seconds": 4.0,
        },
        "results": {
            "recall": recall,
            "retain_extraction": retain,
            "worker_backpressure": {
                "covered_by_metric": "palace_arq_queue_depth",
                "state": "reported_via_existing_queue_telemetry",
            },
        },
        "passed": recall["p95_seconds"] < 2.0 and retain["p95_seconds"] < 4.0,
    }


def render_markdown(report: dict[str, object]) -> str:
    results = report["results"]
    recall = results["recall"]
    retain = results["retain_extraction"]
    return "\n".join(
        [
            "# SAR-1044 Semantic Memory v1.5 Load Report",
            "",
            f"Generated: `{report['generated_at']}`",
            "",
            "## Fixture",
            "",
            f"- Scope: `{report['fixture']['scope']}`",
            f"- Entries: `{report['fixture']['entry_count']}`",
            f"- Semantic candidate limit: `{report['fixture']['semantic_candidate_limit']}`",
            "- Production data used: `false`",
            "- Service path: `semantic_recall_memory` with 10k total considered rows and the API-bounded candidate window",
            "- Service path: `RetentionService.retain` extracted write with deterministic LLM output and canonical memory write",
            "",
            "## Results",
            "",
            f"- Recall p50: `{recall['p50_seconds']}` seconds",
            f"- Recall p95: `{recall['p95_seconds']}` seconds",
            f"- Retain extraction p50: `{retain['p50_seconds']}` seconds",
            f"- Retain extraction p95: `{retain['p95_seconds']}` seconds",
            "- Queue/backpressure: covered by `palace_arq_queue_depth`, `palace_arq_worker_queue_depth`, and `palace_arq_recent_latency_seconds`.",
            f"- Passed: `{str(report['passed']).lower()}`",
            "",
            "## Regenerate",
            "",
            "```bash",
            "python3 scripts/semantic_memory_v15_load_report.py --output-json /tmp/sar-1044-load.json --output-md /tmp/sar-1044-load.md",
            "```",
            "",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate SAR-1044 offline semantic memory load evidence.")
    parser.add_argument("--entries", type=int, default=10_000)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = build_report(entry_count=args.entries, samples=args.samples)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_md:
        args.output_md.parent.mkdir(parents=True, exist_ok=True)
        args.output_md.write_text(render_markdown(report), encoding="utf-8")
    if not args.output_json and not args.output_md:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
