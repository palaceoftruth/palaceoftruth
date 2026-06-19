from pathlib import Path

import pytest

from app.services.agent_memory_eval import (
    AgentMemoryEvalInputError,
    AgentMemoryEvalThresholds,
    LexicalOverlapReranker,
    StaticScoreReranker,
    build_live_retrieval_request,
    case_from_live_response,
    compatibility_fixture_pack_to_eval_pack,
    evaluate_reranker_ablation,
    evaluate_eval_pack,
    pack_rerank_candidates,
    public_benchmark_case_to_eval_case,
    read_compatibility_fixture_eval_pack,
    read_public_benchmark_eval_pack,
    read_eval_pack,
    rerank_eval_pack,
)


FIXTURE = Path(__file__).parent / "fixtures" / "agent_memory_eval_pack.json"
COMPATIBILITY_FIXTURE = Path(__file__).parent / "fixtures" / "agent_memory_compatibility_fixture_pack.json"
PUBLIC_FIXTURE = Path(__file__).parent / "fixtures" / "public_memory_benchmark_rows.json"


def test_agent_memory_eval_pack_reports_scoped_recall_metrics() -> None:
    report = evaluate_eval_pack(read_eval_pack(FIXTURE), top_k=5)

    assert report["summary"]["case_count"] == 11
    assert report["summary"]["average_recall_at_k"] == 0.9091
    assert report["summary"]["average_precision_at_k"] > 0
    assert report["summary"]["mean_reciprocal_rank"] == 0.9242
    assert report["summary"]["average_ndcg_at_k"] == 0.9091
    assert report["summary"]["provenance_label_accuracy"] == 1.0
    assert report["summary"]["expected_top_rank_accuracy"] == 1.0
    assert report["summary"]["expected_top_rank_failure_cases"] == []
    assert report["summary"]["current_first_accuracy"] == 1.0
    assert report["summary"]["current_first_failure_cases"] == []
    assert report["summary"]["source_coverage_accuracy"] == 1.0
    assert report["summary"]["source_coverage_failure_cases"] == []
    assert report["summary"]["context_budget_fit_rate"] == 1.0
    assert report["summary"]["context_budget_failure_cases"] == []
    assert report["summary"]["forbidden_hit_count"] == 0
    assert report["summary"]["forbidden_context_term_count"] == 0
    assert report["summary"]["passed"] is True
    assert report["summary"]["display_cap_hidden_relevant_cases"] == [
        "display-cap-hidden-candidate"
    ]
    assert report["summary"]["hermes_context_failure_cases"] == []
    assert report["summary"]["per_category"]["experience-interference"]["case_count"] == 4


def test_compatibility_fixture_pack_normalizes_all_transports() -> None:
    pack = read_compatibility_fixture_eval_pack(COMPATIBILITY_FIXTURE)
    report = evaluate_eval_pack(pack, top_k=5)

    assert pack["artifact_metadata"]["offline_report_only"] is True
    assert pack["artifact_metadata"]["required_transports"] == [
        "rest",
        "mcp-http",
        "mcp-stdio",
        "hermes",
    ]
    assert len(pack["cases"]) == 12
    assert report["summary"]["passed"] is True
    assert report["summary"]["target_comparisons"]["compatibility_fixture_r5"] == {
        "metric": "R@5",
        "target": 1.0,
        "actual": 1.0,
        "passed": True,
    }
    assert report["summary"]["per_transport"]["rest"]["case_count"] == 3
    assert report["summary"]["per_transport"]["mcp-http"]["case_count"] == 3
    assert report["summary"]["per_transport"]["mcp-stdio"]["case_count"] == 3
    assert report["summary"]["per_transport"]["hermes"]["case_count"] == 3
    assert report["summary"]["route_accuracy"] == 1.0
    assert report["summary"]["source_coverage_accuracy"] == 1.0
    assert report["summary"]["current_first_accuracy"] == 1.0
    assert report["summary"]["forbidden_context_term_count"] == 0


def test_compatibility_fixture_pack_rejects_raw_bodies_and_secretish_values() -> None:
    payload = {
        "schema_version": 1,
        "cases": [
            {
                "id": "bad",
                "query": "bad fixture",
                "expected_item_ids": ["safe"],
                "transport_outputs": {
                    "rest": {"results": [{"item_id": "safe", "body": "raw memory text"}]},
                    "mcp-http": {"results": [{"item_id": "safe"}]},
                    "mcp-stdio": {"results": [{"item_id": "safe"}]},
                    "hermes": {
                        "results": [{"item_id": "safe"}],
                        "hermes_context": "authorization: Bearer abc123",
                    },
                },
            }
        ],
    }

    with pytest.raises(AgentMemoryEvalInputError, match="raw memory body"):
        compatibility_fixture_pack_to_eval_pack(payload)

    del payload["cases"][0]["transport_outputs"]["rest"]["results"][0]["body"]
    with pytest.raises(AgentMemoryEvalInputError, match="secret-like"):
        compatibility_fixture_pack_to_eval_pack(payload)


def test_agent_memory_eval_pack_fails_only_explicit_thresholds() -> None:
    report = evaluate_eval_pack(
        read_eval_pack(FIXTURE),
        top_k=5,
        thresholds=AgentMemoryEvalThresholds(
            recall_at_k=1.0,
            precision_at_k=0.1,
            mrr=1.0,
            ndcg_at_k=1.0,
            provenance_label_accuracy=1.0,
            forbidden_hit_count=0,
        ),
    )

    assert report["summary"]["passed"] is False
    assert report["summary"]["failure_counts"] == {
        "recall_at_k": 1,
        "mrr": 1,
        "ndcg_at_k": 1,
    }


def test_agent_memory_eval_pack_catches_tenant_isolation_negative() -> None:
    payload = read_eval_pack(FIXTURE)
    case = next(row for row in payload["cases"] if row["id"] == "other-agent-isolation-negative")
    case["results"].insert(
        0,
        {
            "item_id": "agent-other-private",
            "title": "Other agent private memory",
            "score": 0.99,
            "scope": {"type": "agent", "key": "other-agent"},
        },
    )

    report = evaluate_eval_pack(payload, top_k=5)

    assert report["summary"]["failure_counts"]["forbidden_hit_count"] == 1
    isolation_case = next(row for row in report["cases"] if row["id"] == case["id"])
    assert isolation_case["forbidden_hits"] == ["agent-other-private"]


def test_agent_memory_eval_pack_catches_provenance_label_regression() -> None:
    payload = read_eval_pack(FIXTURE)
    case = next(row for row in payload["cases"] if row["id"] == "selected-workspace-hit")
    case["results"][0]["scope"] = {"type": "agent", "key": "codex"}

    report = evaluate_eval_pack(payload, top_k=5)

    assert report["summary"]["failure_counts"]["provenance_label_accuracy"] == 1
    selected_case = next(row for row in report["cases"] if row["id"] == case["id"])
    assert selected_case["provenance_label_failures"] == [
        {
            "item_id": "workspace-exampleos-approval",
            "expected": "workspace/exampleos",
            "actual": "agent/codex",
        }
    ]


def test_agent_memory_eval_pack_catches_dream_outranking_source_memory() -> None:
    payload = read_eval_pack(FIXTURE)
    case = next(
        row
        for row in payload["cases"]
        if row["id"] == "raw-memory-preferred-over-dream-summary"
    )
    case["results"] = list(reversed(case["results"]))

    report = evaluate_eval_pack(payload, top_k=5)

    assert report["summary"]["failure_counts"]["expected_top_rank_accuracy"] == 1
    assert report["summary"]["expected_top_rank_failure_cases"] == [
        "raw-memory-preferred-over-dream-summary"
    ]
    dream_case = next(row for row in report["cases"] if row["id"] == case["id"])
    assert dream_case["expected_top_rank_item_id"] == "workspace-palace-checkpoint-raw"
    assert dream_case["expected_top_rank_match"] is False
    assert dream_case["ranked_item_ids"][:2] == [
        "workspace-palace-dream-summary",
        "workspace-palace-checkpoint-raw",
    ]


def test_agent_memory_eval_pack_catches_experience_interference_regressions() -> None:
    payload = read_eval_pack(FIXTURE)
    current_case = next(
        row
        for row in payload["cases"]
        if row["id"] == "current-env-state-beats-stale-checkpoint"
    )
    current_case["results"] = list(reversed(current_case["results"]))
    coverage_case = next(
        row
        for row in payload["cases"]
        if row["id"] == "multi-source-aggregation-covers-queue-and-memory"
    )
    coverage_case["results"] = coverage_case["results"][:1]
    coverage_case["hermes_context"] += "\nPALACEOFTRUTH_API_KEY=redacted"
    coverage_case["forbidden_context_terms"] = ["PALACEOFTRUTH_API_KEY"]
    coverage_case["context_budget_chars"] = 40

    report = evaluate_eval_pack(payload, top_k=5)

    assert report["summary"]["failure_counts"] == {
        "current_first_accuracy": 1,
        "source_coverage_accuracy": 1,
        "context_budget_fit_rate": 1,
        "forbidden_context_term_count": 1,
    }
    assert report["summary"]["current_first_failure_cases"] == [
        "current-env-state-beats-stale-checkpoint"
    ]
    assert report["summary"]["source_coverage_failure_cases"] == [
        "multi-source-aggregation-covers-queue-and-memory"
    ]
    assert report["summary"]["context_budget_failure_cases"] == [
        "multi-source-aggregation-covers-queue-and-memory"
    ]
    assert report["summary"]["forbidden_context_failure_cases"] == [
        "multi-source-aggregation-covers-queue-and-memory"
    ]


def test_live_retrieve_response_normalizes_scope_and_metrics() -> None:
    case = {
        "id": "live-workspace-hit",
        "query": "workspace recall",
        "expected_item_ids": ["11111111-1111-1111-1111-111111111111"],
        "expected_scope_labels": {
            "11111111-1111-1111-1111-111111111111": "workspace/exampleos"
        },
        "expected_route": "global_merge",
    }
    request_payload = {
        "query": "workspace recall",
        "limit": 5,
        "scope": {"type": "workspace", "key": "exampleos"},
    }

    normalized = case_from_live_response(
        case,
        endpoint="/api/v1/memory/retrieve",
        request_payload=request_payload,
        response_payload={
            "scope": {"type": "workspace", "key": "exampleos"},
            "trace": {
                "fallback_used": True,
                "ranking_traces": [{"route": "global_merge"}],
            },
            "results": [
                {
                    "item_id": "11111111-1111-1111-1111-111111111111",
                    "title": "Workspace memory",
                    "score": 0.91,
                }
            ],
            "total": 1,
        },
        latency_ms=12.345,
    )
    report = evaluate_eval_pack({"schema_version": 1, "cases": [normalized]}, top_k=5)

    assert normalized["results"][0]["scope"] == {"type": "workspace", "key": "exampleos"}
    assert normalized["results"][0]["scope_label"] == "workspace/exampleos"
    assert report["cases"][0]["route_accuracy"] == 1.0
    assert report["cases"][0]["fallback_used"] is True
    assert report["summary"]["fallback_rate"] == 1.0
    assert report["summary"]["average_latency_ms"] == 12.35
    assert report["summary"]["recall_at"] == {
        "1": 1.0,
        "3": 1.0,
        "5": 1.0,
        "10": 1.0,
    }


def test_live_agent_retrieve_request_and_response_preserve_trace_without_fake_scope() -> None:
    case = {
        "id": "agent-policy",
        "query": "policy memory",
        "expected_item_ids": ["22222222-2222-2222-2222-222222222222"],
        "agent_scope_key": "codex",
        "workspace_scope_keys": ["palaceoftruth"],
        "tags": ["manual-test"],
        "tags_mode": "all",
    }
    request_payload = build_live_retrieval_request(
        case,
        endpoint="/api/v1/memory/retrieve-agent",
        top_k=10,
        candidate_limit=20,
        broad_candidate_limit=30,
        display_limit=5,
    )

    assert request_payload == {
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
    }

    normalized = case_from_live_response(
        case,
        endpoint="/api/v1/memory/retrieve-agent",
        request_payload=request_payload,
        response_payload={
            "scopes": [
                {"type": "agent", "key": "codex"},
                {"type": "workspace", "key": "palaceoftruth"},
                {"type": "tenant_shared"},
            ],
            "trace": {
                "selected_scope_candidate_limit": 20,
                "broad_candidate_limit": 30,
                "display_limit": 5,
                "fallback_used": False,
                "broad_corpus_searched": True,
            },
            "results": [
                {
                    "item_id": "22222222-2222-2222-2222-222222222222",
                    "title": "Policy memory",
                    "score": 0.87,
                }
            ],
            "total": 1,
        },
        latency_ms=7,
    )

    assert normalized["results"][0]["item_id"] == "22222222-2222-2222-2222-222222222222"
    assert "scope" not in normalized["results"][0]
    assert normalized["live_trace"]["selected_scope_candidate_limit"] == 20
    assert normalized["display_limit"] == 5


def test_agent_memory_eval_pack_validates_shape(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version":1,"cases":[]}', encoding="utf-8")

    with pytest.raises(AgentMemoryEvalInputError):
        read_eval_pack(invalid)


def test_public_benchmark_adapter_maps_longmemeval_rows() -> None:
    pack = read_public_benchmark_eval_pack(
        PUBLIC_FIXTURE,
        suite="longmemeval",
        artifact_metadata={"dataset_revision": "test-rev"},
        benchmark_targets={
            "mempalace_raw_longmemeval_r5": {"metric": "R@5", "value": 0.966}
        },
    )
    report = evaluate_eval_pack(pack, top_k=5)

    assert pack["artifact_metadata"] == {
        "adapter_suite": "longmemeval",
        "dataset_revision": "test-rev",
    }
    assert pack["cases"][0]["id"] == "longmemeval:lme-001"
    assert pack["cases"][0]["expected_item_ids"] == ["session-board-approval"]
    assert pack["cases"][0]["category"] == "single-session"
    assert pack["cases"][0]["results"][0]["item_id"] == "session-board-approval"
    assert report["summary"]["per_suite"]["longmemeval"]["case_count"] == 2
    assert report["summary"]["per_category"]["single-session"]["average_recall_at_k"] == 1.0
    assert report["summary"]["target_comparisons"]["mempalace_raw_longmemeval_r5"] == {
        "metric": "R@5",
        "target": 0.966,
        "actual": 1.0,
        "passed": True,
    }
    assert report["summary"]["average_ingest_time_ms"] == 123.4
    assert report["summary"]["average_index_time_ms"] == 45.6
    assert report["artifact_metadata"]["dataset_revision"] == "test-rev"


def test_public_benchmark_targets_support_precision_at_k() -> None:
    pack = read_public_benchmark_eval_pack(
        PUBLIC_FIXTURE,
        suite="membench",
        benchmark_targets={
            "gbrain_rich_prose_p5": {"metric": "P@5", "value": 0.5}
        },
    )
    report = evaluate_eval_pack(pack, top_k=5)

    assert report["summary"]["target_comparisons"]["gbrain_rich_prose_p5"] == {
        "metric": "P@5",
        "target": 0.5,
        "actual": 0.5,
        "passed": True,
    }


def test_public_benchmark_adapter_rejects_rows_without_ground_truth() -> None:
    with pytest.raises(AgentMemoryEvalInputError, match="missing expected"):
        public_benchmark_case_to_eval_case(
            {"id": "bad", "query": "no evidence"},
            suite="locomo",
        )


def test_reranker_ablation_reports_metric_deltas_and_confusing_rows() -> None:
    pack = {
        "schema_version": 1,
        "pack_id": "reranker-unit",
        "benchmark_targets": {
            "mock_precision_p1": {"metric": "P@1", "value": 0.6},
            "gbrain_rich_prose_r5": {"metric": "R@5", "value": 1.0},
        },
        "cases": [
            {
                "id": "authority-case",
                "query": "risk management framework",
                "expected_item_ids": ["nist-sp-800-37r2"],
                "forbidden_item_ids": ["nist-sp-800-39"],
                "results": [
                    {"item_id": "nist-sp-800-39", "title": "Managing Information Security Risk", "score": 0.99},
                    {"item_id": "nist-sp-800-37r2", "title": "Risk Management Framework", "score": 0.5},
                    {"item_id": "unrelated", "title": "Other source", "score": 0.4},
                ],
            }
        ],
    }

    report = evaluate_reranker_ablation(
        pack,
        rerankers=[StaticScoreReranker(name="mock-cross-encoder", scores={"nist-sp-800-37r2": 2.0})],
        top_k=1,
        candidate_limit=3,
        thresholds=AgentMemoryEvalThresholds(
            recall_at_k=0.0,
            precision_at_k=0.0,
            mrr=0.0,
            ndcg_at_k=0.0,
            provenance_label_accuracy=0.0,
            forbidden_hit_count=99,
        ),
    )

    assert report["baseline"]["summary"]["average_precision_at_k"] == 0.0
    assert report["variants"][0]["summary"]["average_precision_at_k"] == 1.0
    assert report["variants"][0]["metric_delta"]["average_precision_at_k"] == 1.0
    assert report["variants"][0]["summary"]["target_comparisons"]["mock_precision_p1"] == {
        "metric": "P@1",
        "target": 0.6,
        "actual": 1.0,
        "passed": True,
    }
    assert report["baseline"]["false_positive_top_k"] == [
        {
            "case_id": "authority-case",
            "rank": 1,
            "item_id": "nist-sp-800-39",
            "known_forbidden": True,
        }
    ]
    assert report["baseline"]["source_publication_confusion"] == [
        {
            "case_id": "authority-case",
            "rank": 1,
            "item_id": "nist-sp-800-39",
            "matched_expected_source_tokens": ["nist"],
        }
    ]


def test_rerank_eval_pack_keeps_production_order_out_of_runtime_defaults() -> None:
    pack = read_eval_pack(FIXTURE)
    reranked = rerank_eval_pack(
        pack,
        reranker=StaticScoreReranker(
            name="mock",
            scores={"workspace-exampleos-hidden": 10.0},
        ),
        candidate_limit=6,
    )

    original_case = next(case for case in pack["cases"] if case["id"] == "display-cap-hidden-candidate")
    reranked_case = next(case for case in reranked["cases"] if case["id"] == "display-cap-hidden-candidate")

    assert original_case["results"][0]["item_id"] == "distractor-1"
    assert reranked_case["results"][0]["item_id"] == "workspace-exampleos-hidden"
    assert reranked_case["reranker_ablation"]["reranker"] == "mock"
    assert "reranker_ablation" not in original_case


def test_pack_rerank_candidates_redacts_secret_like_text() -> None:
    candidates = pack_rerank_candidates(
        {
            "id": "redaction",
            "query": "policy",
            "expected_item_ids": ["safe"],
            "results": [
                {
                    "item_id": "safe",
                    "title": "Policy",
                    "chunk_text": "authorization: Bearer abc123 should not leak",
                    "score": 1.0,
                }
            ],
        }
    )

    assert candidates[0].packed_text == "Policy authorization=[REDACTED] should not leak"


def test_lexical_overlap_reranker_is_deterministic_without_credentials() -> None:
    pack = {
        "schema_version": 1,
        "cases": [
            {
                "id": "lexical",
                "query": "risk management framework",
                "expected_item_ids": ["rmf"],
                "results": [
                    {"item_id": "other", "title": "Information security risk", "score": 0.99},
                    {"item_id": "rmf", "title": "Risk Management Framework", "score": 0.1},
                ],
            }
        ],
    }

    reranked = rerank_eval_pack(pack, reranker=LexicalOverlapReranker(), candidate_limit=2)

    assert reranked["cases"][0]["results"][0]["item_id"] == "rmf"
