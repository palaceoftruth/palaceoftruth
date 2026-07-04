#!/usr/bin/env python3
"""Evaluate sanitized route-aware Palace agent-memory retrieval packs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.agent_memory_eval import (  # noqa: E402
    AgentMemoryEvalInputError,
    AgentMemoryEvalThresholds,
    LexicalOverlapReranker,
    StaticScoreReranker,
    build_live_retrieval_request,
    case_from_live_response,
    evaluate_eval_pack,
    evaluate_reranker_ablation,
    read_compatibility_fixture_eval_pack,
    read_eval_pack,
    read_public_benchmark_eval_pack,
)

DEFAULT_PACK = REPO_ROOT / "backend" / "tests" / "fixtures" / "agent_memory_eval_pack.json"
DEFAULT_COMPATIBILITY_PACK = (
    REPO_ROOT / "backend" / "tests" / "fixtures" / "agent_memory_compatibility_fixture_pack.json"
)
DEFAULT_LIVE_ENDPOINT = "/api/v1/memory/retrieve-agent"


PUBLIC_ARTIFACT_PROFILES: dict[str, dict[str, object]] = {
    "mempalace-longmemeval-raw": {
        "suite": "longmemeval",
        "pack_id": "mempalace-longmemeval-raw-public-report",
        "artifact_metadata": {
            "competitor": "MemPalace",
            "source_url": "https://mempalace.github.io/mempalace/reference/benchmarks",
            "dataset": "LongMemEval-S",
            "mode": "Raw ChromaDB",
            "llm_required": False,
            "comparison_note": "Published retrieval-only R@5 score; suitable for Palace public-report target comparison on LongMemEval-formatted rows.",
        },
        "benchmark_targets": {
            "mempalace_raw_longmemeval_r5": {
                "metric": "R@5",
                "value": 0.966,
            }
        },
    },
    "mempalace-membench-raw": {
        "suite": "membench",
        "pack_id": "mempalace-membench-raw-public-report",
        "artifact_metadata": {
            "competitor": "MemPalace",
            "source_url": "https://mempalace.github.io/mempalace/reference/benchmarks",
            "dataset": "MemBench",
            "mode": "Raw retrieval",
            "comparison_note": "Published MemBench overall R@5 score; use only with MemBench-shaped rows.",
        },
        "benchmark_targets": {
            "mempalace_raw_membench_r5": {
                "metric": "R@5",
                "value": 0.803,
            }
        },
    },
    "gbrain-rich-prose": {
        "suite": "membench",
        "pack_id": "gbrain-rich-prose-public-report",
        "artifact_metadata": {
            "competitor": "GBrain",
            "source_url": "https://github.com/garrytan/gbrain",
            "dataset": "GBrain 240-page Opus-generated rich-prose corpus",
            "comparison_note": "GBrain's published P@5/R@5 values are from a project-specific corpus, not a standard public LongMemEval/LoCoMo/ConvoMem/MemBench run. Keep this profile report-only and do not present it as an apples-to-apples public benchmark.",
        },
        "benchmark_targets": {
            "gbrain_rich_prose_p5": {
                "metric": "P@5",
                "value": 0.491,
            },
            "gbrain_rich_prose_r5": {
                "metric": "R@5",
                "value": 0.979,
            },
        },
    },
}


def build_thresholds(args: argparse.Namespace) -> AgentMemoryEvalThresholds:
    return AgentMemoryEvalThresholds(
        recall_at_k=args.min_recall_at_k,
        precision_at_k=args.min_precision_at_k,
        mrr=args.min_mrr,
        ndcg_at_k=args.min_ndcg_at_k,
        provenance_label_accuracy=args.min_provenance_label_accuracy,
        forbidden_hit_count=args.max_forbidden_hits,
    )


def cmd_report(args: argparse.Namespace) -> int:
    payload = read_eval_pack(Path(args.pack))
    report = evaluate_eval_pack(payload, top_k=args.top_k, thresholds=build_thresholds(args))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if report["summary"]["passed"] else 1


def cmd_compatibility_report(args: argparse.Namespace) -> int:
    payload = read_compatibility_fixture_eval_pack(Path(args.pack))
    report = evaluate_eval_pack(payload, top_k=args.top_k, thresholds=build_thresholds(args))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output_pack:
        Path(args.output_pack).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if report["summary"]["passed"] else 1


def _parse_json_arg(value: str | None, *, label: str) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AgentMemoryEvalInputError(f"{label} must be JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise AgentMemoryEvalInputError(f"{label} must be a JSON object")
    return parsed


def _public_profile(name: str | None) -> dict[str, object]:
    if not name:
        return {}
    try:
        return PUBLIC_ARTIFACT_PROFILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(PUBLIC_ARTIFACT_PROFILES))
        raise AgentMemoryEvalInputError(
            f"--artifact-profile must be one of: {known}"
        ) from exc


def _merge_dicts(base: object, override: dict) -> dict:
    merged: dict = {}
    if isinstance(base, dict):
        merged.update(base)
    merged.update(override)
    return merged


def _public_pack_from_args(args: argparse.Namespace) -> dict:
    profile = _public_profile(getattr(args, "artifact_profile", None))
    suite = args.suite
    if not suite:
        suite = str(profile.get("suite") or "")
    if not suite:
        raise AgentMemoryEvalInputError("--suite is required without --artifact-profile")
    profile_suite = profile.get("suite")
    if profile_suite and suite != profile_suite:
        raise AgentMemoryEvalInputError(
            f"--artifact-profile {args.artifact_profile} requires --suite {profile_suite}"
        )
    artifact_metadata = _merge_dicts(
        profile.get("artifact_metadata"),
        _parse_json_arg(args.artifact_metadata, label="--artifact-metadata"),
    )
    benchmark_targets = _merge_dicts(
        profile.get("benchmark_targets"),
        _parse_json_arg(args.benchmark_targets, label="--benchmark-targets"),
    )
    return read_public_benchmark_eval_pack(
        Path(args.input),
        suite=suite,
        pack_id=args.pack_id or profile.get("pack_id"),
        artifact_metadata=artifact_metadata,
        benchmark_targets=benchmark_targets,
    )


def cmd_convert_public(args: argparse.Namespace) -> int:
    pack = _public_pack_from_args(args)
    rendered = json.dumps(pack, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def cmd_public_report(args: argparse.Namespace) -> int:
    pack = _public_pack_from_args(args)
    report = evaluate_eval_pack(pack, top_k=args.top_k, thresholds=build_thresholds(args))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    if args.output_pack:
        Path(args.output_pack).write_text(
            json.dumps(pack, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0 if report["summary"]["passed"] else 1


def _rerankers_from_args(args: argparse.Namespace) -> list:
    rerankers = []
    for name in args.reranker or ["lexical-overlap"]:
        if name == "lexical-overlap":
            rerankers.append(LexicalOverlapReranker())
            continue
        if name.startswith("static-json:"):
            path = Path(name.split(":", 1)[1])
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise AgentMemoryEvalInputError(f"cannot read static reranker scores: {path}") from exc
            except json.JSONDecodeError as exc:
                raise AgentMemoryEvalInputError(
                    f"static reranker score file must be JSON: {exc.msg}"
                ) from exc
            if not isinstance(payload, dict):
                raise AgentMemoryEvalInputError("static reranker score file must be a JSON object")
            scores = {
                str(item_id): float(score)
                for item_id, score in payload.items()
                if isinstance(score, (int, float)) and not isinstance(score, bool)
            }
            rerankers.append(StaticScoreReranker(name=f"static-json:{path.name}", scores=scores))
            continue
        raise AgentMemoryEvalInputError(
            "--reranker must be lexical-overlap or static-json:<path>"
        )
    return rerankers


def cmd_reranker_ablation(args: argparse.Namespace) -> int:
    payload = read_eval_pack(Path(args.pack))
    report = evaluate_reranker_ablation(
        payload,
        rerankers=_rerankers_from_args(args),
        top_k=args.top_k,
        candidate_limit=args.candidate_limit,
        thresholds=build_thresholds(args),
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


def cmd_live_report(args: argparse.Namespace) -> int:
    _validate_live_base_url(args.base_url, allow_remote=args.allow_remote)
    source_pack = read_eval_pack(Path(args.pack))
    live_cases: list[dict] = []
    headers = _auth_headers(args)
    timeout = httpx.Timeout(args.timeout)
    with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=timeout) as client:
        for case in source_pack["cases"]:
            endpoint = case.get("endpoint") or args.endpoint
            request_payload = build_live_retrieval_request(
                case,
                endpoint=endpoint,
                top_k=args.top_k,
                candidate_limit=args.candidate_limit,
                broad_candidate_limit=args.broad_candidate_limit,
                display_limit=args.display_limit,
            )
            started = time.perf_counter()
            try:
                response = client.post(endpoint, json=request_payload)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise AgentMemoryEvalInputError(
                    f"{case['id']}: live retrieval failed for {endpoint}: {exc}"
                ) from exc
            live_cases.append(
                case_from_live_response(
                    case,
                    endpoint=endpoint,
                    request_payload=request_payload,
                    response_payload=response.json(),
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
            )

    live_pack = {
        key: value
        for key, value in source_pack.items()
        if key not in {"cases", "description"}
    }
    live_pack.update(
        {
            "description": f"Live retrieval report for {source_pack.get('description') or source_pack.get('pack_id')}",
            "cases": live_cases,
        }
    )
    if args.output_live_pack:
        Path(args.output_live_pack).write_text(
            json.dumps(live_pack, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    report = evaluate_eval_pack(live_pack, top_k=args.top_k, thresholds=build_thresholds(args))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if report["summary"]["passed"] else 1


def _validate_live_base_url(base_url: str, *, allow_remote: bool) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AgentMemoryEvalInputError("--base-url must be an absolute http(s) URL")
    host = parsed.hostname or ""
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    is_local = host in local_hosts or host.endswith(".test")
    if not is_local and not allow_remote:
        raise AgentMemoryEvalInputError(
            "live retrieval against remote URLs requires --allow-remote"
        )


def _auth_headers(args: argparse.Namespace) -> dict[str, str]:
    api_key = (
        args.api_key
        or os.environ.get("PALACEOFTRUTH_API_KEY")
        or os.environ.get("SECONDBRAIN_API_KEY")
        or ""
    ).strip()
    token = (args.token or os.environ.get("PALACE_MEMORY_TOKEN") or "").strip()
    if api_key:
        return {"X-API-Key": api_key, "X-MCP-Scope": "read", "X-MCP-Scopes": "read"}
    if token:
        return {"Authorization": f"Bearer {token}"}
    raise AgentMemoryEvalInputError(
        "--api-key, PALACEOFTRUTH_API_KEY, SECONDBRAIN_API_KEY, or --token is required"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="Print a report-only scoped memory eval summary.")
    report.add_argument("--pack", default=str(DEFAULT_PACK), help="Eval-pack JSON path.")
    report.add_argument("--top-k", type=int, default=5)
    report.add_argument("--min-recall-at-k", type=float, default=0.8)
    report.add_argument("--min-precision-at-k", type=float, default=0.1)
    report.add_argument("--min-mrr", type=float, default=0.8)
    report.add_argument("--min-ndcg-at-k", type=float, default=0.8)
    report.add_argument("--min-provenance-label-accuracy", type=float, default=1.0)
    report.add_argument("--max-forbidden-hits", type=int, default=0)
    report.add_argument("--output", default=None)
    report.set_defaults(func=cmd_report)

    compatibility = sub.add_parser(
        "compatibility-report",
        help="Normalize an offline REST/MCP/Hermes compatibility fixture pack and print an eval report.",
    )
    compatibility.add_argument(
        "--pack",
        default=str(DEFAULT_COMPATIBILITY_PACK),
        help="Compatibility fixture-pack JSON path.",
    )
    compatibility.add_argument("--top-k", type=int, default=5)
    compatibility.add_argument("--min-recall-at-k", type=float, default=0.8)
    compatibility.add_argument("--min-precision-at-k", type=float, default=0.1)
    compatibility.add_argument("--min-mrr", type=float, default=0.8)
    compatibility.add_argument("--min-ndcg-at-k", type=float, default=0.8)
    compatibility.add_argument("--min-provenance-label-accuracy", type=float, default=1.0)
    compatibility.add_argument("--max-forbidden-hits", type=int, default=0)
    compatibility.add_argument("--output", default=None)
    compatibility.add_argument(
        "--output-pack",
        default=None,
        help="Optional path for the normalized eval pack generated from compatibility fixtures.",
    )
    compatibility.set_defaults(func=cmd_compatibility_report)

    convert_public = sub.add_parser(
        "convert-public",
        help="Convert public memory benchmark rows into the Palace eval-pack format.",
    )
    convert_public.add_argument(
        "--suite",
        required=False,
        choices=["longmemeval", "locomo", "convomem", "membench"],
    )
    convert_public.add_argument(
        "--artifact-profile",
        choices=sorted(PUBLIC_ARTIFACT_PROFILES),
        default=None,
        help="Named report-only public artifact metadata and benchmark target preset.",
    )
    convert_public.add_argument("--input", required=True, help="JSON or JSONL public benchmark rows.")
    convert_public.add_argument("--pack-id", default=None)
    convert_public.add_argument(
        "--artifact-metadata",
        default=None,
        help="JSON object with reproducibility metadata such as dataset revision and adapter version.",
    )
    convert_public.add_argument(
        "--benchmark-targets",
        default=None,
        help="JSON object of named target comparisons, for example {'mempalace_raw': {'metric':'R@5','value':0.966}}.",
    )
    convert_public.add_argument("--output", default=None)
    convert_public.set_defaults(func=cmd_convert_public)

    public_report = sub.add_parser(
        "public-report",
        help="Convert public benchmark rows and print a Palace retrieval metric report.",
    )
    public_report.add_argument(
        "--suite",
        required=False,
        choices=["longmemeval", "locomo", "convomem", "membench"],
    )
    public_report.add_argument(
        "--artifact-profile",
        choices=sorted(PUBLIC_ARTIFACT_PROFILES),
        default=None,
        help="Named report-only public artifact metadata and benchmark target preset.",
    )
    public_report.add_argument("--input", required=True, help="JSON or JSONL public benchmark rows.")
    public_report.add_argument("--pack-id", default=None)
    public_report.add_argument("--artifact-metadata", default=None)
    public_report.add_argument("--benchmark-targets", default=None)
    public_report.add_argument("--top-k", type=int, default=5)
    public_report.add_argument("--min-recall-at-k", type=float, default=0.8)
    public_report.add_argument("--min-precision-at-k", type=float, default=0.1)
    public_report.add_argument("--min-mrr", type=float, default=0.8)
    public_report.add_argument("--min-ndcg-at-k", type=float, default=0.8)
    public_report.add_argument("--min-provenance-label-accuracy", type=float, default=1.0)
    public_report.add_argument("--max-forbidden-hits", type=int, default=0)
    public_report.add_argument("--output", default=None)
    public_report.add_argument("--output-pack", default=None)
    public_report.set_defaults(func=cmd_public_report)

    ablation = sub.add_parser(
        "reranker-ablation",
        help="Run offline second-stage reranker ablations against an eval pack.",
    )
    ablation.add_argument("--pack", default=str(DEFAULT_PACK), help="Eval-pack JSON path.")
    ablation.add_argument("--top-k", type=int, default=5)
    ablation.add_argument("--candidate-limit", type=int, default=20)
    ablation.add_argument(
        "--reranker",
        action="append",
        default=None,
        help="Reranker to run. Use lexical-overlap or static-json:<path>. Repeatable.",
    )
    ablation.add_argument("--min-recall-at-k", type=float, default=0.8)
    ablation.add_argument("--min-precision-at-k", type=float, default=0.1)
    ablation.add_argument("--min-mrr", type=float, default=0.8)
    ablation.add_argument("--min-ndcg-at-k", type=float, default=0.8)
    ablation.add_argument("--min-provenance-label-accuracy", type=float, default=1.0)
    ablation.add_argument("--max-forbidden-hits", type=int, default=0)
    ablation.add_argument("--output", default=None)
    ablation.set_defaults(func=cmd_reranker_ablation)

    live = sub.add_parser(
        "live-report",
        help="Run read-only retrieval calls, normalize live results, and print an eval report.",
    )
    live.add_argument("--pack", default=str(DEFAULT_PACK), help="Eval-pack JSON path.")
    live.add_argument("--base-url", required=True, help="Palace origin, for example https://palaceoftruth.test.")
    live.add_argument("--api-key", default=None, help="API key. Defaults to PALACEOFTRUTH_API_KEY or SECONDBRAIN_API_KEY.")
    live.add_argument("--token", default=None, help="Optional bearer token. API key auth is preferred for local scripts.")
    live.add_argument("--endpoint", default=DEFAULT_LIVE_ENDPOINT, choices=[
        "/api/v1/memory/retrieve",
        "/api/v1/memory/retrieve-agent",
    ])
    live.add_argument("--top-k", type=int, default=5)
    live.add_argument("--candidate-limit", type=int, default=20)
    live.add_argument("--broad-candidate-limit", type=int, default=None)
    live.add_argument("--display-limit", type=int, default=5)
    live.add_argument("--min-recall-at-k", type=float, default=0.8)
    live.add_argument("--min-precision-at-k", type=float, default=0.1)
    live.add_argument("--min-mrr", type=float, default=0.8)
    live.add_argument("--min-ndcg-at-k", type=float, default=0.8)
    live.add_argument("--min-provenance-label-accuracy", type=float, default=1.0)
    live.add_argument("--max-forbidden-hits", type=int, default=0)
    live.add_argument("--timeout", type=float, default=15.0)
    live.add_argument("--allow-remote", action="store_true")
    live.add_argument("--output", default=None)
    live.add_argument("--output-live-pack", default=None)
    live.set_defaults(func=cmd_live_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AgentMemoryEvalInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
