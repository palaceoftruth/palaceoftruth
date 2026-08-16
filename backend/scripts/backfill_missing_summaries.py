#!/usr/bin/env python3
"""Run a bounded summary-only backfill for ready media and web items."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_MODEL = "minimax/minimax-m2.7"
DEFAULT_SOURCE_TYPES = ("media", "webpage")
MAX_BATCH_SIZE = 25


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate missing summaries from existing raw content. "
            "The command does not change embeddings, tags, or categories."
        )
    )
    parser.add_argument("--tenant-id", required=True, help="Tenant to inspect.")
    parser.add_argument("--limit", required=True, type=int, help="Maximum candidates to inspect.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=MAX_BATCH_SIZE,
        help=f"Progress batch size; maximum {MAX_BATCH_SIZE}. Provider calls stay sequential.",
    )
    parser.add_argument(
        "--source-type",
        action="append",
        dest="source_types",
        choices=DEFAULT_SOURCE_TYPES,
        help="Eligible source type. Repeat as needed. Defaults to media and webpage.",
    )
    parser.add_argument(
        "--item-id",
        action="append",
        dest="item_ids",
        type=uuid.UUID,
        help="Restrict work to one item UUID. Repeat as needed.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter text model.")
    parser.add_argument("--write", action="store_true", help="Persist summaries. Omit for dry-run.")
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    from app.services.summary_backfill import run_summary_backfill

    report = await run_summary_backfill(
        tenant_id=args.tenant_id,
        limit=args.limit,
        batch_size=args.batch_size,
        source_types=tuple(args.source_types or DEFAULT_SOURCE_TYPES),
        item_ids=tuple(args.item_ids or ()),
        write=args.write,
        model=args.model,
    )
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
