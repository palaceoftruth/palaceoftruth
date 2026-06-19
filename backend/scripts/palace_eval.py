from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/testdb")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("OPENROUTER_API_KEY", "test-openrouter-key")
os.environ.setdefault("API_KEY", "test-api-key")

from app.evals.palace import (
    evaluate_fixture,
    evaluate_reward_fixture,
    load_eval_baseline,
    load_eval_fixture,
    load_reward_eval_fixture,
)


def main() -> int:
    fixture_path = repo_root / "tests" / "fixtures" / "palace_eval_fixture.json"
    baseline_path = repo_root / "tests" / "fixtures" / "palace_eval_baseline.json"

    parser = argparse.ArgumentParser(description="Run the Palace retrieval regression harness.")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    parser.add_argument(
        "--reward-json",
        action="store_true",
        help="Print the reward-style retrieval and answer trajectory report as JSON.",
    )
    args = parser.parse_args()

    if args.reward_json:
        reward_fixture_path = repo_root / "tests" / "fixtures" / "palace_reward_eval_fixture.json"
        print(json.dumps(evaluate_reward_fixture(load_reward_eval_fixture(reward_fixture_path)), indent=2))
        return 0

    report = evaluate_fixture(load_eval_fixture(fixture_path))
    baseline = load_eval_baseline(baseline_path)

    if args.json:
        print(json.dumps({"baseline": baseline, "report": report}, indent=2))
        return 0

    print("Palace retrieval regression harness")
    print(f"Flat accuracy:   {report['flat']['hits']}/{report['flat']['total']} ({report['flat']['accuracy']:.2f})")
    print(f"Palace accuracy: {report['palace']['hits']}/{report['palace']['total']} ({report['palace']['accuracy']:.2f})")
    print(
        f"Route accuracy:  {report['routing']['hits']}/{report['routing']['total']} "
        f"({report['routing']['accuracy']:.2f})"
    )
    print(f"Delta:           {report['delta']:+.2f}")
    print(
        "Thresholds:      "
        f"palace>={baseline['min_palace_accuracy']:.2f}, "
        f"route>={baseline['min_route_accuracy']:.2f}, "
        f"delta>={baseline['min_accuracy_delta']:.2f}"
    )

    for query in report["queries"]:
        outcome = "PASS" if query["palace_hit"] else "FAIL"
        print(
            f"- {outcome} {query['query']} -> "
            f"{query['palace_wing']} / {query['palace_room']} / {query['palace_top_item_id']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
