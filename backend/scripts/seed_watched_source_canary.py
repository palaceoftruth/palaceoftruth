"""Plan or add one isolated watched-source canary without changing existing rows.

The command is zero-write unless ``--write`` is supplied. A write is additive
and idempotent: it creates the exact canary resource only when absent, and it
never updates or deletes an existing resource, alias, item, or source record.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_TENANT_ID = "sar-1207-canary"
DEFAULT_HOST = "palace-source-canary.palace-sarvent.svc.cluster.local"
DEFAULT_URL = f"http://{DEFAULT_HOST}/canary.html"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--allowed-host", default=DEFAULT_HOST)
    parser.add_argument(
        "--refresh-slo-seconds",
        type=int,
        default=900,
        help="Canary policy window; must be 300-3600 seconds.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Add the isolated canary row if absent; never modifies existing rows.",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    host = (urlsplit(args.url).hostname or "").lower().rstrip(".")
    allowed_host = args.allowed_host.lower().rstrip(".")
    if host != allowed_host:
        parser.error("--url hostname must exactly match --allowed-host")
    if args.tenant_id != DEFAULT_TENANT_ID:
        parser.error(f"--tenant-id must remain {DEFAULT_TENANT_ID!r}")
    if not 300 <= args.refresh_slo_seconds <= 3600:
        parser.error("--refresh-slo-seconds must be 300-3600")
    return args


async def seed(args: argparse.Namespace) -> dict[str, object]:
    report: dict[str, object] = {
        "mode": "write" if args.write else "dry_run",
        "tenant_id": args.tenant_id,
        "host": args.allowed_host,
        "created": False,
        "already_present": False,
    }
    if not args.write:
        return report

    from sqlalchemy import select

    from app.database import async_session
    from app.models.source_resource import SourceResource
    from app.services.source_resources import build_alias, canonical_http_identity, normalize_http_url

    canonical_url = normalize_http_url(args.url)
    canonical_identity = canonical_http_identity(canonical_url)
    async with async_session() as db:
        existing = await db.scalar(
            select(SourceResource).where(
                SourceResource.tenant_id == args.tenant_id,
                SourceResource.kind == "http",
                SourceResource.canonical_identity == canonical_identity,
            )
        )
        if existing is not None:
            report["already_present"] = True
            report["resource_id"] = str(existing.id)
            return report

        resource = SourceResource(
            tenant_id=args.tenant_id,
            kind="http",
            source_class="webpage",
            canonical_url=canonical_url,
            canonical_identity=canonical_identity,
            refresh_policy="interval",
            refresh_slo_seconds=args.refresh_slo_seconds,
            status="active",
            next_due_at=datetime.now(timezone.utc),
            consecutive_failures=0,
        )
        db.add(resource)
        await db.flush()
        db.add(
            build_alias(
                resource=resource,
                tenant_id=args.tenant_id,
                observed_url=args.url,
                signal="submitted",
                provenance={"canary": "SAR-1207", "fixture": "internal-only"},
            )
        )
        await db.commit()
        report["created"] = True
        report["resource_id"] = str(resource.id)
    return report


async def main(argv: list[str] | None = None) -> int:
    report = await seed(parse_args(argv))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
