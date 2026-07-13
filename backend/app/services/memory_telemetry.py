from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Iterable

_lock = threading.Lock()
_semantic_recall_counts: dict[tuple[str, str], int] = defaultdict(int)
_retention_extraction_counts: dict[tuple[str, str], int] = defaultdict(int)
_scope_guard_violation_counts: dict[str, int] = defaultdict(int)
_embedding_request_counts: dict[tuple[str, str, str], int] = defaultdict(int)
_retrieval_request_counts: dict[tuple[str, ...], int] = defaultdict(int)
_retrieval_classification_counts: dict[tuple[str, ...], int] = defaultdict(int)
_retrieval_result_counts: dict[tuple[str, ...], int] = defaultdict(int)
_histograms: dict[tuple[str, tuple[str, ...]], dict[str, object]] = {}

RETRIEVAL_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
EMBEDDING_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
EMBEDDING_SIZE_BUCKETS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0)
EMBEDDING_TOKEN_BUCKETS = (64.0, 128.0, 256.0, 512.0, 1024.0, 2048.0, 4096.0, 8192.0, 16384.0, 32768.0, 65536.0, 131072.0, 285000.0)

_ENDPOINTS = {"retrieve", "retrieve_agent", "semantic_recall", "other"}
_OUTCOMES = {"success", "degraded", "error"}
_INTENTS = {"default", "canonical_factual", "latest_status", "historical", "exploratory", "other"}
_CONFIDENCE = {"high", "medium", "low", "none", "other"}
_STAGES = {"embedding", "routing", "scoped_search", "broad_rescue", "rerank", "merge", "total"}
_RANK_BANDS = {"1", "2_3", "4_10", "11_plus"}
_FRESHNESS = {"fresh", "stale", "unknown"}
_TRUST = {"source_backed", "curated_memory", "generated_artifact", "raw", "unknown", "other"}
_SUPPORT = {"source_backed", "direct_source", "promoted", "unsupported", "unknown", "other"}
_PROVIDERS = {"openai", "local_http", "other"}
_INPUT_TYPES = {"query", "document", "image", "other"}
_EMBEDDING_STATUS = {"success", "retry", "error", "other"}
_FAILURE_KINDS = {"none", "timeout", "connection", "rate_limit", "quota", "http_status", "validation", "input_too_large", "other"}


def _bounded(value: object, allowed: set[str], default: str = "other") -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in allowed else default


def _observe(name: str, labels: tuple[str, ...], value: float, buckets: Iterable[float]) -> None:
    if not math.isfinite(value) or value < 0:
        return
    key = (name, labels)
    bucket_tuple = tuple(buckets)
    state = _histograms.setdefault(
        key,
        {"buckets": bucket_tuple, "counts": [0] * len(bucket_tuple), "count": 0, "sum": 0.0},
    )
    counts = state["counts"]
    assert isinstance(counts, list)
    for index, boundary in enumerate(bucket_tuple):
        if value <= boundary:
            counts[index] += 1
    state["count"] = int(state["count"]) + 1
    state["sum"] = float(state["sum"]) + value


def record_semantic_recall(*, status: str, scope_type: str) -> None:
    with _lock:
        _semantic_recall_counts[(status, scope_type)] += 1


def record_retention_extraction(*, status: str, mode: str) -> None:
    with _lock:
        _retention_extraction_counts[(status, mode)] += 1


def record_scope_guard_violation(*, reason: str) -> None:
    with _lock:
        _scope_guard_violation_counts[reason] += 1


def record_embedding_request(
    *,
    status: str,
    failure_kind: str,
    retryable: bool,
    provider: str | None = None,
    input_type: str | None = None,
    duration_seconds: float | None = None,
    batch_size: int | None = None,
    input_tokens: int | None = None,
) -> None:
    """Record bounded provider telemetry without provider messages, input, or IDs."""
    bounded_status = _bounded(status, _EMBEDDING_STATUS)
    bounded_failure = _bounded(failure_kind, _FAILURE_KINDS)
    bounded_provider = _bounded(provider, _PROVIDERS)
    bounded_input = _bounded(input_type, _INPUT_TYPES)
    with _lock:
        # Preserve the original metric label contract.
        _embedding_request_counts[(bounded_status, bounded_failure, str(retryable).lower())] += 1
        detailed_labels = (bounded_provider, bounded_input, bounded_status, bounded_failure)
        if duration_seconds is not None:
            _observe("embedding_duration", detailed_labels, duration_seconds, EMBEDDING_DURATION_BUCKETS)
        if batch_size is not None:
            _observe("embedding_batch_size", (bounded_provider, bounded_input), float(batch_size), EMBEDDING_SIZE_BUCKETS)
        if input_tokens is not None:
            _observe("embedding_input_tokens", (bounded_provider, bounded_input), float(input_tokens), EMBEDDING_TOKEN_BUCKETS)


def record_retrieval(
    *,
    endpoint: str,
    outcome: str,
    intent: str | None = None,
    route_confidence: str | None = None,
    fallback_used: bool = False,
    abstained: bool = False,
    empty: bool = False,
    budget_truncated: bool = False,
    stage_seconds: dict[str, float | None] | None = None,
    results: Iterable[object] = (),
) -> None:
    bounded_endpoint = _bounded(endpoint, _ENDPOINTS)
    bounded_outcome = _bounded(outcome, _OUTCOMES)
    bounded_intent = _bounded(intent, _INTENTS, "default" if intent is None else "other")
    bounded_confidence = _bounded(route_confidence, _CONFIDENCE, "none" if route_confidence is None else "other")
    decisions = {
        "intent": bounded_intent,
        "route_confidence": bounded_confidence,
        "fallback_used": str(bool(fallback_used)).lower(),
        "abstained": str(bool(abstained)).lower(),
        "empty": str(bool(empty)).lower(),
        "budget_truncated": str(bool(budget_truncated)).lower(),
    }
    with _lock:
        _retrieval_request_counts[(bounded_endpoint, bounded_outcome)] += 1
        for dimension, value in decisions.items():
            _retrieval_classification_counts[(bounded_endpoint, dimension, value)] += 1
        for stage, duration in (stage_seconds or {}).items():
            bounded_stage = _bounded(stage, _STAGES)
            if bounded_stage != "other" and duration is not None:
                _observe("retrieval_stage_duration", (bounded_endpoint, bounded_stage), duration, RETRIEVAL_DURATION_BUCKETS)
        for rank, result in enumerate(results, start=1):
            band = "1" if rank == 1 else "2_3" if rank <= 3 else "4_10" if rank <= 10 else "11_plus"
            freshness = _bounded(getattr(result, "freshness", None), _FRESHNESS, "unknown")
            trust = _bounded(getattr(result, "trust_class", None), _TRUST, "unknown")
            support = _bounded(getattr(result, "source_support_state", None), _SUPPORT, "unknown")
            _retrieval_result_counts[(bounded_endpoint, band, freshness, trust, support)] += 1


def memory_telemetry_snapshot() -> dict[str, object]:
    with _lock:
        return {
            "semantic_recall": list(sorted(_semantic_recall_counts.items())),
            "retention_extraction": list(sorted(_retention_extraction_counts.items())),
            "scope_guard_violations": [((reason,), count) for reason, count in sorted(_scope_guard_violation_counts.items())],
            "embedding_requests": list(sorted(_embedding_request_counts.items())),
            "retrieval_requests": list(sorted(_retrieval_request_counts.items())),
            "retrieval_classifications": list(sorted(_retrieval_classification_counts.items())),
            "retrieval_results": list(sorted(_retrieval_result_counts.items())),
            "histograms": [
                (
                    name,
                    labels,
                    {
                        "buckets": tuple(state["buckets"]),
                        "counts": list(state["counts"]),
                        "count": state["count"],
                        "sum": state["sum"],
                    },
                )
                for (name, labels), state in sorted(_histograms.items())
            ],
        }


def reset_memory_telemetry_for_tests() -> None:
    with _lock:
        _semantic_recall_counts.clear()
        _retention_extraction_counts.clear()
        _scope_guard_violation_counts.clear()
        _embedding_request_counts.clear()
        _retrieval_request_counts.clear()
        _retrieval_classification_counts.clear()
        _retrieval_result_counts.clear()
        _histograms.clear()
