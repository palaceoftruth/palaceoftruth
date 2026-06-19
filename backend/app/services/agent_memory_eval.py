from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class AgentMemoryEvalInputError(ValueError):
    pass


@dataclass(frozen=True)
class AgentMemoryEvalThresholds:
    recall_at_k: float = 0.8
    precision_at_k: float = 0.1
    mrr: float = 0.8
    ndcg_at_k: float = 0.8
    provenance_label_accuracy: float = 1.0
    expected_top_rank_accuracy: float = 1.0
    current_first_accuracy: float = 1.0
    source_coverage_accuracy: float = 1.0
    context_budget_fit_rate: float = 1.0
    forbidden_hit_count: int = 0
    forbidden_context_term_count: int = 0


DEFAULT_AGENT_MEMORY_EVAL_THRESHOLDS = AgentMemoryEvalThresholds()

LIVE_RETRIEVAL_ENDPOINTS = {
    "/api/v1/memory/retrieve",
    "/api/v1/memory/retrieve-agent",
}
COMPATIBILITY_FIXTURE_TRANSPORTS = ("rest", "mcp-http", "mcp-stdio", "hermes")
RECALL_CUTOFFS = (1, 3, 5, 10)
PUBLIC_BENCHMARK_SUITES = {
    "longmemeval",
    "locomo",
    "convomem",
    "membench",
}

SECRETISH_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|secret|token|password)\s*[:=]\s*(?:Bearer\s+)?\S+"
)
WORD_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class RerankCandidate:
    item_id: str
    original_rank: int
    baseline_score: float | None
    packed_text: str


@dataclass(frozen=True)
class RerankDecision:
    item_id: str
    score: float
    reason: str


class SecondStageReranker(Protocol):
    name: str

    def rerank(self, *, query: str, candidates: list[RerankCandidate]) -> list[RerankDecision]:
        ...


@dataclass(frozen=True)
class LexicalOverlapReranker:
    """Deterministic local reranker for report-only precision ablations."""

    name: str = "lexical-overlap"

    def rerank(self, *, query: str, candidates: list[RerankCandidate]) -> list[RerankDecision]:
        query_terms = set(_tokens(query))
        decisions: list[RerankDecision] = []
        for candidate in candidates:
            candidate_terms = set(_tokens(candidate.packed_text))
            overlap = len(query_terms & candidate_terms)
            score = (
                overlap
                + (candidate.baseline_score or 0.0) * 0.001
                + 1 / (candidate.original_rank + 1000)
            )
            decisions.append(
                RerankDecision(
                    item_id=candidate.item_id,
                    score=round(score, 6),
                    reason=f"query_term_overlap={overlap}",
                )
            )
        return decisions


@dataclass(frozen=True)
class StaticScoreReranker:
    name: str
    scores: dict[str, float]

    def rerank(self, *, query: str, candidates: list[RerankCandidate]) -> list[RerankDecision]:
        return [
            RerankDecision(
                item_id=candidate.item_id,
                score=float(self.scores.get(candidate.item_id, 0.0)),
                reason="static_score",
            )
            for candidate in candidates
        ]


def public_benchmark_case_to_eval_case(row: dict[str, Any], *, suite: str) -> dict[str, Any]:
    normalized_suite = suite.lower().strip()
    if normalized_suite not in PUBLIC_BENCHMARK_SUITES:
        raise AgentMemoryEvalInputError(
            f"unsupported public benchmark suite: {suite}"
        )
    if not isinstance(row, dict):
        raise AgentMemoryEvalInputError("public benchmark row must be an object")

    case_id = _first_text(row, "id", "question_id", "qid", "custom_id", "sample_id")
    query = _first_text(row, "query", "question", "prompt")
    if not case_id:
        raise AgentMemoryEvalInputError(f"{normalized_suite}: missing case id")
    if not query:
        raise AgentMemoryEvalInputError(f"{normalized_suite}:{case_id}: missing query")

    expected = _expected_item_ids(row)
    if not expected:
        raise AgentMemoryEvalInputError(
            f"{normalized_suite}:{case_id}: missing expected item/session ids"
        )

    case: dict[str, Any] = {
        "id": f"{normalized_suite}:{case_id}",
        "benchmark_suite": normalized_suite,
        "category": _first_text(
            row,
            "category",
            "question_type",
            "type",
            "evidence_category",
            "memory_type",
        ),
        "query": query,
        "expected_item_ids": expected,
        "forbidden_item_ids": _list_text(row.get("forbidden_item_ids")),
        "relevance": _relevance_from_public_row(row, expected),
        "results": _results_from_public_row(row),
        "retrieval_mode": _first_text(row, "retrieval_mode", "mode") or "offline",
        "llm_api_calls_used": bool(row.get("llm_api_calls_used", False)),
        "ingest_time_ms": _optional_number(row.get("ingest_time_ms")),
        "index_time_ms": _optional_number(row.get("index_time_ms")),
        "latency_ms": _optional_number(row.get("latency_ms")),
        "display_limit": row.get("display_limit"),
        "tags": row.get("tags"),
    }
    return {key: value for key, value in case.items() if value not in (None, [], {})}


def public_benchmark_rows_to_eval_pack(
    rows: list[dict[str, Any]],
    *,
    suite: str,
    pack_id: str | None = None,
    artifact_metadata: dict[str, Any] | None = None,
    benchmark_targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not rows:
        raise AgentMemoryEvalInputError("public benchmark rows must be non-empty")
    normalized_suite = suite.lower().strip()
    payload = {
        "schema_version": 1,
        "pack_id": pack_id or f"{normalized_suite}-public-memory-adapter",
        "description": f"Public {normalized_suite} memory benchmark adapter pack.",
        "artifact_metadata": {
            "adapter_suite": normalized_suite,
            **(artifact_metadata or {}),
        },
        "benchmark_targets": benchmark_targets or {},
        "cases": [
            public_benchmark_case_to_eval_case(row, suite=normalized_suite)
            for row in rows
        ],
    }
    validate_eval_pack(payload)
    return payload


def read_public_benchmark_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise AgentMemoryEvalInputError(f"public benchmark input does not exist: {path}")
    rows: list[dict[str, Any]] = []
    if path.suffix.lower() == ".jsonl":
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AgentMemoryEvalInputError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise AgentMemoryEvalInputError(f"{path}:{line_number}: row must be an object")
            rows.append(row)
        return rows

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentMemoryEvalInputError(f"{path}: invalid JSON: {exc.msg}") from exc
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("cases", "questions", "data", "rows", "qa"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            rows = [payload]
    else:
        raise AgentMemoryEvalInputError(f"{path}: expected object, list, or JSONL rows")
    if not all(isinstance(row, dict) for row in rows):
        raise AgentMemoryEvalInputError(f"{path}: all public benchmark rows must be objects")
    return rows


def read_public_benchmark_eval_pack(
    path: Path,
    *,
    suite: str,
    pack_id: str | None = None,
    artifact_metadata: dict[str, Any] | None = None,
    benchmark_targets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return public_benchmark_rows_to_eval_pack(
        read_public_benchmark_rows(path),
        suite=suite,
        pack_id=pack_id,
        artifact_metadata=artifact_metadata,
        benchmark_targets=benchmark_targets,
    )


def read_eval_pack(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentMemoryEvalInputError(f"eval pack does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentMemoryEvalInputError(f"{path}: invalid JSON: {exc.msg}") from exc
    validate_eval_pack(payload, path=path)
    return payload


def read_compatibility_fixture_eval_pack(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AgentMemoryEvalInputError(f"compatibility fixture pack does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentMemoryEvalInputError(f"{path}: invalid JSON: {exc.msg}") from exc
    return compatibility_fixture_pack_to_eval_pack(payload, path=path)


def validate_compatibility_fixture_pack(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
    required_transports: tuple[str, ...] = COMPATIBILITY_FIXTURE_TRANSPORTS,
) -> None:
    location = str(path) if path else "compatibility fixture pack"
    if payload.get("schema_version") != 1:
        raise AgentMemoryEvalInputError(f"{location}: unsupported schema_version")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise AgentMemoryEvalInputError(f"{location}: cases must be a non-empty list")
    _assert_sanitized_fixture_payload(payload, location)
    for index, case in enumerate(cases):
        case_location = f"{location}:cases[{index}]"
        if not isinstance(case, dict):
            raise AgentMemoryEvalInputError(f"{case_location}: case must be an object")
        if not str(case.get("id") or "").strip():
            raise AgentMemoryEvalInputError(f"{case_location}: missing id")
        if not str(case.get("query") or "").strip():
            raise AgentMemoryEvalInputError(f"{case_location}: missing query")
        if not isinstance(case.get("expected_item_ids"), list):
            raise AgentMemoryEvalInputError(f"{case_location}: expected_item_ids must be a list")
        outputs = case.get("transport_outputs")
        if not isinstance(outputs, dict):
            raise AgentMemoryEvalInputError(f"{case_location}: transport_outputs must be an object")
        missing = [transport for transport in required_transports if transport not in outputs]
        if missing:
            raise AgentMemoryEvalInputError(
                f"{case_location}: missing transport output(s): {', '.join(missing)}"
            )
        for transport, output in outputs.items():
            output_location = f"{case_location}:transport_outputs[{transport}]"
            if transport not in COMPATIBILITY_FIXTURE_TRANSPORTS:
                raise AgentMemoryEvalInputError(f"{output_location}: unsupported transport")
            if not isinstance(output, dict):
                raise AgentMemoryEvalInputError(f"{output_location}: output must be an object")
            results = output.get("results", [])
            if not isinstance(results, list):
                raise AgentMemoryEvalInputError(f"{output_location}: results must be a list")
            if transport == "hermes" and not str(output.get("hermes_context") or "").strip():
                raise AgentMemoryEvalInputError(f"{output_location}: missing hermes_context")


def compatibility_fixture_pack_to_eval_pack(
    payload: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    validate_compatibility_fixture_pack(payload, path=path)
    cases: list[dict[str, Any]] = []
    for case in payload["cases"]:
        base_case = {
            key: value
            for key, value in case.items()
            if key not in {"transport_outputs", "transport_notes"}
        }
        case_id = str(case["id"])
        for transport in COMPATIBILITY_FIXTURE_TRANSPORTS:
            output = case["transport_outputs"][transport]
            trace = output.get("trace") if isinstance(output.get("trace"), dict) else {}
            converted = {
                **base_case,
                "id": f"{case_id}::{transport}",
                "compatibility_case_id": case_id,
                "compatibility_transport": transport,
                "retrieval_mode": "offline-compatibility-fixture",
                "results": output.get("results", []),
                "fallback_used": bool(output.get("fallback_used", trace.get("fallback_used", False))),
            }
            route = output.get("route") or _response_route(trace)
            if route is not None:
                converted["route"] = route
            if output.get("display_limit") is not None:
                converted["display_limit"] = output["display_limit"]
            if output.get("context_budget_chars") is not None:
                converted["context_budget_chars"] = output["context_budget_chars"]
            if "hermes_context" in output:
                converted["hermes_context"] = output["hermes_context"]
            elif transport != "hermes":
                converted.pop("expected_hermes_scope_labels", None)
                converted.pop("expected_hermes_titles", None)
            cases.append({key: value for key, value in converted.items() if value not in (None, [], {})})

    eval_pack = {
        "schema_version": 1,
        "pack_id": payload.get("pack_id"),
        "description": payload.get("description"),
        "artifact_metadata": {
            "adapter_suite": "palace-agent-memory-compatibility",
            "offline_report_only": True,
            "required_transports": list(COMPATIBILITY_FIXTURE_TRANSPORTS),
            **(payload.get("artifact_metadata") if isinstance(payload.get("artifact_metadata"), dict) else {}),
        },
        "benchmark_targets": payload.get("benchmark_targets") or {},
        "cases": cases,
    }
    validate_eval_pack(eval_pack, path=path)
    return eval_pack


def validate_eval_pack(payload: dict[str, Any], *, path: Path | None = None) -> None:
    location = str(path) if path else "eval pack"
    if payload.get("schema_version") != 1:
        raise AgentMemoryEvalInputError(f"{location}: unsupported schema_version")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise AgentMemoryEvalInputError(f"{location}: cases must be a non-empty list")
    for index, case in enumerate(cases):
        case_location = f"{location}:cases[{index}]"
        if not isinstance(case, dict):
            raise AgentMemoryEvalInputError(f"{case_location}: case must be an object")
        if not str(case.get("id") or "").strip():
            raise AgentMemoryEvalInputError(f"{case_location}: missing id")
        if not str(case.get("query") or "").strip():
            raise AgentMemoryEvalInputError(f"{case_location}: missing query")
        results = case.get("results", [])
        if not isinstance(results, list):
            raise AgentMemoryEvalInputError(f"{case_location}: results must be a list")
        expected_ids = case.get("expected_item_ids")
        if not isinstance(expected_ids, list):
            raise AgentMemoryEvalInputError(
                f"{case_location}: expected_item_ids must be a list"
            )
        endpoint = case.get("endpoint")
        if endpoint is not None and endpoint not in LIVE_RETRIEVAL_ENDPOINTS:
            raise AgentMemoryEvalInputError(
                f"{case_location}: endpoint must be one of {sorted(LIVE_RETRIEVAL_ENDPOINTS)}"
            )
        for result_index, result in enumerate(results):
            result_location = f"{case_location}:results[{result_index}]"
            if not isinstance(result, dict):
                raise AgentMemoryEvalInputError(f"{result_location}: result must be an object")
            if not str(result.get("item_id") or "").strip():
                raise AgentMemoryEvalInputError(f"{result_location}: missing item_id")


def evaluate_eval_pack(
    payload: dict[str, Any],
    *,
    top_k: int = 5,
    thresholds: AgentMemoryEvalThresholds = DEFAULT_AGENT_MEMORY_EVAL_THRESHOLDS,
) -> dict[str, Any]:
    validate_eval_pack(payload)
    if top_k < 1:
        raise AgentMemoryEvalInputError("top_k must be at least 1")

    case_reports = [
        evaluate_case(case, top_k=top_k)
        for case in payload["cases"]
    ]
    aggregate = _aggregate_case_reports(case_reports)
    failures = _threshold_failures(aggregate, thresholds)
    return {
        "schema_version": 1,
        "pack_id": payload.get("pack_id"),
        "description": payload.get("description"),
        "artifact_metadata": payload.get("artifact_metadata"),
        "benchmark_targets": payload.get("benchmark_targets"),
        "top_k": top_k,
        "summary": {
            **aggregate,
            "per_suite": _aggregate_case_groups(case_reports, "benchmark_suite"),
            "per_category": _aggregate_case_groups(case_reports, "category"),
            "per_transport": _aggregate_case_groups(case_reports, "compatibility_transport"),
            "target_comparisons": _target_comparisons(
                aggregate,
                payload.get("benchmark_targets"),
                top_k=top_k,
            ),
            "failure_counts": failures,
            "passed": not failures,
        },
        "cases": case_reports,
    }


def evaluate_reranker_ablation(
    payload: dict[str, Any],
    *,
    rerankers: list[SecondStageReranker],
    top_k: int = 5,
    candidate_limit: int = 20,
    thresholds: AgentMemoryEvalThresholds = DEFAULT_AGENT_MEMORY_EVAL_THRESHOLDS,
) -> dict[str, Any]:
    validate_eval_pack(payload)
    if top_k < 1:
        raise AgentMemoryEvalInputError("top_k must be at least 1")
    if candidate_limit < top_k:
        raise AgentMemoryEvalInputError("candidate_limit must be greater than or equal to top_k")
    if not rerankers:
        raise AgentMemoryEvalInputError("at least one reranker is required")

    baseline = evaluate_eval_pack(payload, top_k=top_k, thresholds=thresholds)
    variants = []
    for reranker in rerankers:
        reranked_payload = rerank_eval_pack(
            payload,
            reranker=reranker,
            candidate_limit=candidate_limit,
        )
        report = evaluate_eval_pack(reranked_payload, top_k=top_k, thresholds=thresholds)
        variants.append(
            {
                "name": reranker.name,
                "candidate_limit": candidate_limit,
                "summary": report["summary"],
                "metric_delta": _metric_delta(baseline["summary"], report["summary"]),
                "false_positive_top_k": _false_positive_top_k(report["cases"], top_k=top_k),
                "source_publication_confusion": _source_publication_confusion(
                    report["cases"],
                    top_k=top_k,
                ),
                "cases": report["cases"],
            }
        )

    return {
        "schema_version": 1,
        "pack_id": payload.get("pack_id"),
        "description": payload.get("description"),
        "top_k": top_k,
        "candidate_limit": candidate_limit,
        "baseline": {
            "summary": baseline["summary"],
            "false_positive_top_k": _false_positive_top_k(baseline["cases"], top_k=top_k),
            "source_publication_confusion": _source_publication_confusion(
                baseline["cases"],
                top_k=top_k,
            ),
        },
        "variants": variants,
    }


def rerank_eval_pack(
    payload: dict[str, Any],
    *,
    reranker: SecondStageReranker,
    candidate_limit: int = 20,
) -> dict[str, Any]:
    validate_eval_pack(payload)
    if candidate_limit < 1:
        raise AgentMemoryEvalInputError("candidate_limit must be at least 1")
    reranked = {
        key: value
        for key, value in payload.items()
        if key != "cases"
    }
    reranked["cases"] = [
        rerank_case(case, reranker=reranker, candidate_limit=candidate_limit)
        for case in payload["cases"]
    ]
    return reranked


def rerank_case(
    case: dict[str, Any],
    *,
    reranker: SecondStageReranker,
    candidate_limit: int = 20,
) -> dict[str, Any]:
    candidates = pack_rerank_candidates(case, candidate_limit=candidate_limit)
    decisions = reranker.rerank(query=str(case.get("query") or ""), candidates=candidates)
    decision_by_id = {decision.item_id: decision for decision in decisions}
    candidate_ids = [candidate.item_id for candidate in candidates]
    original_results = [result for result in case.get("results", []) if isinstance(result, dict)]
    candidate_results = original_results[: len(candidates)]
    remainder = original_results[len(candidates):]
    original_rank_by_id = {candidate.item_id: candidate.original_rank for candidate in candidates}

    def sort_key(result: dict[str, Any]) -> tuple[float, int, str]:
        item_id = str(result.get("item_id"))
        decision = decision_by_id.get(item_id)
        return (
            -(decision.score if decision is not None else float("-inf")),
            original_rank_by_id.get(item_id, 999999),
            item_id,
        )

    ordered = sorted(candidate_results, key=sort_key) + remainder
    converted = {
        key: value
        for key, value in case.items()
        if key != "results"
    }
    converted["results"] = ordered
    converted["reranker_ablation"] = {
        "reranker": reranker.name,
        "candidate_limit": candidate_limit,
        "candidate_count": len(candidates),
        "reranked_item_ids": [str(result.get("item_id")) for result in ordered[: len(candidates)]],
        "decisions": [
            {
                "item_id": item_id,
                "score": decision_by_id[item_id].score,
                "reason": decision_by_id[item_id].reason,
                "original_rank": original_rank_by_id[item_id],
            }
            for item_id in candidate_ids
            if item_id in decision_by_id
        ],
    }
    return converted


def pack_rerank_candidates(
    case: dict[str, Any],
    *,
    candidate_limit: int = 20,
    max_text_chars: int = 600,
) -> list[RerankCandidate]:
    if candidate_limit < 1:
        raise AgentMemoryEvalInputError("candidate_limit must be at least 1")
    candidates: list[RerankCandidate] = []
    for index, result in enumerate(case.get("results", [])[:candidate_limit], start=1):
        if not isinstance(result, dict):
            continue
        item_id = str(result.get("item_id") or "").strip()
        if not item_id:
            continue
        candidates.append(
            RerankCandidate(
                item_id=item_id,
                original_rank=index,
                baseline_score=_optional_number(result.get("score")),
                packed_text=_redact_candidate_text(_candidate_text(result), max_chars=max_text_chars),
            )
        )
    return candidates


def evaluate_case(case: dict[str, Any], *, top_k: int) -> dict[str, Any]:
    expected = {str(item_id) for item_id in case.get("expected_item_ids", [])}
    forbidden = {str(item_id) for item_id in case.get("forbidden_item_ids", [])}
    results = [result for result in case.get("results", []) if isinstance(result, dict)]
    ranked_ids = [str(result.get("item_id")) for result in results]
    top_ids = ranked_ids[:top_k]
    expected_in_top = [item_id for item_id in top_ids if item_id in expected]
    forbidden_hits = [item_id for item_id in ranked_ids if item_id in forbidden]
    first_rank = _first_relevant_rank(ranked_ids, expected)
    relevance_by_id = _relevance_by_item_id(case)
    dcg = _dcg(ranked_ids[:top_k], relevance_by_id)
    ideal_dcg = _dcg(_ideal_ids(relevance_by_id, top_k), relevance_by_id)
    provenance = _provenance_label_report(case, results)
    hermes = _hermes_context_report(case)
    display_limit = int(case.get("display_limit") or top_k)
    fallback_used = bool(case.get("fallback_used"))
    route_accuracy = _route_accuracy(case)
    latency_ms = _latency_ms(case)
    expected_top_rank_item_id = str(case.get("expected_top_rank_item_id") or "").strip()
    expected_top_rank_match = (
        None
        if not expected_top_rank_item_id
        else (ranked_ids[0] == expected_top_rank_item_id if ranked_ids else False)
    )
    current_first = _current_first_report(case, ranked_ids)
    source_coverage = _source_coverage_report(case, ranked_ids[:top_k])
    context_budget = _context_budget_report(case)
    forbidden_context_terms = _forbidden_context_terms(case)

    recall = len(expected_in_top) / len(expected) if expected else 1.0
    precision = len(expected_in_top) / min(top_k, len(results)) if results else 0.0
    return {
        "id": case["id"],
        "query": case["query"],
        "compatibility_case_id": case.get("compatibility_case_id"),
        "compatibility_transport": case.get("compatibility_transport"),
        "benchmark_suite": case.get("benchmark_suite"),
        "category": case.get("category"),
        "retrieval_mode": case.get("retrieval_mode"),
        "llm_api_calls_used": bool(case.get("llm_api_calls_used")),
        "ingest_time_ms": _optional_number(case.get("ingest_time_ms")),
        "index_time_ms": _optional_number(case.get("index_time_ms")),
        "top_k": top_k,
        "display_limit": display_limit,
        "expected_item_ids": sorted(expected),
        "forbidden_item_ids": sorted(forbidden),
        "ranked_item_ids": ranked_ids,
        "recall_at_k": round(recall, 4),
        "recall_at": _recall_by_cutoff(ranked_ids, expected),
        "precision_at_k": round(precision, 4),
        "mrr": round(1 / first_rank, 4) if first_rank else 0.0,
        "ndcg_at_k": round(dcg / ideal_dcg, 4) if ideal_dcg else 1.0,
        "first_relevant_rank": first_rank,
        "expected_top_rank_item_id": expected_top_rank_item_id or None,
        "expected_top_rank_match": expected_top_rank_match,
        "current_item_ids": current_first["current_item_ids"],
        "stale_item_ids": current_first["stale_item_ids"],
        "interfering_item_ids": current_first["interfering_item_ids"],
        "current_first_match": current_first["match"],
        "current_first_failures": current_first["failures"],
        "source_coverage_accuracy": source_coverage["accuracy"],
        "missing_source_coverage_groups": source_coverage["missing_groups"],
        "context_budget_chars": context_budget["budget_chars"],
        "hermes_context_chars": context_budget["actual_chars"],
        "context_budget_fit": context_budget["fit"],
        "forbidden_context_term_count": len(forbidden_context_terms),
        "forbidden_context_terms": forbidden_context_terms,
        "forbidden_hit_count": len(forbidden_hits),
        "forbidden_hits": forbidden_hits,
        "provenance_label_accuracy": provenance["accuracy"],
        "provenance_label_failures": provenance["failures"],
        "hermes_context_label_failures": hermes["label_failures"],
        "hermes_context_title_failures": hermes["title_failures"],
        "fallback_used": fallback_used,
        "route": case.get("route"),
        "expected_route": case.get("expected_route"),
        "route_accuracy": route_accuracy,
        "latency_ms": latency_ms,
        "display_cap_hides_relevant": _display_cap_hides_relevant(
            ranked_ids,
            expected,
            display_limit,
        ),
    }


def build_live_retrieval_request(
    case: dict[str, Any],
    *,
    endpoint: str,
    top_k: int,
    candidate_limit: int | None = None,
    broad_candidate_limit: int | None = None,
    display_limit: int | None = None,
) -> dict[str, Any]:
    if endpoint not in LIVE_RETRIEVAL_ENDPOINTS:
        raise AgentMemoryEvalInputError(f"unsupported live endpoint: {endpoint}")

    query = str(case.get("query") or "").strip()
    if not query:
        raise AgentMemoryEvalInputError(f"{case.get('id', 'case')}: missing query")

    payload: dict[str, Any] = {
        "query": query,
        "tags": case.get("tags"),
        "tags_mode": case.get("tags_mode", "any"),
        "min_score": case.get("min_score"),
        "date_from": case.get("date_from"),
        "date_to": case.get("date_to"),
    }
    payload = {key: value for key, value in payload.items() if value is not None}

    if endpoint == "/api/v1/memory/retrieve":
        payload["limit"] = int(case.get("limit") or candidate_limit or top_k)
        payload["scope"] = case.get("scope", {"type": "tenant_shared"})
        if case.get("room_id") is not None:
            payload["room_id"] = case["room_id"]
        return payload

    resolved_display_limit = int(case.get("display_limit") or display_limit or top_k)
    resolved_candidate_limit = int(
        case.get("candidate_limit")
        or candidate_limit
        or max(resolved_display_limit, top_k)
    )
    payload.update(
        {
            "agent_scope_key": case.get("agent_scope_key"),
            "workspace_scope_keys": case.get("workspace_scope_keys", []),
            "session_scope_key": case.get("session_scope_key"),
            "include_tenant_shared": case.get("include_tenant_shared", True),
            "include_broad_corpus": case.get("include_broad_corpus", True),
            "limit": resolved_display_limit,
            "candidate_limit": resolved_candidate_limit,
            "broad_candidate_limit": int(
                case.get("broad_candidate_limit")
                or broad_candidate_limit
                or resolved_candidate_limit
            ),
            "display_limit": resolved_display_limit,
            "context_budget_chars": case.get("context_budget_chars"),
        }
    )
    return {key: value for key, value in payload.items() if value is not None}


def case_from_live_response(
    case: dict[str, Any],
    *,
    endpoint: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    latency_ms: float,
) -> dict[str, Any]:
    if endpoint not in LIVE_RETRIEVAL_ENDPOINTS:
        raise AgentMemoryEvalInputError(f"unsupported live endpoint: {endpoint}")
    if not isinstance(response_payload, dict):
        raise AgentMemoryEvalInputError(f"{case.get('id', 'case')}: response must be an object")

    trace = response_payload.get("trace") if isinstance(response_payload.get("trace"), dict) else {}
    scope = response_payload.get("scope") if isinstance(response_payload.get("scope"), dict) else None
    if scope is None and endpoint == "/api/v1/memory/retrieve":
        scope = request_payload.get("scope") if isinstance(request_payload.get("scope"), dict) else None

    converted = {
        key: value
        for key, value in case.items()
        if key not in {"results", "hermes_context"}
    }
    converted.update(
        {
            "endpoint": endpoint,
            "display_limit": _response_display_limit(trace, request_payload),
            "results": [
                _result_from_live_response(result, fallback_scope=scope)
                for result in response_payload.get("results", [])
                if isinstance(result, dict)
            ],
            "fallback_used": bool(trace.get("fallback_used")),
            "route": _response_route(trace),
            "latency_ms": round(latency_ms, 2),
            "live_trace": trace,
        }
    )
    return converted


def _relevance_by_item_id(case: dict[str, Any]) -> dict[str, float]:
    relevance = {
        str(item_id): 1.0
        for item_id in case.get("expected_item_ids", [])
    }
    overrides = case.get("relevance")
    if isinstance(overrides, dict):
        for item_id, value in overrides.items():
            if isinstance(value, (int, float)) and value > 0:
                relevance[str(item_id)] = float(value)
    return relevance


def _first_relevant_rank(ranked_ids: list[str], expected: set[str]) -> int | None:
    for index, item_id in enumerate(ranked_ids, start=1):
        if item_id in expected:
            return index
    return None


def _recall_by_cutoff(ranked_ids: list[str], expected: set[str]) -> dict[str, float]:
    if not expected:
        return {str(cutoff): 1.0 for cutoff in RECALL_CUTOFFS}
    return {
        str(cutoff): round(
            len([item_id for item_id in ranked_ids[:cutoff] if item_id in expected])
            / len(expected),
            4,
        )
        for cutoff in RECALL_CUTOFFS
    }


def _dcg(ids: list[str], relevance_by_id: dict[str, float]) -> float:
    total = 0.0
    for index, item_id in enumerate(ids, start=1):
        relevance = relevance_by_id.get(item_id, 0.0)
        total += (2**relevance - 1) / math.log2(index + 1)
    return total


def _ideal_ids(relevance_by_id: dict[str, float], top_k: int) -> list[str]:
    return [
        item_id
        for item_id, _ in sorted(
            relevance_by_id.items(),
            key=lambda entry: (-entry[1], entry[0]),
        )[:top_k]
    ]


def _result_scope_label(result: dict[str, Any]) -> str | None:
    label = result.get("scope_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    scope = result.get("scope")
    if not isinstance(scope, dict):
        return None
    scope_type = str(scope.get("type") or "").strip()
    if scope_type == "tenant_shared":
        return "tenant_shared"
    key = str(scope.get("key") or "").strip()
    if scope_type and key:
        return f"{scope_type}/{key}"
    return scope_type or None


def _provenance_label_report(case: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    expected_labels = case.get("expected_scope_labels")
    if not isinstance(expected_labels, dict):
        expected_labels = {}
    checked = 0
    failures: list[dict[str, str | None]] = []
    for result in results:
        item_id = str(result.get("item_id"))
        expected = expected_labels.get(item_id)
        if expected is None:
            continue
        checked += 1
        actual = _result_scope_label(result)
        if actual != expected:
            failures.append({"item_id": item_id, "expected": expected, "actual": actual})
    accuracy = 1.0 if checked == 0 else (checked - len(failures)) / checked
    return {"accuracy": round(accuracy, 4), "failures": failures}


def _hermes_context_report(case: dict[str, Any]) -> dict[str, Any]:
    context = str(case.get("hermes_context") or "")
    expected_labels = [
        str(label)
        for label in case.get("expected_hermes_scope_labels", [])
        if str(label).strip()
    ]
    expected_titles = [
        str(title)
        for title in case.get("expected_hermes_titles", [])
        if str(title).strip()
    ]
    return {
        "label_failures": [label for label in expected_labels if label not in context],
        "title_failures": [title for title in expected_titles if title not in context],
    }


def _display_cap_hides_relevant(
    ranked_ids: list[str],
    expected: set[str],
    display_limit: int,
) -> bool:
    visible = set(ranked_ids[:display_limit])
    hidden = set(ranked_ids[display_limit:])
    return bool(expected - visible and expected & hidden)


def _route_accuracy(case: dict[str, Any]) -> float | None:
    expected_route = case.get("expected_route")
    if expected_route is None:
        return None
    return 1.0 if case.get("route") == expected_route else 0.0


def _latency_ms(case: dict[str, Any]) -> float | None:
    latency = case.get("latency_ms")
    if isinstance(latency, (int, float)) and not isinstance(latency, bool):
        return round(float(latency), 2)
    return None


def _response_display_limit(trace: dict[str, Any], request_payload: dict[str, Any]) -> int:
    for value in (
        trace.get("display_limit"),
        request_payload.get("display_limit"),
        request_payload.get("limit"),
    ):
        if isinstance(value, int) and value > 0:
            return value
    return 5


def _response_route(trace: dict[str, Any]) -> str | None:
    ranking_traces = trace.get("ranking_traces")
    if isinstance(ranking_traces, list) and ranking_traces:
        first = ranking_traces[0]
        if isinstance(first, dict) and isinstance(first.get("route"), str):
            return first["route"]
    search_ranking_trace = trace.get("search_ranking_trace")
    if isinstance(search_ranking_trace, dict) and isinstance(search_ranking_trace.get("route"), str):
        return search_ranking_trace["route"]
    return None


def _scope_label(scope: dict[str, Any] | None) -> str | None:
    if not isinstance(scope, dict):
        return None
    scope_type = str(scope.get("type") or "").strip()
    if scope_type == "tenant_shared":
        return "tenant_shared"
    key = str(scope.get("key") or "").strip()
    if scope_type and key:
        return f"{scope_type}/{key}"
    return scope_type or None


def _result_from_live_response(
    result: dict[str, Any],
    *,
    fallback_scope: dict[str, Any] | None,
) -> dict[str, Any]:
    converted = {
        "item_id": str(result.get("item_id") or ""),
        "title": result.get("title"),
        "score": result.get("score"),
        "source_type": result.get("source_type"),
        "tags": result.get("tags"),
        "chunk_index": result.get("chunk_index"),
    }
    if isinstance(result.get("scope"), dict):
        converted["scope"] = result["scope"]
    elif isinstance(result.get("scope_label"), str):
        converted["scope_label"] = result["scope_label"]
    elif fallback_scope is not None:
        converted["scope"] = fallback_scope
        label = _scope_label(fallback_scope)
        if label is not None:
            converted["scope_label"] = label
    return {key: value for key, value in converted.items() if value is not None}


def _aggregate_case_reports(case_reports: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(case_reports)
    if count == 0:
        return {
            "case_count": 0,
            "average_recall_at_k": 0.0,
            "average_precision_at_k": 0.0,
            "mean_reciprocal_rank": 0.0,
            "average_ndcg_at_k": 0.0,
            "recall_at": {str(cutoff): 0.0 for cutoff in RECALL_CUTOFFS},
            "provenance_label_accuracy": 0.0,
            "expected_top_rank_accuracy": 0.0,
            "expected_top_rank_failure_cases": [],
            "current_first_accuracy": 0.0,
            "current_first_failure_cases": [],
            "source_coverage_accuracy": 0.0,
            "source_coverage_failure_cases": [],
            "context_budget_fit_rate": 0.0,
            "context_budget_failure_cases": [],
            "forbidden_hit_count": 0,
            "forbidden_context_term_count": 0,
            "forbidden_context_failure_cases": [],
            "fallback_rate": 0.0,
            "route_accuracy": None,
            "average_latency_ms": None,
            "average_ingest_time_ms": None,
            "average_index_time_ms": None,
            "llm_api_case_count": 0,
            "display_cap_hidden_relevant_cases": [],
            "hermes_context_failure_cases": [],
        }
    hermes_failures = [
        report["id"]
        for report in case_reports
        if report["hermes_context_label_failures"] or report["hermes_context_title_failures"]
    ]
    return {
        "case_count": count,
        "average_recall_at_k": _mean(case_reports, "recall_at_k"),
        "average_precision_at_k": _mean(case_reports, "precision_at_k"),
        "mean_reciprocal_rank": _mean(case_reports, "mrr"),
        "average_ndcg_at_k": _mean(case_reports, "ndcg_at_k"),
        "recall_at": {
            str(cutoff): round(
                sum(float(report["recall_at"][str(cutoff)]) for report in case_reports)
                / count,
                4,
            )
            for cutoff in RECALL_CUTOFFS
        },
        "provenance_label_accuracy": _mean(case_reports, "provenance_label_accuracy"),
        "expected_top_rank_accuracy": _expected_top_rank_accuracy(case_reports),
        "expected_top_rank_failure_cases": [
            report["id"] for report in case_reports if report["expected_top_rank_match"] is False
        ],
        "current_first_accuracy": _current_first_accuracy(case_reports),
        "current_first_failure_cases": [
            report["id"] for report in case_reports if report["current_first_match"] is False
        ],
        "source_coverage_accuracy": _mean(case_reports, "source_coverage_accuracy"),
        "source_coverage_failure_cases": [
            report["id"] for report in case_reports if report["missing_source_coverage_groups"]
        ],
        "context_budget_fit_rate": _context_budget_fit_rate(case_reports),
        "context_budget_failure_cases": [
            report["id"] for report in case_reports if report["context_budget_fit"] is False
        ],
        "forbidden_hit_count": sum(report["forbidden_hit_count"] for report in case_reports),
        "forbidden_context_term_count": sum(
            report["forbidden_context_term_count"] for report in case_reports
        ),
        "forbidden_context_failure_cases": [
            report["id"] for report in case_reports if report["forbidden_context_terms"]
        ],
        "fallback_rate": round(
            sum(1 for report in case_reports if report["fallback_used"]) / count,
            4,
        ),
        "route_accuracy": _optional_mean(case_reports, "route_accuracy"),
        "average_latency_ms": _optional_mean(case_reports, "latency_ms"),
        "average_ingest_time_ms": _optional_mean(case_reports, "ingest_time_ms"),
        "average_index_time_ms": _optional_mean(case_reports, "index_time_ms"),
        "llm_api_case_count": sum(1 for report in case_reports if report["llm_api_calls_used"]),
        "display_cap_hidden_relevant_cases": [
            report["id"] for report in case_reports if report["display_cap_hides_relevant"]
        ],
        "hermes_context_failure_cases": hermes_failures,
    }


def _mean(reports: list[dict[str, Any]], key: str) -> float:
    return round(sum(float(report[key]) for report in reports) / len(reports), 4)


def _expected_top_rank_accuracy(reports: list[dict[str, Any]]) -> float:
    checked = [
        report
        for report in reports
        if report["expected_top_rank_match"] is not None
    ]
    if not checked:
        return 1.0
    matches = sum(1 for report in checked if report["expected_top_rank_match"] is True)
    return round(matches / len(checked), 4)


def _current_first_accuracy(reports: list[dict[str, Any]]) -> float:
    checked = [
        report
        for report in reports
        if report["current_first_match"] is not None
    ]
    if not checked:
        return 1.0
    matches = sum(1 for report in checked if report["current_first_match"] is True)
    return round(matches / len(checked), 4)


def _context_budget_fit_rate(reports: list[dict[str, Any]]) -> float:
    checked = [
        report
        for report in reports
        if report["context_budget_fit"] is not None
    ]
    if not checked:
        return 1.0
    matches = sum(1 for report in checked if report["context_budget_fit"] is True)
    return round(matches / len(checked), 4)


def _optional_mean(reports: list[dict[str, Any]], key: str) -> float | None:
    values = [
        float(report[key])
        for report in reports
        if isinstance(report.get(key), (int, float)) and not isinstance(report.get(key), bool)
    ]
    if not values:
        return None
    return round(sum(values) / len(values), 4)


def _optional_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 2)
    return None


def _first_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _list_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, list):
        values: list[str] = []
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                values.append(entry.strip())
            elif isinstance(entry, (int, float)) and not isinstance(entry, bool):
                values.append(str(entry))
            elif isinstance(entry, dict):
                text = _first_text(entry, "item_id", "session_id", "id", "dia_id", "source_id")
                if text:
                    values.append(text)
        return values
    return []


def _expected_item_ids(row: dict[str, Any]) -> list[str]:
    for key in (
        "expected_item_ids",
        "answer_session_ids",
        "evidence_session_ids",
        "evidence_ids",
        "evidence",
        "gold_item_ids",
        "supporting_item_ids",
    ):
        values = _list_text(row.get(key))
        if values:
            return values
    value = _first_text(row, "answer_session_id", "evidence_session_id", "session_id")
    return [value] if value else []


def _results_from_public_row(row: dict[str, Any]) -> list[dict[str, Any]]:
    source = row.get("results") or row.get("retrieved") or row.get("retrieval_results")
    if not isinstance(source, list):
        return []
    results: list[dict[str, Any]] = []
    for index, result in enumerate(source, start=1):
        if not isinstance(result, dict):
            item_id = str(result).strip()
            if item_id:
                results.append({"item_id": item_id})
            continue
        item_id = _first_text(result, "item_id", "session_id", "id", "doc_id", "source_id")
        if not item_id:
            continue
        converted = {
            "item_id": item_id,
            "title": _first_text(result, "title", "session_date", "source"),
            "score": result.get("score"),
            "source_type": result.get("source_type") or row.get("source_type"),
            "tags": result.get("tags") or row.get("tags"),
            "chunk_index": result.get("chunk_index") or result.get("turn_index"),
            "rank": result.get("rank", index),
        }
        results.append({key: value for key, value in converted.items() if value is not None})
    return results


def _relevance_from_public_row(row: dict[str, Any], expected: list[str]) -> dict[str, float]:
    relevance = row.get("relevance") or row.get("qrels")
    if isinstance(relevance, dict):
        return {
            str(item_id): float(value)
            for item_id, value in relevance.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        }
    return {item_id: 1.0 for item_id in expected}


def _current_first_report(case: dict[str, Any], ranked_ids: list[str]) -> dict[str, Any]:
    current_ids = _list_text(case.get("current_item_ids"))
    stale_ids = _list_text(case.get("stale_item_ids"))
    interfering_ids = _list_text(case.get("interfering_item_ids"))
    if not current_ids:
        return {
            "current_item_ids": [],
            "stale_item_ids": stale_ids,
            "interfering_item_ids": interfering_ids,
            "match": None,
            "failures": [],
        }

    rank_by_id = {item_id: index for index, item_id in enumerate(ranked_ids, start=1)}
    blocker_ids = stale_ids + interfering_ids
    failures: list[dict[str, Any]] = []
    for current_id in current_ids:
        current_rank = rank_by_id.get(current_id)
        if current_rank is None:
            failures.append({"item_id": current_id, "reason": "missing_current_item"})
            continue
        for blocker_id in blocker_ids:
            blocker_rank = rank_by_id.get(blocker_id)
            if blocker_rank is not None and blocker_rank < current_rank:
                failures.append(
                    {
                        "item_id": current_id,
                        "reason": "stale_or_interfering_ranked_first",
                        "blocking_item_id": blocker_id,
                    }
                )
                break
    return {
        "current_item_ids": current_ids,
        "stale_item_ids": stale_ids,
        "interfering_item_ids": interfering_ids,
        "match": not failures,
        "failures": failures,
    }


def _source_coverage_report(case: dict[str, Any], top_ids: list[str]) -> dict[str, Any]:
    groups = case.get("expected_source_groups")
    if not isinstance(groups, dict) or not groups:
        return {"accuracy": 1.0, "missing_groups": []}

    top_id_set = set(top_ids)
    missing: list[str] = []
    checked = 0
    for group_name, item_ids in groups.items():
        expected_ids = _list_text(item_ids)
        if not expected_ids:
            continue
        checked += 1
        if not top_id_set.intersection(expected_ids):
            missing.append(str(group_name))
    if checked == 0:
        return {"accuracy": 1.0, "missing_groups": []}
    return {
        "accuracy": round((checked - len(missing)) / checked, 4),
        "missing_groups": missing,
    }


def _context_budget_report(case: dict[str, Any]) -> dict[str, Any]:
    budget = case.get("context_budget_chars")
    if not isinstance(budget, int) or budget < 1:
        return {"budget_chars": None, "actual_chars": None, "fit": None}
    actual = len(str(case.get("hermes_context") or ""))
    return {
        "budget_chars": budget,
        "actual_chars": actual,
        "fit": actual <= budget,
    }


def _forbidden_context_terms(case: dict[str, Any]) -> list[str]:
    terms = _list_text(case.get("forbidden_context_terms"))
    if not terms:
        return []
    context = str(case.get("hermes_context") or "")
    context_lower = context.lower()
    return [term for term in terms if term.lower() in context_lower]


def _assert_sanitized_fixture_payload(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {"body", "raw_body", "memory_body", "raw_memory_body"}:
                raise AgentMemoryEvalInputError(
                    f"{location}: raw memory body field is not allowed in compatibility fixtures"
                )
            _assert_sanitized_fixture_payload(item, f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_sanitized_fixture_payload(item, f"{location}[{index}]")
        return
    if isinstance(value, str) and SECRETISH_PATTERN.search(value):
        raise AgentMemoryEvalInputError(
            f"{location}: secret-like value is not allowed in compatibility fixtures"
        )


def _aggregate_case_groups(case_reports: list[dict[str, Any]], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for report in case_reports:
        value = report.get(key)
        if isinstance(value, str) and value.strip():
            groups.setdefault(value.strip(), []).append(report)
    return {
        name: {
            "case_count": len(rows),
            "average_recall_at_k": _mean(rows, "recall_at_k"),
            "mean_reciprocal_rank": _mean(rows, "mrr"),
            "average_ndcg_at_k": _mean(rows, "ndcg_at_k"),
            "recall_at": {
                str(cutoff): round(
                    sum(float(report["recall_at"][str(cutoff)]) for report in rows)
                    / len(rows),
                    4,
                )
                for cutoff in RECALL_CUTOFFS
            },
            "average_latency_ms": _optional_mean(rows, "latency_ms"),
            "average_ingest_time_ms": _optional_mean(rows, "ingest_time_ms"),
            "average_index_time_ms": _optional_mean(rows, "index_time_ms"),
            "llm_api_case_count": sum(1 for report in rows if report["llm_api_calls_used"]),
        }
        for name, rows in sorted(groups.items())
    }


def _target_comparisons(
    aggregate: dict[str, Any],
    targets: Any,
    *,
    top_k: int,
) -> dict[str, Any]:
    if not isinstance(targets, dict):
        return {}
    comparisons: dict[str, Any] = {}
    for name, target in sorted(targets.items()):
        if not isinstance(target, dict):
            continue
        metric = str(target.get("metric") or "").strip()
        threshold = target.get("value")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            continue
        actual = _target_metric_value(aggregate, metric, top_k=top_k)
        comparisons[name] = {
            "metric": metric,
            "target": float(threshold),
            "actual": actual,
            "passed": actual is not None and actual >= float(threshold),
        }
    return comparisons


def _target_metric_value(aggregate: dict[str, Any], metric: str, *, top_k: int) -> float | None:
    normalized = metric.lower().replace("@k", f"@{top_k}")
    if normalized in {"recall", "average_recall", f"recall@{top_k}", f"r@{top_k}"}:
        return float(aggregate["average_recall_at_k"])
    if normalized in {
        "precision",
        "average_precision",
        f"precision@{top_k}",
        f"p@{top_k}",
    }:
        return float(aggregate["average_precision_at_k"])
    if normalized in {"mrr", "mean_reciprocal_rank"}:
        return float(aggregate["mean_reciprocal_rank"])
    if normalized in {f"ndcg@{top_k}", f"ndcg_at_{top_k}"}:
        return float(aggregate["average_ndcg_at_k"])
    if normalized.startswith("recall@") or normalized.startswith("r@"):
        cutoff = normalized.split("@", 1)[1]
        value = aggregate["recall_at"].get(cutoff)
        return float(value) if value is not None else None
    return None


def _metric_delta(baseline: dict[str, Any], variant: dict[str, Any]) -> dict[str, float]:
    keys = (
        "average_recall_at_k",
        "average_precision_at_k",
        "mean_reciprocal_rank",
        "average_ndcg_at_k",
        "average_latency_ms",
    )
    delta: dict[str, float] = {}
    for key in keys:
        before = baseline.get(key)
        after = variant.get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            delta[key] = round(float(after) - float(before), 4)
    return delta


def _false_positive_top_k(case_reports: list[dict[str, Any]], *, top_k: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report in case_reports:
        expected = set(report.get("expected_item_ids") or [])
        forbidden = set(report.get("forbidden_item_ids") or [])
        for rank, item_id in enumerate(report.get("ranked_item_ids", [])[:top_k], start=1):
            if item_id in expected:
                continue
            rows.append(
                {
                    "case_id": report["id"],
                    "rank": rank,
                    "item_id": item_id,
                    "known_forbidden": item_id in forbidden,
                }
            )
    return rows


def _source_publication_confusion(
    case_reports: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report in case_reports:
        expected_sources = _expected_source_tokens(report)
        if not expected_sources:
            continue
        ranked = report.get("ranked_item_ids", [])[:top_k]
        expected = set(report.get("expected_item_ids") or [])
        for rank, item_id in enumerate(ranked, start=1):
            if item_id in expected:
                continue
            item_tokens = set(_tokens(str(item_id)))
            if item_tokens & expected_sources:
                rows.append(
                    {
                        "case_id": report["id"],
                        "rank": rank,
                        "item_id": item_id,
                        "matched_expected_source_tokens": sorted(item_tokens & expected_sources),
                    }
                )
    return rows


def _expected_source_tokens(report: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for item_id in report.get("expected_item_ids") or []:
        tokens.update(_tokens(str(item_id)))
    return {
        token
        for token in tokens
        if len(token) >= 3 and not token.isdigit()
    }


def _candidate_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "title",
        "summary",
        "source",
        "source_title",
        "publication",
        "source_type",
        "text",
        "chunk_text",
        "content",
        "excerpt",
    ):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    tags = result.get("tags")
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags if str(tag).strip())
    scope_label = result.get("scope_label")
    if isinstance(scope_label, str) and scope_label.strip():
        parts.append(scope_label.strip())
    return "\n".join(parts)


def _redact_candidate_text(text: str, *, max_chars: int) -> str:
    redacted = SECRETISH_PATTERN.sub(r"\1=[REDACTED]", text)
    redacted = " ".join(redacted.split())
    if len(redacted) <= max_chars:
        return redacted
    return redacted[: max_chars - 3].rstrip() + "..."


def _tokens(value: str) -> list[str]:
    return WORD_PATTERN.findall(value.lower())


def _threshold_failures(
    aggregate: dict[str, Any],
    thresholds: AgentMemoryEvalThresholds,
) -> dict[str, int]:
    failures: dict[str, int] = {}
    if aggregate["average_recall_at_k"] < thresholds.recall_at_k:
        failures["recall_at_k"] = 1
    if aggregate["average_precision_at_k"] < thresholds.precision_at_k:
        failures["precision_at_k"] = 1
    if aggregate["mean_reciprocal_rank"] < thresholds.mrr:
        failures["mrr"] = 1
    if aggregate["average_ndcg_at_k"] < thresholds.ndcg_at_k:
        failures["ndcg_at_k"] = 1
    if aggregate["provenance_label_accuracy"] < thresholds.provenance_label_accuracy:
        failures["provenance_label_accuracy"] = 1
    if aggregate["expected_top_rank_accuracy"] < thresholds.expected_top_rank_accuracy:
        failures["expected_top_rank_accuracy"] = len(aggregate["expected_top_rank_failure_cases"])
    if aggregate["current_first_accuracy"] < thresholds.current_first_accuracy:
        failures["current_first_accuracy"] = len(aggregate["current_first_failure_cases"])
    if aggregate["source_coverage_accuracy"] < thresholds.source_coverage_accuracy:
        failures["source_coverage_accuracy"] = len(aggregate["source_coverage_failure_cases"])
    if aggregate["context_budget_fit_rate"] < thresholds.context_budget_fit_rate:
        failures["context_budget_fit_rate"] = len(aggregate["context_budget_failure_cases"])
    if aggregate["forbidden_hit_count"] > thresholds.forbidden_hit_count:
        failures["forbidden_hit_count"] = aggregate["forbidden_hit_count"]
    if aggregate["forbidden_context_term_count"] > thresholds.forbidden_context_term_count:
        failures["forbidden_context_term_count"] = aggregate["forbidden_context_term_count"]
    if aggregate["hermes_context_failure_cases"]:
        failures["hermes_context"] = len(aggregate["hermes_context_failure_cases"])
    return failures
