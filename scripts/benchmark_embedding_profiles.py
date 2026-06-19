#!/usr/bin/env python3
"""Report-only comparison of OpenAI and local embedding-profile retrieval captures."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.embedding_profile_eval import (  # noqa: E402
    EmbeddingProfileEvalInputError,
    build_native_image_provider_capture_report,
    compare_embedding_profiles,
    materialize_live_capture_pack,
    parse_profile_metadata,
    read_profile_captures,
)


def _write_report(report: dict, output: str | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if output:
        Path(output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)


def cmd_compare(args: argparse.Namespace) -> int:
    return _run_compare(args)


def _run_compare(args: argparse.Namespace) -> int:
    metadata = parse_profile_metadata(args.profile_metadata)
    profiles = read_profile_captures(args.profile, profile_metadata=metadata)
    report = compare_embedding_profiles(
        profiles,
        baseline_profile=args.baseline_profile,
        top_k=args.top_k,
        latency_delta_warn_ms=args.latency_delta_warn_ms,
        min_recall=args.min_recall,
        min_precision=args.min_precision,
        min_mrr=args.min_mrr,
        min_ndcg=args.min_ndcg,
        max_top1_change_rate=args.max_top1_change_rate,
    )
    _write_report(report, args.output)
    return 0 if report["summary"]["passed"] else 1


def cmd_materialize_live_captures(args: argparse.Namespace) -> int:
    materialized = materialize_live_capture_pack(Path(args.pack), Path(args.output_dir))
    print(json.dumps(materialized.manifest, indent=2, sort_keys=True))
    return 0


def cmd_live_pack(args: argparse.Namespace) -> int:
    materialized = materialize_live_capture_pack(Path(args.pack), Path(args.output_dir))
    args.profile = materialized.profile_specs
    args.profile_metadata = materialized.profile_metadata_specs
    return _run_compare(args)


def _embedding_service_factory():
    from app.services.embedder import EmbeddingService

    return EmbeddingService()


async def _capture_native_image_provider(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    async with _embedding_service_factory() as embedder:
        if embedder.profile.profile_name != args.profile_name:
            raise EmbeddingProfileEvalInputError(
                f"active embedding profile {embedder.profile.profile_name!r} does not match "
                f"--profile-name {args.profile_name!r}"
            )
        vectors = await embedder.embed_image_references(args.image_reference)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 4)
    return build_native_image_provider_capture_report(
        profile_name=args.profile_name,
        image_references=args.image_reference,
        vectors=vectors,
        latency_ms=elapsed_ms,
    )


def cmd_native_image_provider_capture(args: argparse.Namespace) -> int:
    report = asyncio.run(_capture_native_image_provider(args))
    _write_report(report, args.output)
    return 0 if report["readiness"]["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    compare = sub.add_parser("compare", help="Compare retrieval captures across embedding profiles.")
    compare.add_argument(
        "--profile",
        action="append",
        required=True,
        help="Profile capture as name=path. Repeat for OpenAI baseline and local candidates.",
    )
    compare.add_argument(
        "--profile-metadata",
        action="append",
        default=[],
        help="Optional profile metadata as name=metadata.json, including provider/model/dimensions/service config.",
    )
    compare.add_argument("--baseline-profile", default="openai")
    compare.add_argument("--top-k", type=int, default=5)
    compare.add_argument("--latency-delta-warn-ms", type=float, default=500.0)
    compare.add_argument("--min-recall", type=float, default=None)
    compare.add_argument("--min-precision", type=float, default=None)
    compare.add_argument("--min-mrr", type=float, default=None)
    compare.add_argument("--min-ndcg", type=float, default=None)
    compare.add_argument("--max-top1-change-rate", type=float, default=None)
    compare.add_argument("--output", default=None)
    compare.set_defaults(func=cmd_compare)

    materialize = sub.add_parser(
        "materialize-live-captures",
        help="Render a JSON live-capture pack into replay NDJSON and metadata files.",
    )
    materialize.add_argument("--pack", required=True, help="Path to a live-capture pack JSON file.")
    materialize.add_argument("--output-dir", required=True, help="Directory for generated NDJSON and manifest files.")
    materialize.set_defaults(func=cmd_materialize_live_captures)

    live_pack = sub.add_parser(
        "live-pack",
        help="Materialize a live-capture pack and compare the resulting profile captures.",
    )
    live_pack.add_argument("--pack", required=True, help="Path to a live-capture pack JSON file.")
    live_pack.add_argument("--output-dir", required=True, help="Directory for generated NDJSON and manifest files.")
    live_pack.add_argument("--baseline-profile", default="text-description")
    live_pack.add_argument("--top-k", type=int, default=5)
    live_pack.add_argument("--latency-delta-warn-ms", type=float, default=500.0)
    live_pack.add_argument("--min-recall", type=float, default=None)
    live_pack.add_argument("--min-precision", type=float, default=None)
    live_pack.add_argument("--min-mrr", type=float, default=None)
    live_pack.add_argument("--min-ndcg", type=float, default=None)
    live_pack.add_argument("--max-top1-change-rate", type=float, default=None)
    live_pack.add_argument("--output", default=None)
    live_pack.set_defaults(func=cmd_live_pack, profile=[], profile_metadata=[])

    native_capture = sub.add_parser(
        "native-image-provider-capture",
        help="Call the active native-image provider and write a report-only capture summary.",
    )
    native_capture.add_argument(
        "--profile-name",
        default="local-http-clip-native-image-768",
        help="Disabled-by-default native image profile expected in the active environment.",
    )
    native_capture.add_argument(
        "--image-reference",
        action="append",
        required=True,
        help="Provider-specific image reference. Repeat for each test image; no vectors are stored.",
    )
    native_capture.add_argument("--output", default=None)
    native_capture.set_defaults(func=cmd_native_image_provider_capture)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EmbeddingProfileEvalInputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
