#!/usr/bin/env python3
"""Compare Palace retrieval capture NDJSON files without mutating Palace data."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.retrieval_replay import ReplayInputError, compare_captures, read_capture_file  # noqa: E402


class _FixtureEmbedder:
    def __init__(self) -> None:
        from app.embedding_profile import resolve_embedding_profile

        self.profile = resolve_embedding_profile()

    async def embed_single(self, _query: str) -> list[float]:
        return [0.1] * self.profile.dimensions


class _FixtureResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    def fetchall(self) -> list[SimpleNamespace]:
        return self.rows


class _FixtureDB:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    async def execute(self, _statement: object, params: dict | None = None) -> _FixtureResult:
        return _FixtureResult(self.rows if params is not None else [])


def _fixture_row(label: str, score: float, *, scope_type: str, scope_key: str | None = None,
                 superseded: bool = False) -> tuple[SimpleNamespace, uuid.UUID]:
    item_id = uuid.uuid5(uuid.NAMESPACE_URL, f"palace-currentness:{label}")
    superseded_by = uuid.uuid5(uuid.NAMESPACE_URL, f"palace-currentness:{label}:replacement") if superseded else None
    metadata = {"memory_entry": {"scope": {"type": scope_type, "key": scope_key}}}
    return SimpleNamespace(
        item_id=item_id, title=label.replace("-", " "), summary=None, source_type="note",
        source_url=None, tags=[], created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_date=None, effective_date_source="fixture", effective_date_quality="high",
        chunk_text=label.replace("-", " "), chunk_index=0, score=score,
        item_metadata=metadata, canonical_valid_until=None,
        canonical_superseded_by_entry_id=superseded_by,
    ), item_id


async def _generate_currentness_records() -> list[dict]:
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://fixture:fixture@localhost/fixture")
    os.environ.setdefault("OPENAI_API_KEY", "fixture-openai-key")
    os.environ.setdefault("OPENROUTER_API_KEY", "fixture-openrouter-key")
    os.environ.setdefault("API_KEY", "fixture-api-key")
    from app.services.search import SearchService

    scenarios = [
        ("latest-palace-deployment", "latest deploy current", "/api/v1/memory/retrieve", "workspace", "palaceoftruth",
         [("deploy-superseded", .9, True), ("deploy-current", .7, False)]),
        ("exact-version-0.1.481", "release 0.1.481", "/api/v1/memory/retrieve", "workspace", "palaceoftruth",
         [("release-0.1.480", .9, True), ("release-0.1.481", .65, False)]),
        ("strict-codex-scope", "codex memory", "/api/v1/memory/retrieve-agent", "agent", "codex",
         [("codex-memory", .8, False)]),
        ("evergreen-postgresql-docs", "postgresql current docs", "/api/v1/search", "tenant_shared", None,
         [("recent-unrelated", .69, True), ("postgresql-current-doc", .64, False)]),
    ]
    records = []
    for fingerprint, query, endpoint, scope_type, scope_key, specs in scenarios:
        rows, labels = [], {}
        for label, score, superseded in specs:
            row, item_id = _fixture_row(label, score, scope_type=scope_type, scope_key=scope_key, superseded=superseded)
            rows.append(row)
            labels[item_id] = label
        service = SearchService(_FixtureDB(rows), _FixtureEmbedder(), tenant_id="fixture")
        results = await service.vector_search(query=query, limit=5, scope_type=scope_type, scope_key=scope_key)
        records.append({
            "schema_version": 1,
            "capture": {"set": "currentness-regression", "corpus_id": "sar1060-currentness-v1",
                        "run_id": "generated", "source_ranking_mode": "currentness-aware"},
            "endpoint": endpoint, "tenant_id": "fixture", "status": "ok", "latency_ms": 0,
            "request": {"query_fingerprint": fingerprint, "limit": 5,
                        "scope": {"type": scope_type, "key": scope_key}, "tags": []},
            "fallback_used": endpoint.endswith("retrieve-agent"),
            **({"trace": {
                "authorized_agent_scope_keys": ["codex"],
                "denied_agent_scope_keys": [],
                "result_counts_by_scope": {"agent/codex": len(results)},
                "selected_scope_fallback_used": True,
                "selected_scope_completeness_warnings": [],
                "broad_corpus_searched": False,
                "broad_result_count": 0,
                "fallback_used": True,
            }} if endpoint.endswith("retrieve-agent") else {}),
            "results": [{"rank": rank, "item_id": labels[result.item_id],
                         "retrieved_scope_label": result.retrieved_scope_label,
                         "currentness": result.currentness}
                        for rank, result in enumerate(results, start=1)],
        })
    return records


def cmd_generate_currentness(args: argparse.Namespace) -> int:
    records = asyncio.run(_generate_currentness_records())
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records), encoding="utf-8")
    return 0


def _write_report(report: dict, output: str | None) -> None:
    payload = json.dumps(report, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


def _run_comparison(args: argparse.Namespace) -> dict:
    baseline = read_capture_file(Path(args.baseline))
    current = read_capture_file(Path(args.current))
    return compare_captures(
        baseline,
        current,
        top_k=args.top_k,
        latency_delta_warn_ms=args.latency_delta_warn_ms,
        min_jaccard=args.min_jaccard,
        min_recall=args.min_recall,
        min_mrr=args.min_mrr,
        min_ndcg=args.min_ndcg,
        fail_on_forbidden=args.fail_on_forbidden,
        require_expected_scope=args.require_expected_scope,
        require_expected_route=args.require_expected_route,
        required_capture_sets=set(args.required_capture_set or []),
        require_capture_metadata=args.require_capture_metadata,
        require_current_source_ranking_mode=args.require_current_source_ranking_mode,
    )


def cmd_compare(args: argparse.Namespace) -> int:
    report = _run_comparison(args)
    _write_report(report, args.output)
    return 1 if report["summary"]["failure_counts"] else 0


def cmd_gate(args: argparse.Namespace) -> int:
    report = _run_comparison(args)
    _write_report(report, args.output)
    return 1 if report["summary"]["failure_counts"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    compare = sub.add_parser("compare", help="Compare baseline and current retrieval capture files.")
    compare.add_argument("--baseline", required=True, help="Baseline capture NDJSON path.")
    compare.add_argument("--current", required=True, help="Current capture NDJSON path.")
    compare.add_argument("--top-k", type=int, default=5)
    compare.add_argument("--latency-delta-warn-ms", type=float, default=500.0)
    compare.add_argument("--min-jaccard", type=float, default=0.0)
    compare.add_argument("--min-recall", type=float, default=None)
    compare.add_argument("--min-mrr", type=float, default=None)
    compare.add_argument("--min-ndcg", type=float, default=None)
    compare.add_argument("--fail-on-forbidden", action="store_true")
    compare.add_argument("--require-expected-scope", action="store_true")
    compare.add_argument("--require-expected-route", action="store_true")
    compare.add_argument("--required-capture-set", action="append", default=[])
    compare.add_argument("--require-capture-metadata", action="store_true")
    compare.add_argument(
        "--require-current-source-ranking-mode",
        choices=["on", "off", "currentness-aware"],
        default=None,
        help="Fail unless every matched current capture reports this source-ranking mode.",
    )
    compare.add_argument("--output", default=None)
    compare.set_defaults(func=cmd_compare)
    gate = sub.add_parser("gate", help="Run the pre-merge retrieval replay gate.")
    gate.add_argument("--baseline", required=True, help="Baseline capture NDJSON path.")
    gate.add_argument("--current", required=True, help="Current capture NDJSON path.")
    gate.add_argument("--top-k", type=int, default=5)
    gate.add_argument("--latency-delta-warn-ms", type=float, default=500.0)
    gate.add_argument("--min-jaccard", type=float, default=0.8)
    gate.add_argument("--min-recall", type=float, default=1.0)
    gate.add_argument("--min-mrr", type=float, default=0.5)
    gate.add_argument("--min-ndcg", type=float, default=0.8)
    gate.add_argument("--allow-forbidden-hits", dest="fail_on_forbidden", action="store_false")
    gate.set_defaults(fail_on_forbidden=True)
    gate.add_argument("--allow-scope-mismatch", dest="require_expected_scope", action="store_false")
    gate.set_defaults(require_expected_scope=True)
    gate.add_argument("--allow-route-mismatch", dest="require_expected_route", action="store_false")
    gate.set_defaults(require_expected_route=True)
    gate.add_argument(
        "--required-capture-set",
        action="append",
        default=["retained-nist", "agent-memory"],
    )
    gate.set_defaults(require_capture_metadata=True)
    gate.add_argument(
        "--require-current-source-ranking-mode",
        choices=["on", "off", "currentness-aware"],
        default=None,
        help="Fail unless every matched current capture reports this source-ranking mode.",
    )
    gate.add_argument("--output", default=None)
    gate.set_defaults(func=cmd_gate)
    generate = sub.add_parser("generate-currentness", help="Generate deterministic currentness capture via SearchService.")
    generate.add_argument("--output", required=True)
    generate.set_defaults(func=cmd_generate_currentness)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ReplayInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
