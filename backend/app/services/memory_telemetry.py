from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_semantic_recall_counts: dict[tuple[str, str], int] = defaultdict(int)
_retention_extraction_counts: dict[tuple[str, str], int] = defaultdict(int)
_scope_guard_violation_counts: dict[str, int] = defaultdict(int)
_embedding_request_counts: dict[tuple[str, str, str], int] = defaultdict(int)


def record_semantic_recall(*, status: str, scope_type: str) -> None:
    with _lock:
        _semantic_recall_counts[(status, scope_type)] += 1


def record_retention_extraction(*, status: str, mode: str) -> None:
    with _lock:
        _retention_extraction_counts[(status, mode)] += 1


def record_scope_guard_violation(*, reason: str) -> None:
    with _lock:
        _scope_guard_violation_counts[reason] += 1


def record_embedding_request(*, status: str, failure_kind: str, retryable: bool) -> None:
    """Record bounded embedding request outcomes without provider messages or IDs."""
    with _lock:
        _embedding_request_counts[(status, failure_kind, str(retryable).lower())] += 1


def memory_telemetry_snapshot() -> dict[str, list[tuple[tuple[str, ...], int]]]:
    with _lock:
        return {
            "semantic_recall": [
                (labels, count) for labels, count in sorted(_semantic_recall_counts.items())
            ],
            "retention_extraction": [
                (labels, count) for labels, count in sorted(_retention_extraction_counts.items())
            ],
            "scope_guard_violations": [
                ((reason,), count) for reason, count in sorted(_scope_guard_violation_counts.items())
            ],
            "embedding_requests": [
                (labels, count) for labels, count in sorted(_embedding_request_counts.items())
            ],
        }


def reset_memory_telemetry_for_tests() -> None:
    with _lock:
        _semantic_recall_counts.clear()
        _retention_extraction_counts.clear()
        _scope_guard_violation_counts.clear()
        _embedding_request_counts.clear()
