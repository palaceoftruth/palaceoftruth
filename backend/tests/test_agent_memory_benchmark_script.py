import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_agent_memory_retrieval.py"
SPEC = importlib.util.spec_from_file_location("benchmark_agent_memory_retrieval", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
benchmark_module = importlib.util.module_from_spec(SPEC)
sys.modules["benchmark_agent_memory_retrieval"] = benchmark_module
SPEC.loader.exec_module(benchmark_module)


def test_live_report_posts_agent_policy_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack.json"
    pack.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "live-policy-pack",
                "cases": [
                    {
                        "id": "agent-policy",
                        "query": "policy memory",
                        "expected_item_ids": ["33333333-3333-3333-3333-333333333333"],
                        "agent_scope_key": "codex",
                        "workspace_scope_keys": ["palaceoftruth"],
                        "tags": ["manual-test"],
                        "tags_mode": "all",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    posted: list[tuple[str, dict[str, Any]]] = []
    headers_seen: list[dict[str, str]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "scopes": [{"type": "agent", "key": "codex"}],
                "trace": {
                    "selected_scope_candidate_limit": 20,
                    "broad_candidate_limit": 30,
                    "display_limit": 5,
                    "fallback_used": False,
                },
                "results": [
                    {
                        "item_id": "33333333-3333-3333-3333-333333333333",
                        "title": "Policy memory",
                        "score": 0.92,
                    }
                ],
                "total": 1,
            }

    class FakeClient:
        def __init__(self, *, base_url: str, headers: dict[str, str], timeout: Any) -> None:
            assert base_url == "https://palaceoftruth.test"
            headers_seen.append(headers)

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def post(self, endpoint: str, *, json: dict[str, Any]) -> FakeResponse:
            posted.append((endpoint, json))
            return FakeResponse()

    monkeypatch.setattr(benchmark_module.httpx, "Client", FakeClient)
    args = benchmark_module.build_parser().parse_args(
        [
            "live-report",
            "--pack",
            str(pack),
            "--base-url",
            "https://palaceoftruth.test",
            "--api-key",
            "secret",
            "--top-k",
            "10",
            "--candidate-limit",
            "20",
            "--broad-candidate-limit",
            "30",
            "--display-limit",
            "5",
        ]
    )

    assert benchmark_module.cmd_live_report(args) == 0
    assert headers_seen == [{"X-API-Key": "secret", "X-MCP-Scope": "read", "X-MCP-Scopes": "read"}]
    assert posted == [
        (
            "/api/v1/memory/retrieve-agent",
            {
                "query": "policy memory",
                "tags": ["manual-test"],
                "tags_mode": "all",
                "agent_scope_key": "codex",
                "workspace_scope_keys": ["palaceoftruth"],
                "include_tenant_shared": True,
                "include_broad_corpus": True,
                "limit": 5,
                "candidate_limit": 20,
                "broad_candidate_limit": 30,
                "display_limit": 5,
            },
        )
    ]


def test_public_report_converts_public_rows_and_writes_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "longmemeval.jsonl"
    report_path = tmp_path / "report.json"
    pack_path = tmp_path / "pack.json"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "question_id": "q1",
                        "question_type": "single-session",
                        "question": "Where is the approval policy?",
                        "answer_session_ids": ["s1"],
                        "results": [{"session_id": "s1", "score": 0.91}],
                        "latency_ms": 3.2,
                    }
                ),
                json.dumps(
                    {
                        "question_id": "q2",
                        "question_type": "multi-session",
                        "question": "Where is the rollout date?",
                        "answer_session_ids": ["s2"],
                        "results": [{"session_id": "s2", "score": 0.87}],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = benchmark_module.build_parser().parse_args(
        [
            "public-report",
            "--suite",
            "longmemeval",
            "--input",
            str(source),
            "--artifact-metadata",
            '{"dataset_revision":"unit-test"}',
            "--benchmark-targets",
            '{"mempalace_raw_longmemeval_r5":{"metric":"R@5","value":0.966}}',
            "--output",
            str(report_path),
            "--output-pack",
            str(pack_path),
        ]
    )

    assert benchmark_module.cmd_public_report(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    pack = json.loads(pack_path.read_text(encoding="utf-8"))

    assert pack["artifact_metadata"]["dataset_revision"] == "unit-test"
    assert report["summary"]["per_suite"]["longmemeval"]["case_count"] == 2
    assert report["summary"]["target_comparisons"]["mempalace_raw_longmemeval_r5"]["passed"] is True


def test_public_report_applies_named_mempalace_profile(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "public_memory_benchmark_mempalace_longmemeval_sample.jsonl"
    report_path = tmp_path / "report.json"
    pack_path = tmp_path / "pack.json"
    args = benchmark_module.build_parser().parse_args(
        [
            "public-report",
            "--artifact-profile",
            "mempalace-longmemeval-raw",
            "--input",
            str(source),
            "--output",
            str(report_path),
            "--output-pack",
            str(pack_path),
        ]
    )

    assert benchmark_module.cmd_public_report(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    pack = json.loads(pack_path.read_text(encoding="utf-8"))

    assert pack["pack_id"] == "mempalace-longmemeval-raw-public-report"
    assert pack["artifact_metadata"]["competitor"] == "MemPalace"
    assert pack["artifact_metadata"]["mode"] == "Raw ChromaDB"
    assert report["summary"]["target_comparisons"]["mempalace_raw_longmemeval_r5"] == {
        "metric": "R@5",
        "target": 0.966,
        "actual": 1.0,
        "passed": True,
    }


def test_public_report_applies_named_gbrain_profile_with_precision_target(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures" / "public_memory_benchmark_gbrain_rich_prose_sample.jsonl"
    report_path = tmp_path / "report.json"
    pack_path = tmp_path / "pack.json"
    args = benchmark_module.build_parser().parse_args(
        [
            "public-report",
            "--artifact-profile",
            "gbrain-rich-prose",
            "--input",
            str(source),
            "--output",
            str(report_path),
            "--output-pack",
            str(pack_path),
            "--min-precision-at-k",
            "0.1",
        ]
    )

    assert benchmark_module.cmd_public_report(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    pack = json.loads(pack_path.read_text(encoding="utf-8"))

    assert pack["pack_id"] == "gbrain-rich-prose-public-report"
    assert pack["artifact_metadata"]["competitor"] == "GBrain"
    assert "not a standard public" in pack["artifact_metadata"]["comparison_note"]
    assert report["summary"]["target_comparisons"]["gbrain_rich_prose_p5"] == {
        "metric": "P@5",
        "target": 0.491,
        "actual": 0.4166,
        "passed": False,
    }
    assert report["summary"]["target_comparisons"]["gbrain_rich_prose_r5"] == {
        "metric": "R@5",
        "target": 0.979,
        "actual": 1.0,
        "passed": True,
    }


def test_reranker_ablation_writes_report_without_credentials(tmp_path: Path) -> None:
    pack = tmp_path / "pack.json"
    report_path = tmp_path / "reranker-report.json"
    score_path = tmp_path / "scores.json"
    pack.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "reranker-cli-pack",
                "benchmark_targets": {
                    "gbrain_rich_prose_p5": {"metric": "P@5", "value": 1.0}
                },
                "cases": [
                    {
                        "id": "precision-case",
                        "query": "risk management framework",
                        "expected_item_ids": ["rmf"],
                        "results": [
                            {"item_id": "distractor", "title": "Risk overview", "score": 0.99},
                            {"item_id": "rmf", "title": "Risk Management Framework", "score": 0.4},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    score_path.write_text(json.dumps({"rmf": 2.0, "distractor": 0.1}), encoding="utf-8")
    args = benchmark_module.build_parser().parse_args(
        [
            "reranker-ablation",
            "--pack",
            str(pack),
            "--candidate-limit",
            "2",
            "--top-k",
            "1",
            "--reranker",
            f"static-json:{score_path}",
            "--output",
            str(report_path),
        ]
    )

    assert benchmark_module.cmd_reranker_ablation(args) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["baseline"]["summary"]["average_precision_at_k"] == 0.0
    assert report["variants"][0]["name"] == "static-json:scores.json"
    assert report["variants"][0]["summary"]["average_precision_at_k"] == 1.0
    assert report["variants"][0]["cases"][0]["ranked_item_ids"][0] == "rmf"
