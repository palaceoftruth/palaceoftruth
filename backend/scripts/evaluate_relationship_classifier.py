from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic


repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/testdb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "offline-not-configured")
os.environ.setdefault("OPENROUTER_API_KEY", "offline-not-configured")
os.environ.setdefault("API_KEY", "offline-not-configured")

from app.services.llm import LLMService
from app.services.relationship_classification_contract import (
    RELATIONSHIP_PROMPT_SHA256,
    RELATIONSHIP_PROMPT_VERSION,
    RELATIONSHIP_SCHEMA_VERSION,
)
from app.services.relationship_precision_eval import (
    evaluate_relationship_precision,
    load_relationship_eval_fixture,
    load_relationship_eval_outputs,
)


DEFAULT_FIXTURE = (
    repo_root
    / "tests"
    / "fixtures"
    / "relationship_precision"
    / "sar_1245_locked_holdout_v2.json"
)
DEFAULT_FIXTURE_SHA256 = "4a942eda5fe29dd0301536454c88e7aa28df695e4af485a4075e70020a8e38b5"


def _code_revision() -> dict[str, object]:
    completed = subprocess.run(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "backend",
            "chart",
            ".env.example",
            "docker-compose.yml",
            ".github",
        ],
        cwd=repo_root.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root.parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"git_revision": revision, "git_dirty": bool(completed.stdout)}


async def _capture_synthetic_outputs(fixture: dict) -> dict:
    if os.environ["OPENROUTER_API_KEY"] == "offline-not-configured":
        raise RuntimeError("OPENROUTER_API_KEY is required for --live-synthetic")
    service = LLMService()
    concurrency = asyncio.Semaphore(4)

    async def classify(case: dict) -> dict:
        async with concurrency:
            started_at = monotonic()
            result = await service.classify_relationship_detailed(
                case["title_a"],
                case["summary_a"],
                case["title_b"],
                case["summary_b"],
            )
            return {
                "case_id": case["id"],
                "relationship": result.relationship,
                "confidence": result.confidence,
                "schema_valid": result.validation_outcome in {"valid", "empty"},
                "validation_outcome": result.validation_outcome,
                "latency_seconds": monotonic() - started_at,
                "fallback_used": result.fallback_used,
                "retry_count": result.retry_count,
                "upstream_provider": result.upstream_provider,
                "identity": {
                    "provider": result.provider,
                    "requested_model": result.requested_model,
                    "model": result.model,
                    "prompt_version": result.prompt_version,
                    "prompt_sha256": RELATIONSHIP_PROMPT_SHA256,
                    "classifier_schema_version": RELATIONSHIP_SCHEMA_VERSION,
                    "temperature": result.temperature,
                    "seed": result.seed,
                },
            }

    return {
        "schema_version": 1,
        "output_set_id": (
            "sar-1245-offline-synthetic-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": fixture["fixture_sha256"],
        "prompt_version": RELATIONSHIP_PROMPT_VERSION,
        "prompt_sha256": RELATIONSHIP_PROMPT_SHA256,
        "classifier_schema_version": RELATIONSHIP_SCHEMA_VERSION,
        **_code_revision(),
        "outputs": await asyncio.gather(*(classify(case) for case in fixture["cases"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the SAR-1245 report-only relationship precision gate."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--replay", type=Path)
    source.add_argument("--live-synthetic", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    fixture = load_relationship_eval_fixture(
        args.fixture,
        expected_sha256=DEFAULT_FIXTURE_SHA256 if args.fixture == DEFAULT_FIXTURE else None,
    )
    outputs = (
        asyncio.run(_capture_synthetic_outputs(fixture))
        if args.live_synthetic
        else load_relationship_eval_outputs(args.replay)
    )
    report = evaluate_relationship_precision(
        fixture,
        outputs,
        extraction_threshold=args.threshold,
    )
    payload = {"outputs": outputs, "report": report}
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    # Keep terminal and CI output bounded; the optional output artifact retains
    # per-case evidence for audit and diagnosis.
    terminal_report = {key: value for key, value in report.items() if key != "cases"}
    print(json.dumps(terminal_report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
