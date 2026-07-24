"""Inventory or explicitly enroll saved webpages as manually watched sources.

The default mode is an aggregate-only, zero-write report.  ``--write`` requires
explicit WebSave IDs and creates only manual resources: it never performs HTTP
requests, enables dispatch, or changes aliases on existing resources.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# Make the backend package importable when this script is invoked by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def _encode_cursor(web_save: Any) -> str:
    payload = {"saved_at": web_save.saved_at.isoformat(), "web_save_id": str(web_save.id)}
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[Any, uuid.UUID]:
    from datetime import datetime

    try:
        payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        saved_at = datetime.fromisoformat(payload["saved_at"])
        web_save_id = uuid.UUID(payload["web_save_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cursor must be a valid enrollment cursor") from exc
    if saved_at.tzinfo is None:
        raise ValueError("cursor timestamp must be timezone-aware")
    return saved_at, web_save_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--web-save-id", action="append", default=[], help="Explicit WebSave UUID; repeat to enroll a bounded set.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows to inspect (1-500).")
    parser.add_argument("--per-host-limit", type=int, default=10, help="Maximum selected candidates per host (1-100).")
    parser.add_argument("--cursor", help="Opaque cursor returned by a prior inventory report.")
    parser.add_argument("--write", action="store_true", help="Create manual source resources for the explicit IDs only.")
    args = parser.parse_args()
    if not 1 <= args.limit <= 500 or not 1 <= args.per_host_limit <= 100:
        parser.error("--limit must be 1-500 and --per-host-limit must be 1-100")
    if args.write and not args.web_save_id:
        parser.error("--write requires at least one --web-save-id; inventory is zero-write by default")
    if args.write and len(args.web_save_id) > args.limit:
        parser.error("explicit IDs exceed --limit")
    if args.cursor and args.web_save_id:
        parser.error("--cursor cannot be combined with explicit --web-save-id selection")
    if args.cursor:
        try:
            _decode_cursor(args.cursor)
        except ValueError as exc:
            parser.error(str(exc))
    return args


async def rows_for_args(args: argparse.Namespace) -> tuple[list[tuple[WebSave, Item]], str | None]:
    from sqlalchemy import select, tuple_

    from app.database import async_session
    from app.models.item import Item
    from app.models.web_save import WebSave

    statement = (
        select(WebSave, Item)
        .join(Item, Item.id == WebSave.item_id)
        .where(WebSave.tenant_id == args.tenant_id, Item.tenant_id == args.tenant_id)
        .order_by(WebSave.saved_at.asc(), WebSave.id.asc())
        .limit(args.limit + 1)
    )
    if args.web_save_id:
        statement = statement.where(WebSave.id.in_(args.web_save_id))
    elif args.cursor:
        saved_at, web_save_id = _decode_cursor(args.cursor)
        statement = statement.where(tuple_(WebSave.saved_at, WebSave.id) > tuple_(saved_at, web_save_id))
    async with async_session() as db:
        rows = (await db.execute(statement)).all()
    has_more = len(rows) > args.limit
    page = rows[: args.limit]
    return page, _encode_cursor(page[-1][0]) if has_more and page else None


async def enroll(args: argparse.Namespace, rows: list[tuple[WebSave, Item]], next_cursor: str | None) -> dict[str, Any]:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.database import async_session
    from app.models.source_resource import SourceResource
    from app.services.source_resources import build_alias
    from app.services.watched_source_enrollment import candidate_from_web_save

    outcomes: Counter[str] = Counter()
    policy: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    selected_by_host: defaultdict[str, int] = defaultdict(int)
    candidates = []
    for web_save, item in rows:
        source_type = item.source_type or "unknown"
        policy[f"source_type:{source_type}"] += 1
        candidate, reason = candidate_from_web_save(web_save, item)
        if candidate is None:
            outcomes[reason or "excluded"] += 1
            continue
        policy["eligible_webpage"] += 1
        domains[candidate.domain] += 1
        if selected_by_host[candidate.domain] >= args.per_host_limit:
            outcomes["per_host_limit"] += 1
            continue
        selected_by_host[candidate.domain] += 1
        candidates.append((web_save, candidate))

    report: dict[str, Any] = {
        "mode": "write" if args.write else "dry_run",
        "tenant_id": args.tenant_id,
        "inspected": len(rows),
        "candidate_policy": dict(sorted(policy.items())),
        "exclusion_reason": dict(sorted(outcomes.items())),
        "source_type": dict(sorted((key.removeprefix("source_type:"), value) for key, value in policy.items() if key.startswith("source_type:"))),
        "domain": dict(sorted(domains.items())),
        "selected": len(candidates),
        "selected_domain": dict(sorted(selected_by_host.items())),
        "next_cursor": next_cursor,
        "exhausted": next_cursor is None,
        "writes": {"created": 0, "already_enrolled": 0, "failed": 0},
    }
    if not args.write:
        return report

    async with async_session() as db:
        for web_save, candidate in candidates:
            try:
                # A savepoint keeps an idempotency race from rolling back earlier IDs.
                async with db.begin_nested():
                    existing = await db.scalar(
                        select(SourceResource).where(
                            SourceResource.tenant_id == candidate.tenant_id,
                            SourceResource.kind == "http",
                            SourceResource.canonical_identity == candidate.canonical_identity,
                        )
                    )
                    if existing is not None:
                        report["writes"]["already_enrolled"] += 1
                        continue
                    resource = SourceResource(
                        tenant_id=candidate.tenant_id,
                        kind="http",
                        source_class="webpage",
                        canonical_url=candidate.canonical_url,
                        canonical_identity=candidate.canonical_identity,
                        # Policy selection is intentionally deferred to the later operator task.
                        refresh_policy="manual",
                        status="active",
                        refresh_slo_seconds=86400,
                        consecutive_failures=0,
                    )
                    db.add(resource)
                    await db.flush()
                    # Retain the submitted alias even when it conflicts cross-origin; never merge it.
                    db.add(build_alias(resource=resource, tenant_id=candidate.tenant_id, observed_url=candidate.original_url, signal="submitted", provenance={"enrollment": "web_save"}))
                    db.add(build_alias(resource=resource, tenant_id=candidate.tenant_id, observed_url=candidate.canonical_url, signal="canonical", provenance={"enrollment": "web_save"}))
                    await db.flush()
            except IntegrityError:
                existing = await db.scalar(
                    select(SourceResource).where(
                        SourceResource.tenant_id == candidate.tenant_id,
                        SourceResource.kind == "http",
                        SourceResource.canonical_identity == candidate.canonical_identity,
                    )
                )
                if existing is not None:
                    report["writes"]["already_enrolled"] += 1
                else:
                    report["writes"]["failed"] += 1
                continue
            report["writes"]["created"] += 1
        await db.commit()
    return report


async def main() -> int:
    args = parse_args()
    rows, next_cursor = await rows_for_args(args)
    report = await enroll(args, rows, next_cursor)
    print(json.dumps(report, sort_keys=True))
    return 1 if report["writes"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
