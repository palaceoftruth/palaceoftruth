#!/usr/bin/env python3
"""Run report-only candidate curation scoring against sanitized fixtures."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://curation-scoring:unused@localhost/palace")
os.environ.setdefault("API_KEY", "curation-scoring-unused")
os.environ.setdefault("OPENAI_API_KEY", "curation-scoring-unused")
os.environ.setdefault("OPENROUTER_API_KEY", "curation-scoring-unused")

from app.services.candidate_curation_scoring import (  # noqa: E402
    CandidateCurationScoringError,
    candidate_curation_report_to_json,
    read_candidate_fixture_pack,
    score_candidate_fixture_pack,
)


DEFAULT_PACK = REPO_ROOT / "backend" / "tests" / "fixtures" / "candidate_curation_scoring_pack.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Score sanitized candidate curation artifacts without mutating Palace data "
            "or printing raw memory bodies."
        )
    )
    parser.add_argument("--pack", default=str(DEFAULT_PACK), help="Sanitized fixture pack to score.")
    parser.add_argument("--output", help="Write the JSON report to this path instead of stdout.")
    parser.add_argument(
        "--require-all-ready",
        action="store_true",
        help="Exit non-zero unless every candidate is promotion-ready.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = score_candidate_fixture_pack(read_candidate_fixture_pack(Path(args.pack)))
    except CandidateCurationScoringError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    rendered = candidate_curation_report_to_json(report)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)

    if not report["passed"]:
        return 1
    if args.require_all_ready and report["blocked_promotion_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
