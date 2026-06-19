#!/usr/bin/env python3
"""Preview or ingest Palace memory entries from local agent transcript files.

By default this tool never writes to Palace. The sweep and hook commands only
write when --write is set and Palace API credentials are configured.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.memory import MemoryScope  # noqa: E402
from app.services.transcript_ingestion import TranscriptIngestionConfig, sweep_transcripts  # noqa: E402
from app.services.transcript_sources import normalize_transcript_files, transcript_result_to_dry_run_json  # noqa: E402


def _scope(args: argparse.Namespace) -> MemoryScope:
    return MemoryScope.model_validate({"type": args.scope_type, "key": args.scope_key})


def _die(message: str, *, quiet: bool = False) -> NoReturn:
    if not quiet:
        print(message, file=sys.stderr)
    raise SystemExit(1)


def cmd_dry_run(args: argparse.Namespace) -> int:
    scope = _scope(args)
    result = normalize_transcript_files(
        args.paths,
        adapter=args.adapter,
        tenant_id=args.tenant_id,
        scope=scope,
        tags=args.tag,
        relationship_policy=args.relationship_policy,
        max_body_chars=args.max_body_chars,
    )
    payload = transcript_result_to_dry_run_json(result, include_body=args.include_body)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.records else 1


def cmd_sweep(args: argparse.Namespace) -> int:
    try:
        report = sweep_transcripts(
            args.paths,
            adapter=args.adapter,
            tenant_id=args.tenant_id,
            scope=_scope(args),
            tags=args.tag,
            relationship_policy=args.relationship_policy,
            max_body_chars=args.max_body_chars,
            glob_pattern=args.glob,
            write=args.write,
            lock_path=args.lock_file,
            config=TranscriptIngestionConfig.from_env(
                api_base_url=args.api_base_url,
                api_key=args.api_key,
                timeout_seconds=args.timeout_seconds,
            ),
        )
    except (RuntimeError, ValueError) as exc:
        _die(str(exc))
    print(json.dumps(report.to_json(include_records=args.include_records), indent=2, sort_keys=True))
    if report.write_error_count:
        return 2
    return 0 if report.record_count else 1


def cmd_hook(args: argparse.Namespace) -> int:
    # Hooks must not write to stdout: many agent clients reserve stdout for
    # JSON-RPC framing. Use --verbose for stderr diagnostics during setup only.
    paths = args.paths
    if not paths and args.path_env:
        paths = [Path(value) for value in args.path_env.split(args.path_separator) if value]
    if not paths:
        if args.verbose:
            print("No transcript paths configured; hook did nothing.", file=sys.stderr)
        return 0
    try:
        report = sweep_transcripts(
            paths,
            adapter=args.adapter,
            tenant_id=args.tenant_id,
            scope=_scope(args),
            tags=args.tag,
            relationship_policy=args.relationship_policy,
            max_body_chars=args.max_body_chars,
            glob_pattern=args.glob,
            write=args.write,
            lock_path=args.lock_file,
            config=TranscriptIngestionConfig.from_env(
                api_base_url=args.api_base_url,
                api_key=args.api_key,
                timeout_seconds=args.timeout_seconds,
            ),
        )
    except (RuntimeError, ValueError) as exc:
        if args.verbose:
            print(str(exc), file=sys.stderr)
        return 1
    if args.verbose:
        print(json.dumps(report.to_json(include_records=False), sort_keys=True), file=sys.stderr)
    if report.write_error_count:
        return 2
    return 0


def add_transcript_args(parser: argparse.ArgumentParser, *, paths_nargs: str) -> None:
    parser.add_argument("paths", nargs=paths_nargs, type=Path)
    parser.add_argument("--adapter", choices=("codex", "claude", "gemini"), required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--scope-type", default="agent", choices=("session", "agent", "workspace", "tenant_shared"))
    parser.add_argument("--scope-key")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--relationship-policy", choices=("immediate", "deferred", "skip"), default="deferred")
    parser.add_argument("--max-body-chars", type=int, default=20_000)


def add_write_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--write", action="store_true", help="Submit normalized records to Palace memory entries.")
    parser.add_argument("--api-base-url", help="Palace API base URL. Defaults to PALACEOFTRUTH_API_BASE_URL.")
    parser.add_argument("--api-key", help="Palace API key. Defaults to PALACEOFTRUTH_API_KEY.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--lock-file", type=Path, help="PID lock path used to avoid concurrent hook/sweeper writes.")
    parser.add_argument("--glob", default="*.jsonl", help="Glob used when a path argument is a directory.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    dry_run = sub.add_parser("dry-run", help="Normalize transcript files without writing to Palace.")
    add_transcript_args(dry_run, paths_nargs="+")
    dry_run.add_argument(
        "--include-body",
        action="store_true",
        help="Include raw transcript bodies in output. Default redacts bodies but preserves character counts.",
    )
    dry_run.set_defaults(func=cmd_dry_run)

    sweep = sub.add_parser("sweep", help="Scan transcript files or directories and optionally write Palace memory.")
    add_transcript_args(sweep, paths_nargs="+")
    add_write_args(sweep)
    sweep.add_argument(
        "--include-records",
        action="store_true",
        help="Include source ids and idempotency keys in the JSON report.",
    )
    sweep.set_defaults(func=cmd_sweep)

    hook = sub.add_parser("hook", help="Silent hook entrypoint for agent clients; never writes to stdout.")
    add_transcript_args(hook, paths_nargs="*")
    add_write_args(hook)
    hook.add_argument(
        "--path-env",
        default=os.getenv("PALACEOFTRUTH_TRANSCRIPT_PATHS", ""),
        help="Optional path list value, usually from PALACEOFTRUTH_TRANSCRIPT_PATHS.",
    )
    hook.add_argument("--path-separator", default=":")
    hook.add_argument("--verbose", action="store_true", help="Emit hook diagnostics to stderr.")
    hook.set_defaults(func=cmd_hook)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
