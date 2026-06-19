#!/usr/bin/env python3
"""Run Palace database schema/index health checks."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

ORIGINAL_DATABASE_URL = os.environ.get("DATABASE_URL")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://health-check:unused@localhost/palace")
os.environ.setdefault("API_KEY", "health-check-unused")
os.environ.setdefault("OPENAI_API_KEY", "health-check-unused")
os.environ.setdefault("OPENROUTER_API_KEY", "health-check-unused")

from app.services.database_health import (  # noqa: E402
    render_report,
    report_to_json,
    run_live_health,
    run_static_health,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Palace Alembic, pgvector, tenant, job, MCP, and search database expectations."
    )
    parser.add_argument(
        "--repo-root",
        default=str(REPO_ROOT),
        help="Repository root to inspect for static checks. Defaults to this script's repository.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also inspect a live Postgres database. Requires --database-url or DATABASE_URL.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres SQLAlchemy URL for live inspection. Redacted in output.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root)
    if args.live:
        database_url = args.database_url or ORIGINAL_DATABASE_URL
        if not database_url:
            print("--live requires --database-url or DATABASE_URL", file=sys.stderr)
            return 2
        report = await run_live_health(repo_root, database_url)
    else:
        report = run_static_health(repo_root)

    print(report_to_json(report) if args.format == "json" else render_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
