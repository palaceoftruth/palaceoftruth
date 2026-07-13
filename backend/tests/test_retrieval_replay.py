import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.services.retrieval_replay import (
    ReplayInputError,
    compare_captures,
    read_capture_file,
    validate_capture_record,
)


def _record(
    item_ids: list[str],
    *,
    fallback_used: bool = False,
    latency_ms: float = 10.0,
    capture_set: str = "retained-nist",
    corpus_id: str = "nist-sp800-retained-v1",
    run_id: str = "baseline-run",
    source_ranking_mode: str | None = None,
    ablation_label: str | None = None,
    expectations: dict | None = None,
    route: str | None = None,
    scope_labels: list[str] | None = None,
    trust_classes: list[str] | None = None,
    source_support_states: list[str] | None = None,
    derived_raw_classes: list[str] | None = None,
) -> dict:
    scope_labels = scope_labels or []
    trust_classes = trust_classes or []
    source_support_states = source_support_states or []
    derived_raw_classes = derived_raw_classes or []
    return {
        "schema_version": 1,
        "capture": {
            "set": capture_set,
            "corpus_id": corpus_id,
            "run_id": run_id,
            **({"source_ranking_mode": source_ranking_mode} if source_ranking_mode else {}),
            **({"ablation_label": ablation_label} if ablation_label else {}),
        },
        **({"expectations": expectations} if expectations else {}),
        "endpoint": "/api/v1/memory/retrieve",
        "tenant_id": "tenant-a",
        "status": "ok",
        "latency_ms": latency_ms,
        "request": {
            "query_mode": "fingerprint",
            "query_fingerprint": "risk-management-framework-hash",
            "limit": 5,
            "scope": {"type": "tenant_shared", "key": None},
            "tags": ["benchmark:nist"],
        },
        "fallback_used": fallback_used,
        "results": [
            {
                "rank": rank,
                "item_id": item_id,
                "chunk_index": 0,
                "score": 1.0 / rank,
                "source_type": "pdf",
                "tags": ["benchmark:nist"],
                **({"retrieved_scope_label": scope_labels[rank - 1]} if rank <= len(scope_labels) else {}),
                **({"trust_class": trust_classes[rank - 1]} if rank <= len(trust_classes) else {}),
                **(
                    {"source_support_state": source_support_states[rank - 1]}
                    if rank <= len(source_support_states)
                    else {}
                ),
                **(
                    {"derived_raw_classification": derived_raw_classes[rank - 1]}
                    if rank <= len(derived_raw_classes)
                    else {}
                ),
            }
            for rank, item_id in enumerate(item_ids, start=1)
        ],
        **({"trace": {"selected_wing": route}} if route else {}),
    }


def test_compare_captures_reports_top_rank_and_fallback_drift() -> None:
    baseline = [_record(["nist-800-37r2", "nist-800-39"], fallback_used=False, latency_ms=20)]
    current = [_record(["nist-800-39", "nist-800-37r2"], fallback_used=True, latency_ms=700, run_id="current-run")]

    report = compare_captures(baseline, current, top_k=2, latency_delta_warn_ms=100)

    assert report["summary"]["failure_counts"] == {
        "top1_changed": 1,
        "fallback_changed": 1,
        "latency_delta_warn": 1,
    }
    comparison = report["comparisons"][0]
    assert comparison["baseline_top1"] == "nist-800-37r2"
    assert comparison["current_top1"] == "nist-800-39"
    assert comparison["jaccard_at_2"] == 1.0
    assert comparison["capture"] == {
        "set": "retained-nist",
        "baseline_corpus_id": "nist-sp800-retained-v1",
        "baseline_run_id": "baseline-run",
        "current_corpus_id": "nist-sp800-retained-v1",
        "current_run_id": "current-run",
        "baseline_source_ranking_mode": None,
        "current_source_ranking_mode": None,
        "baseline_ablation_label": None,
        "current_ablation_label": None,
    }
    assert report["summary"]["capture_sets"]["retained-nist"] == {
        "matched_records": 1,
        "corpus_ids": ["nist-sp800-retained-v1"],
        "run_ids": ["baseline-run", "current-run"],
    }


def test_compare_captures_reports_provenance_trust_mix() -> None:
    baseline = [
        _record(
            ["nist-800-37r2", "nist-800-39"],
            trust_classes=["raw_source", "raw_source"],
            source_support_states=["direct_source", "direct_source"],
            derived_raw_classes=["raw", "raw"],
        )
    ]
    current = [
        _record(
            ["nist-800-37r2", "generated-brief"],
            run_id="current-run",
            trust_classes=["raw_source", "low_support_generated"],
            source_support_states=["direct_source", "unsupported"],
            derived_raw_classes=["raw", "derived"],
        )
    ]

    report = compare_captures(baseline, current, top_k=2)

    assert report["comparisons"][0]["result_mix"] == {
        "baseline": {
            "trust_class_counts": {"raw_source": 2},
            "source_support_counts": {"direct_source": 2},
            "freshness_counts": {},
            "derived_raw_counts": {"raw": 2},
        },
        "current": {
            "trust_class_counts": {"low_support_generated": 1, "raw_source": 1},
            "source_support_counts": {"direct_source": 1, "unsupported": 1},
            "freshness_counts": {},
            "derived_raw_counts": {"derived": 1, "raw": 1},
        },
    }


def test_compare_captures_enforces_jaccard_and_required_sets() -> None:
    baseline = [_record(["nist-800-37r2", "nist-800-39", "nist-800-53"])]
    current = [_record(["nist-800-37r2", "unrelated"], run_id="current-run")]

    report = compare_captures(
        baseline,
        current,
        top_k=3,
        min_jaccard=0.75,
        required_capture_sets={"retained-nist", "agent-memory"},
    )

    assert report["summary"]["failure_counts"] == {
        "jaccard_below_threshold": 1,
        "missing_required_capture_set": 1,
    }
    assert report["summary"]["missing_required_capture_sets"] == ["agent-memory"]


def test_compare_captures_reports_ablation_metadata_without_changing_match_key() -> None:
    baseline = [
        _record(
            ["nist-800-37r2", "nist-800-39"],
            source_ranking_mode="off",
            ablation_label="source-ranking-off",
        )
    ]
    current = [
        _record(
            ["nist-800-37r2", "nist-800-39"],
            run_id="current-run",
            source_ranking_mode="on",
            ablation_label="source-ranking-on",
        )
    ]

    report = compare_captures(baseline, current, top_k=2, require_capture_metadata=True)

    assert report["summary"]["failure_counts"] == {}
    assert report["summary"]["matched_records"] == 1
    assert report["comparisons"][0]["capture"] == {
        "set": "retained-nist",
        "baseline_corpus_id": "nist-sp800-retained-v1",
        "baseline_run_id": "baseline-run",
        "current_corpus_id": "nist-sp800-retained-v1",
        "current_run_id": "current-run",
        "baseline_source_ranking_mode": "off",
        "current_source_ranking_mode": "on",
        "baseline_ablation_label": "source-ranking-off",
        "current_ablation_label": "source-ranking-on",
    }


def test_compare_captures_can_require_current_source_ranking_mode() -> None:
    baseline = [_record(["nist-800-37r2"], source_ranking_mode="off")]
    current = [_record(["nist-800-37r2"], run_id="current-run", source_ranking_mode="on")]

    report = compare_captures(
        baseline,
        current,
        require_current_source_ranking_mode="on",
    )

    assert report["summary"]["failure_counts"] == {}
    assert report["summary"]["require_current_source_ranking_mode"] == "on"


def test_compare_captures_fails_current_source_ranking_mode_mismatch() -> None:
    baseline = [_record(["nist-800-37r2"], source_ranking_mode="off")]
    current = [_record(["nist-800-37r2"], run_id="current-run", source_ranking_mode="off")]

    report = compare_captures(
        baseline,
        current,
        require_current_source_ranking_mode="on",
    )

    assert report["summary"]["failure_counts"] == {
        "current_source_ranking_mode_mismatch": 1
    }


def test_compare_captures_rejects_corpus_drift() -> None:
    baseline = [_record(["item-a"], corpus_id="agent-memory-v1")]
    current = [_record(["item-a"], corpus_id="agent-memory-v2", run_id="current-run")]

    report = compare_captures(baseline, current, require_capture_metadata=True)

    assert report["summary"]["failure_counts"] == {"corpus_id_changed": 1}


def test_compare_captures_reports_labeled_quality_metrics() -> None:
    baseline = [
        _record(
            ["nist-800-37r2", "nist-800-39"],
            expectations={
                "expected_item_ids": ["nist-800-37r2", "nist-800-39", "nist-800-53"],
                "forbidden_item_ids": ["unrelated"],
                "expected_scope_label": "workspace/nist",
                "expected_route": "Standards",
                "expected_top_rank": "nist-800-37r2",
                "query_type": "known_item",
            },
        )
    ]
    current = [
        _record(
            ["nist-800-37r2", "unrelated", "nist-800-39"],
            run_id="current-run",
            route="Standards",
            scope_labels=["workspace/nist", "workspace/nist", "workspace/nist"],
        )
    ]

    report = compare_captures(
        baseline,
        current,
        top_k=3,
        min_recall=0.8,
        min_mrr=1.0,
        min_ndcg=0.9,
        fail_on_forbidden=True,
        require_expected_scope=True,
        require_expected_route=True,
    )

    assert report["summary"]["failure_counts"] == {
        "recall_below_threshold": 1,
        "ndcg_below_threshold": 1,
        "forbidden_hit": 1,
    }
    comparison = report["comparisons"][0]
    assert comparison["quality"] == {
        "expected_count": 3,
        "hit_count": 2,
        "recall": 0.6667,
        "mrr": 1.0,
        "ndcg": 0.7039,
        "forbidden_hits": ["unrelated"],
    }
    assert comparison["expectations"] == {
        "query_type": "known_item",
        "expected_item_ids": ["nist-800-37r2", "nist-800-39", "nist-800-53"],
        "forbidden_item_ids": ["unrelated"],
        "expected_scope_label": "workspace/nist",
        "expected_scope_match": True,
        "expected_route": "Standards",
        "expected_route_match": True,
        "expected_top_rank": "nist-800-37r2",
    }


def test_compare_captures_can_fail_scope_route_and_expected_top_rank() -> None:
    baseline = [
        _record(
            ["expected-a"],
            expectations={
                "expected_item_ids": ["expected-a"],
                "expected_scope_label": "workspace/nist",
                "expected_route": "Standards",
                "expected_top_rank": "expected-a",
            },
        )
    ]
    current = [_record(["expected-b", "expected-a"], route="Other", scope_labels=["workspace/other", "workspace/other"])]

    report = compare_captures(
        baseline,
        current,
        top_k=2,
        require_expected_scope=True,
        require_expected_route=True,
    )

    assert report["summary"]["failure_counts"] == {
        "top1_changed": 1,
        "expected_top_rank_mismatch": 1,
        "expected_scope_mismatch": 1,
        "expected_route_mismatch": 1,
    }


def test_hermes_delegated_agent_fixture_rejects_caller_scope_dominance() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "retrieval_replay"
    baseline = read_capture_file(fixture_dir / "hermes_delegated_baseline.ndjson")
    current = read_capture_file(fixture_dir / "hermes_delegated_current.ndjson")

    report = compare_captures(
        baseline,
        current,
        top_k=3,
        min_recall=1.0,
        min_mrr=1.0,
        fail_on_forbidden=False,
        require_expected_scope=True,
        require_capture_metadata=True,
        require_current_source_ranking_mode="agent-scope-first",
    )

    assert report["summary"]["failure_counts"] == {}
    assert len(report["comparisons"]) == 3
    comparison = report["comparisons"][0]
    assert comparison["expectations"]["query_type"] == "delegated_policy_meta_recall"
    assert comparison["current_top1"] == "agent-security-policy"
    assert comparison["expectations"]["expected_scope_match"] is True
    assert current[0]["trace"]["authorized_agent_scope_keys"] == ["security", "macos"]
    assert current[0]["trace"]["denied_agent_scope_keys"] == []
    assert current[0]["trace"]["result_counts_by_scope"] == {
        "agent/orchestrator": 20,
        "agent/security": 5,
        "agent/macos": 5,
    }
    assert current[0]["trace"]["selected_scope_fallback_used"] is True
    assert current[0]["trace"]["selected_scope_completeness_warnings"] == [
        "Selected scoped retrieval reported low route confidence."
    ]
    assert current[0]["trace"]["broad_corpus_searched"] is False
    assert current[0]["trace"]["broad_result_count"] == 0
    assert current[0]["trace"]["fallback_used"] is True

    caller_first = json.loads(json.dumps(current[0]))
    caller_first["capture"]["run_id"] = "hermes-delegated-regression-20260524"
    caller_first["results"] = [
        {**result, "rank": rank}
        for rank, result in enumerate(
            [current[0]["results"][2], current[0]["results"][0], current[0]["results"][1]],
            start=1,
        )
    ]

    regression = compare_captures(
        [baseline[0]],
        [caller_first],
        top_k=3,
        min_recall=1.0,
        min_mrr=1.0,
        require_expected_scope=True,
        require_capture_metadata=True,
        require_current_source_ranking_mode="agent-scope-first",
    )

    assert regression["summary"]["failure_counts"] == {
        "top1_changed": 1,
        "expected_top_rank_mismatch": 1,
        "mrr_below_threshold": 1,
    }

    missing_trace = json.loads(json.dumps(current[0]))
    missing_trace.pop("trace")
    bad_path = fixture_dir / "bad.ndjson"
    with pytest.raises(ReplayInputError, match="retrieve-agent capture missing trace"):
        validate_capture_record(missing_trace, path=bad_path, line_number=1)


def test_conversation_fact_fixture_proves_scoped_source_span_recall() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "retrieval_replay"
    baseline = read_capture_file(fixture_dir / "conversation_facts_baseline.ndjson")
    current = read_capture_file(fixture_dir / "conversation_facts_current.ndjson")

    report = compare_captures(
        baseline,
        current,
        top_k=3,
        min_recall=1.0,
        min_mrr=1.0,
        fail_on_forbidden=True,
        require_expected_scope=True,
        require_capture_metadata=True,
        require_current_source_ranking_mode="conversation-fact-source-spans",
    )

    assert report["summary"]["failure_counts"] == {}
    comparison = report["comparisons"][0]
    assert comparison["expectations"]["query_type"] == "conversation_fact_scoped_recall"
    assert comparison["current_top1"] == "conv-fact-codex-pr-ready"
    assert comparison["expectations"]["expected_scope_match"] is True
    assert current[0]["results"][0]["source_span"] == {
        "source_item_id": "raw-conversation-source",
        "chunk_index": 0,
        "line_start": 1,
        "line_end": 1,
        "turn_index": 0,
    }


def test_conversation_trajectory_fixture_proves_source_spanned_timeline() -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "retrieval_replay"
    baseline = read_capture_file(fixture_dir / "conversation_trajectory_baseline.ndjson")
    current = read_capture_file(fixture_dir / "conversation_trajectory_current.ndjson")

    report = compare_captures(
        baseline,
        current,
        top_k=3,
        min_recall=1.0,
        min_mrr=1.0,
        fail_on_forbidden=True,
        require_expected_scope=True,
        require_capture_metadata=True,
        require_current_source_ranking_mode="conversation-trajectory-source-spans",
    )

    assert report["summary"]["failure_counts"] == {}
    assert report["comparisons"][0]["expectations"]["query_type"] == "conversation_fact_trajectory"
    assert [row["status"] for row in current[0]["results"]] == ["current", "stale"]
    assert current[0]["results"][0]["source_span"]["line_start"] == 5


def test_read_capture_file_rejects_malformed_ndjson(tmp_path) -> None:
    path = tmp_path / "bad.ndjson"
    path.write_text('{"schema_version": 1}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ReplayInputError, match="missing request.query_fingerprint"):
        read_capture_file(path)


def test_currentness_fixture_preserves_latest_and_historical_top_rank_without_scope_leaks(tmp_path) -> None:
    fixture_dir = Path(__file__).parent / "fixtures" / "retrieval_replay"
    baseline = read_capture_file(fixture_dir / "currentness_baseline.ndjson")
    current_path = tmp_path / "currentness_current.ndjson"
    subprocess.run(
        [sys.executable, str(Path(__file__).parents[2] / "scripts" / "replay_retrieval_capture.py"),
         "generate-currentness", "--output", str(current_path)],
        check=True,
    )
    current = read_capture_file(current_path)
    report = compare_captures(
        baseline, current, top_k=5, min_mrr=0.98, fail_on_forbidden=True,
        require_expected_scope=True, required_capture_sets={"currentness-regression"},
        require_capture_metadata=True,
        require_current_source_ranking_mode="currentness-aware",
        # The known-defect baseline contains historical live latency, while this
        # deterministic fixture validates ranking correctness rather than timing.
        latency_delta_warn_ms=3_000.0,
    )
    assert report["summary"]["failure_counts"] == {}
    assert [row["current_top1"] for row in report["comparisons"]] == [
        "deploy-current", "release-0.1.481", "codex-memory", "postgresql-current-doc"
    ]


def test_read_capture_file_loads_valid_ndjson(tmp_path) -> None:
    path = tmp_path / "capture.ndjson"
    path.write_text(json.dumps(_record(["item-a"])) + "\n", encoding="utf-8")

    records = read_capture_file(path)

    assert len(records) == 1
    assert records[0]["results"][0]["item_id"] == "item-a"
