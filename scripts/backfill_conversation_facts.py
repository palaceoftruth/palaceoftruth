#!/usr/bin/env python3
"""Opt-in scoped conversation fact backfill for Palace memory entries."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://conversation-facts:unused@localhost/palace")
os.environ.setdefault("API_KEY", "conversation-facts-unused")
os.environ.setdefault("OPENAI_API_KEY", "conversation-facts-unused")
os.environ.setdefault("OPENROUTER_API_KEY", "conversation-facts-unused")

from app.database import async_session  # noqa: E402
from app.services.conversation_facts import backfill_conversation_facts  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract advisory source-linked conversation facts from existing scoped memory entries."
    )
    parser.add_argument("--tenant-id", default="default", help="Tenant id to inspect.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum ready memory items to scan.")
    parser.add_argument(
        "--max-facts-per-item",
        type=int,
        default=20,
        help="Maximum conversation turns to backfill per source memory item.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Submit derived conversation facts as queued memory-artifact jobs. Omit for dry-run.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Bounded worker count for source-item discovery/parsing. Writes remain sequential for DB safety.",
    )
    parser.add_argument(
        "--item-timeout-seconds",
        type=float,
        default=None,
        help="Optional per-source-item timeout for discovery and submission; timed-out items are reported and skipped.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    async with async_session() as db:
        result = await backfill_conversation_facts(
            db,
            tenant_id=args.tenant_id,
            limit=args.limit,
            max_facts_per_item=args.max_facts_per_item,
            dry_run=not args.write,
            workers=args.workers,
            item_timeout_seconds=args.item_timeout_seconds,
        )
    print(
        "\n".join(
            [
                f"dry_run={result.dry_run}",
                f"worker_count={result.worker_count}",
                f"items_scanned={result.items_scanned}",
                f"items_completed={result.items_completed}",
                f"items_skipped={result.items_skipped}",
                f"items_failed={result.items_failed}",
                f"facts_discovered={result.facts_discovered}",
                f"facts_submitted={result.facts_submitted}",
                f"facts_queued={result.facts_queued}",
                f"facts_existing={result.facts_existing}",
            ]
        )
    )
    for failure in result.failures:
        print(
            "failure="
            f"source_item_id={failure['source_item_id']} "
            f"reason={failure['reason']} "
            f"error={failure['error']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
