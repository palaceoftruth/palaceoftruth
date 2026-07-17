"""Bounded runtime telemetry for authoritative HTTP source refreshes."""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from typing import Iterable

_lock = threading.Lock()
_counts: dict[tuple[str, str, str], int] = defaultdict(int)
_histograms: dict[tuple[str, tuple[str, ...]], dict[str, object]] = {}

REFRESH_DURATION_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
CHANGE_TO_INDEX_DURATION_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_OUTCOMES = {"success", "not_modified", "failure", "gone"}
_VALIDATORS = {"etag", "last_modified", "none"}
_CHANGES = {"changed", "unchanged", "unknown"}


def _bounded(value: object, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized if normalized in allowed else default


def _observe(name: str, labels: tuple[str, ...], value: float, buckets: Iterable[float]) -> None:
    if not math.isfinite(value) or value < 0:
        return
    bucket_tuple = tuple(buckets)
    state = _histograms.setdefault(
        (name, labels), {"buckets": bucket_tuple, "counts": [0] * len(bucket_tuple), "count": 0, "sum": 0.0}
    )
    counts = state["counts"]
    assert isinstance(counts, list)
    for index, boundary in enumerate(bucket_tuple):
        if value <= boundary:
            counts[index] += 1
    state["count"] = int(state["count"]) + 1
    state["sum"] = float(state["sum"]) + value


def record_source_refresh(
    *,
    outcome: str,
    validator: str,
    change: str,
    refresh_duration_seconds: float,
    change_to_index_seconds: float | None = None,
) -> None:
    """Record a committed refresh without using resource or tenant identifiers as labels."""

    labels = (
        _bounded(outcome, _OUTCOMES, "failure"),
        _bounded(validator, _VALIDATORS, "none"),
        _bounded(change, _CHANGES, "unknown"),
    )
    with _lock:
        _counts[labels] += 1
        _observe("refresh_duration", labels, refresh_duration_seconds, REFRESH_DURATION_BUCKETS)
        if labels[2] == "changed" and change_to_index_seconds is not None:
            _observe("change_to_index_duration", (), change_to_index_seconds, CHANGE_TO_INDEX_DURATION_BUCKETS)


def source_refresh_telemetry_snapshot() -> dict[str, object]:
    with _lock:
        return {
            "counts": list(sorted(_counts.items())),
            "histograms": [
                (name, labels, {"buckets": tuple(state["buckets"]), "counts": list(state["counts"]), "count": state["count"], "sum": state["sum"]})
                for (name, labels), state in sorted(_histograms.items())
            ],
        }


def reset_source_refresh_telemetry_for_tests() -> None:
    with _lock:
        _counts.clear()
        _histograms.clear()
