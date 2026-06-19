#!/usr/bin/env python3
"""Taxonomy-only backfill for ready items with missing tags or categories."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("API_KEY", "taxonomy-backfill-unused")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://taxonomy-backfill:unused@localhost/palace")
os.environ.setdefault("OPENAI_API_KEY", "taxonomy-backfill-unused")
os.environ.setdefault("OPENROUTER_API_KEY", "taxonomy-backfill-unused")

from arq import create_pool  # noqa: E402

from app.config import make_redis_settings  # noqa: E402
from app.services.llm import LLMService  # noqa: E402
from app.workers.tasks import backfill_missing_taxonomy  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate missing tags/categories from existing item.raw_content[:4000]. "
            "Does not retry jobs, download media, or transcribe audio."
        )
    )
    parser.add_argument("--tenant-id", default="default", help="Tenant id to inspect.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum candidate items to inspect.")
    parser.add_argument(
        "--source-type",
        action="append",
        dest="source_types",
        help=(
            "Restrict to a source_type. Repeat for multiple values. "
            "Defaults to media, webpage, pdf, doc, image, and note."
        ),
    )
    parser.add_argument("--write", action="store_true", help="Persist taxonomy updates. Omit for dry-run.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    redis = await create_pool(make_redis_settings())
    try:
        report = await backfill_missing_taxonomy(
            {"llm": LLMService(), "redis": redis},
            tenant_id=args.tenant_id,
            limit=args.limit,
            dry_run=not args.write,
            source_types=args.source_types,
        )
    finally:
        await redis.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if int(report.get("failure_count", 0)) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
