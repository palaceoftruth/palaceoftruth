#!/usr/bin/env python3
"""Run report-only Palace memory storage quality checks."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://memory-quality:unused@localhost/palace")
os.environ.setdefault("API_KEY", "memory-quality-unused")
os.environ.setdefault("OPENAI_API_KEY", "memory-quality-unused")
os.environ.setdefault("OPENROUTER_API_KEY", "memory-quality-unused")

from app.database import async_session  # noqa: E402
from app.services.memory_storage_quality import (  # noqa: E402
    memory_storage_quality_report_to_json,
    render_memory_storage_quality_report,
    run_memory_storage_quality_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report scoped memory provenance and derived-artifact storage quality without mutating data."
    )
    parser.add_argument("--tenant-id", default="default", help="Tenant id to inspect.")
    parser.add_argument("--limit", type=int, default=500, help="Maximum recent note items to inspect.")
    parser.add_argument("--sample-limit", type=int, default=25, help="Maximum warning samples to print.")
    parser.add_argument(
        "--include-adjudication-gate",
        action="store_true",
        help="Include the report-only stale/current contradiction adjudication gate.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    async with async_session() as db:
        report = await run_memory_storage_quality_report(
            db,
            tenant_id=args.tenant_id,
            limit=args.limit,
            sample_limit=args.sample_limit,
            include_adjudication=args.include_adjudication_gate,
        )
    print(
        memory_storage_quality_report_to_json(report)
        if args.format == "json"
        else render_memory_storage_quality_report(report)
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
