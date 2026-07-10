"""Low-cardinality runtime telemetry for relationship extraction."""

from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_extraction_counts: dict[tuple[str, str, str], int] = defaultdict(int)
_retry_counts: dict[str, int] = defaultdict(int)
_duration_sums: dict[tuple[str, str], float] = defaultdict(float)
_duration_counts: dict[tuple[str, str], int] = defaultdict(int)
_edge_counts: dict[str, int] = defaultdict(int)


def record_relationship_extraction(
    *,
    provider: str,
    retry_provider: str,
    validation_outcome: str,
    fallback_used: bool,
    retry_count: int,
    duration_seconds: float,
    edges_extracted: int,
) -> None:
    """Record only bounded labels; never include item ids, prompts, or raw errors."""

    safe_provider = provider if provider in {"openrouter", "openai"} else "unknown"
    safe_retry_provider = retry_provider if retry_provider in {"openrouter", "openai"} else "unknown"
    safe_outcome = validation_outcome if validation_outcome in {
        "valid", "empty", "malformed", "timeout", "provider_error"
    } else "provider_error"
    fallback = str(bool(fallback_used)).lower()
    labels = (safe_provider, safe_outcome)
    with _lock:
        _extraction_counts[(safe_provider, safe_outcome, fallback)] += 1
        _retry_counts[safe_retry_provider] += max(0, int(retry_count))
        _duration_sums[labels] += max(0.0, duration_seconds)
        _duration_counts[labels] += 1
        _edge_counts[safe_provider] += max(0, int(edges_extracted))


def relationship_telemetry_snapshot() -> dict[str, list[tuple[tuple[str, ...], int | float]]]:
    with _lock:
        return {
            "extractions": [(labels, count) for labels, count in sorted(_extraction_counts.items())],
            "retries": [((provider,), count) for provider, count in sorted(_retry_counts.items())],
            "duration_sums": [(labels, total) for labels, total in sorted(_duration_sums.items())],
            "duration_counts": [(labels, count) for labels, count in sorted(_duration_counts.items())],
            "edges": [((provider,), count) for provider, count in sorted(_edge_counts.items())],
        }


def reset_relationship_telemetry_for_tests() -> None:
    with _lock:
        _extraction_counts.clear()
        _retry_counts.clear()
        _duration_sums.clear()
        _duration_counts.clear()
        _edge_counts.clear()
