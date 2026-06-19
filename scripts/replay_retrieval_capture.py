#!/usr/bin/env python3
"""Compare Palace retrieval capture NDJSON files without mutating Palace data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.retrieval_replay import ReplayInputError, compare_captures, read_capture_file


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
        choices=["on", "off"],
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
    gate.add_argument("--required-capture-set", action="append", default=["retained-nist", "agent-memory"])
    gate.set_defaults(require_capture_metadata=True)
    gate.add_argument(
        "--require-current-source-ranking-mode",
        choices=["on", "off"],
        default=None,
        help="Fail unless every matched current capture reports this source-ranking mode.",
    )
    gate.add_argument("--output", default=None)
    gate.set_defaults(func=cmd_gate)
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
