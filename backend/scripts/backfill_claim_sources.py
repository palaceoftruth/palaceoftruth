from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict

from app.database import async_session
from app.services.source_compiler import backfill_claims_from_temporal_facts


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill claim/source support from temporal facts.")
    parser.add_argument("--tenant-id", required=True, help="Tenant to backfill.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum temporal facts to inspect.")
    parser.add_argument(
        "--fact-id",
        action="append",
        default=None,
        help="Specific temporal_fact id to include. May be repeated.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write claims and claim_sources. Omit for dry-run planning.",
    )
    return parser.parse_args()


def _parse_fact_ids(values: list[str] | None) -> list[uuid.UUID] | None:
    if not values:
        return None
    return [uuid.UUID(value) for value in values]


async def _main() -> None:
    args = _parse_args()
    async with async_session() as db:
        report = await backfill_claims_from_temporal_facts(
            db,
            tenant_id=args.tenant_id,
            fact_ids=_parse_fact_ids(args.fact_id),
            limit=args.limit,
            dry_run=not args.write,
        )
    print(json.dumps(asdict(report), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
