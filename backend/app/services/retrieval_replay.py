from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ReplayInputError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayKey:
    endpoint: str
    query_fingerprint: str
    scope_type: str | None
    scope_key: str | None
    tags: tuple[str, ...]
    limit: int | None


def read_capture_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ReplayInputError(f"capture file does not exist: {path}")
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ReplayInputError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            validate_capture_record(record, path=path, line_number=line_number)
            records.append(record)
    if not records:
        raise ReplayInputError(f"capture file has no records: {path}")
    return records


def validate_capture_record(record: dict[str, Any], *, path: Path, line_number: int) -> None:
    if record.get("schema_version") != 1:
        raise ReplayInputError(f"{path}:{line_number}: unsupported schema_version")
    request = record.get("request")
    if not isinstance(request, dict) or not request.get("query_fingerprint"):
        raise ReplayInputError(f"{path}:{line_number}: missing request.query_fingerprint")
    if record.get("endpoint") not in {
        "/api/v1/search",
        "/api/v1/memory/retrieve",
        "/api/v1/memory/retrieve-agent",
        "/api/v1/memory/trajectory",
    }:
        raise ReplayInputError(f"{path}:{line_number}: unsupported endpoint")
    if not isinstance(record.get("results"), list):
        raise ReplayInputError(f"{path}:{line_number}: results must be a list")
    if record.get("endpoint") == "/api/v1/memory/retrieve-agent":
        trace = record.get("trace")
        if not isinstance(trace, dict):
            raise ReplayInputError(f"{path}:{line_number}: retrieve-agent capture missing trace")
        required_trace_fields = (
            "authorized_agent_scope_keys",
            "denied_agent_scope_keys",
            "result_counts_by_scope",
            "selected_scope_fallback_used",
            "selected_scope_completeness_warnings",
            "broad_corpus_searched",
            "broad_result_count",
            "fallback_used",
        )
        missing_fields = [field for field in required_trace_fields if field not in trace]
        if missing_fields:
            raise ReplayInputError(
                f"{path}:{line_number}: retrieve-agent trace missing {', '.join(missing_fields)}"
            )


def replay_key(record: dict[str, Any]) -> ReplayKey:
    request = record["request"]
    scope = request.get("scope") if isinstance(request.get("scope"), dict) else {}
    return ReplayKey(
        endpoint=record["endpoint"],
        query_fingerprint=request["query_fingerprint"],
        scope_type=scope.get("type") or record.get("requested_scope_type"),
        scope_key=scope.get("key") or record.get("requested_scope_key"),
        tags=tuple(sorted(request.get("tags") or [])),
        limit=request.get("limit"),
    )


def result_ids(record: dict[str, Any], k: int) -> list[str]:
    ids: list[str] = []
    for row in record.get("results", [])[:k]:
        item_id = row.get("item_id") if isinstance(row, dict) else None
        if item_id is not None:
            ids.append(str(item_id))
    return ids


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def expectation_metadata(record: dict[str, Any]) -> dict[str, Any]:
    expectations = record.get("expectations") if isinstance(record.get("expectations"), dict) else {}
    return {
        "expected_item_ids": _string_list(expectations.get("expected_item_ids") or record.get("expected_item_ids")),
        "forbidden_item_ids": _string_list(expectations.get("forbidden_item_ids") or record.get("forbidden_item_ids")),
        "query_type": expectations.get("query_type") or record.get("query_type"),
        "expected_scope_label": expectations.get("expected_scope_label") or record.get("expected_scope_label"),
        "expected_route": expectations.get("expected_route") or record.get("expected_route"),
        "expected_top_rank": expectations.get("expected_top_rank") or record.get("expected_top_rank"),
    }


def _quality_metrics(result_item_ids: list[str], expected_item_ids: list[str], forbidden_item_ids: list[str]) -> dict[str, Any]:
    expected = list(dict.fromkeys(expected_item_ids))
    expected_set = set(expected)
    forbidden_set = set(forbidden_item_ids)
    hits = [item_id for item_id in result_item_ids if item_id in expected_set]
    first_hit_rank = next((rank for rank, item_id in enumerate(result_item_ids, start=1) if item_id in expected_set), None)
    dcg = sum(1.0 / math.log2(rank + 1) for rank, item_id in enumerate(result_item_ids, start=1) if item_id in expected_set)
    ideal_count = len(expected)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return {
        "expected_count": len(expected),
        "hit_count": len(set(hits)),
        "recall": round(len(set(hits)) / len(expected), 4) if expected else None,
        "mrr": round(1.0 / first_hit_rank, 4) if first_hit_rank else (0.0 if expected else None),
        "ndcg": round(dcg / idcg, 4) if idcg else None,
        "forbidden_hits": [item_id for item_id in result_item_ids if item_id in forbidden_set],
    }


def _route_value(record: dict[str, Any]) -> str | None:
    trace = record.get("trace") if isinstance(record.get("trace"), dict) else {}
    for key in ("route", "selected_route", "selected_wing", "routed_room_id"):
        value = record.get(key) or trace.get(key)
        if value is not None:
            return str(value)
    return None


def _scope_labels(record: dict[str, Any], k: int) -> list[str]:
    labels: list[str] = []
    for row in record.get("results", [])[:k]:
        if not isinstance(row, dict):
            continue
        label = row.get("retrieved_scope_label") or row.get("scope_label") or row.get("source_provenance")
        if label is not None:
            labels.append(str(label))
    return labels


def capture_metadata(record: dict[str, Any]) -> dict[str, str | None]:
    capture = record.get("capture") if isinstance(record.get("capture"), dict) else {}
    trace = record.get("trace") if isinstance(record.get("trace"), dict) else {}
    search_trace = trace.get("search_ranking_trace") if isinstance(trace.get("search_ranking_trace"), dict) else {}
    source_ranking_enabled = trace.get("source_ranking_enabled", search_trace.get("source_ranking_enabled"))
    if isinstance(source_ranking_enabled, bool):
        source_ranking_mode = "on" if source_ranking_enabled else "off"
    else:
        source_ranking_mode = capture.get("source_ranking_mode") or record.get("source_ranking_mode")
    return {
        "capture_set": capture.get("set") or record.get("capture_set"),
        "corpus_id": capture.get("corpus_id") or record.get("corpus_id"),
        "run_id": capture.get("run_id") or record.get("run_id"),
        "source_ranking_mode": source_ranking_mode,
        "ablation_label": capture.get("ablation_label") or record.get("ablation_label"),
    }


def compare_captures(
    baseline_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
    *,
    top_k: int = 5,
    latency_delta_warn_ms: float = 500.0,
    min_jaccard: float = 0.0,
    min_recall: float | None = None,
    min_mrr: float | None = None,
    min_ndcg: float | None = None,
    fail_on_forbidden: bool = False,
    require_expected_scope: bool = False,
    require_expected_route: bool = False,
    required_capture_sets: set[str] | None = None,
    require_capture_metadata: bool = False,
    require_current_source_ranking_mode: str | None = None,
) -> dict[str, Any]:
    current_by_key: dict[ReplayKey, list[dict[str, Any]]] = defaultdict(list)
    for record in current_records:
        current_by_key[replay_key(record)].append(record)

    comparisons: list[dict[str, Any]] = []
    failures = Counter()
    matched_capture_sets: Counter[str] = Counter()
    capture_sets: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"corpus_ids": set(), "run_ids": set()})
    for baseline in baseline_records:
        key = replay_key(baseline)
        current = current_by_key[key].pop(0) if current_by_key.get(key) else None
        baseline_metadata = capture_metadata(baseline)
        baseline_capture_set = baseline_metadata["capture_set"] or "unknown"
        capture_sets[baseline_capture_set]["corpus_ids"].add(str(baseline_metadata["corpus_id"] or "missing"))
        capture_sets[baseline_capture_set]["run_ids"].add(str(baseline_metadata["run_id"] or "missing"))
        if current is None:
            failures["missing_current"] += 1
            comparisons.append(
                {
                    "key": key.__dict__,
                    "status": "missing_current",
                    "capture": {
                        "set": baseline_metadata["capture_set"],
                        "baseline_corpus_id": baseline_metadata["corpus_id"],
                        "baseline_run_id": baseline_metadata["run_id"],
                        "current_corpus_id": None,
                        "current_run_id": None,
                        "baseline_source_ranking_mode": baseline_metadata["source_ranking_mode"],
                        "current_source_ranking_mode": None,
                        "baseline_ablation_label": baseline_metadata["ablation_label"],
                        "current_ablation_label": None,
                    },
                }
            )
            continue

        current_metadata = capture_metadata(current)
        current_capture_set = current_metadata["capture_set"] or "unknown"
        capture_sets[current_capture_set]["corpus_ids"].add(str(current_metadata["corpus_id"] or "missing"))
        capture_sets[current_capture_set]["run_ids"].add(str(current_metadata["run_id"] or "missing"))
        if baseline_metadata["capture_set"]:
            matched_capture_sets[baseline_metadata["capture_set"]] += 1
        baseline_ids = result_ids(baseline, top_k)
        current_ids = result_ids(current, top_k)
        expectations = expectation_metadata(baseline)
        baseline_top = baseline_ids[0] if baseline_ids else None
        current_top = current_ids[0] if current_ids else None
        intersection = set(baseline_ids) & set(current_ids)
        union = set(baseline_ids) | set(current_ids)
        jaccard = len(intersection) / len(union) if union else 1.0
        latency_delta_ms = float(current.get("latency_ms") or 0) - float(baseline.get("latency_ms") or 0)
        fallback_changed = baseline.get("fallback_used") != current.get("fallback_used")
        fallback_regressed = baseline.get("fallback_used") is False and current.get("fallback_used") is True
        quality = _quality_metrics(
            current_ids,
            expectations["expected_item_ids"],
            expectations["forbidden_item_ids"],
        )
        expected_scope_match = None
        expected_scope_label = expectations["expected_scope_label"]
        if expected_scope_label:
            expected_scope_match = str(expected_scope_label) in _scope_labels(current, top_k)
        expected_route_match = None
        expected_route = expectations["expected_route"]
        if expected_route:
            expected_route_match = _route_value(current) == str(expected_route)
        status = "ok"
        if baseline_top != current_top:
            status = "top1_changed"
            failures[status] += 1
        if require_capture_metadata:
            if baseline_metadata["capture_set"] != current_metadata["capture_set"]:
                failures["capture_set_changed"] += 1
            if baseline_metadata["corpus_id"] != current_metadata["corpus_id"]:
                failures["corpus_id_changed"] += 1
            if baseline_metadata["capture_set"] is None or baseline_metadata["corpus_id"] is None:
                failures["missing_baseline_capture_metadata"] += 1
            if current_metadata["capture_set"] is None or current_metadata["corpus_id"] is None:
                failures["missing_current_capture_metadata"] += 1
        if (
            require_current_source_ranking_mode
            and current_metadata["source_ranking_mode"] != require_current_source_ranking_mode
        ):
            failures["current_source_ranking_mode_mismatch"] += 1
        if jaccard < min_jaccard:
            failures["jaccard_below_threshold"] += 1
        if fallback_changed:
            failures["fallback_changed"] += 1
        if abs(latency_delta_ms) > latency_delta_warn_ms:
            failures["latency_delta_warn"] += 1
        if min_recall is not None and quality["recall"] is not None and quality["recall"] < min_recall:
            failures["recall_below_threshold"] += 1
        if min_mrr is not None and quality["mrr"] is not None and quality["mrr"] < min_mrr:
            failures["mrr_below_threshold"] += 1
        if min_ndcg is not None and quality["ndcg"] is not None and quality["ndcg"] < min_ndcg:
            failures["ndcg_below_threshold"] += 1
        if fail_on_forbidden and quality["forbidden_hits"]:
            failures["forbidden_hit"] += len(quality["forbidden_hits"])
        if expectations["expected_top_rank"] and current_top != str(expectations["expected_top_rank"]):
            failures["expected_top_rank_mismatch"] += 1
        if require_expected_scope and expected_scope_label and expected_scope_match is False:
            failures["expected_scope_mismatch"] += 1
        if require_expected_route and expected_route and expected_route_match is False:
            failures["expected_route_mismatch"] += 1

        comparisons.append(
            {
                "key": key.__dict__,
                "status": status,
                "capture": {
                    "set": baseline_metadata["capture_set"],
                    "baseline_corpus_id": baseline_metadata["corpus_id"],
                    "baseline_run_id": baseline_metadata["run_id"],
                    "current_corpus_id": current_metadata["corpus_id"],
                    "current_run_id": current_metadata["run_id"],
                    "baseline_source_ranking_mode": baseline_metadata["source_ranking_mode"],
                    "current_source_ranking_mode": current_metadata["source_ranking_mode"],
                    "baseline_ablation_label": baseline_metadata["ablation_label"],
                    "current_ablation_label": current_metadata["ablation_label"],
                },
                "baseline_top1": baseline_top,
                "current_top1": current_top,
                f"jaccard_at_{top_k}": round(jaccard, 4),
                "latency_delta_ms": round(latency_delta_ms, 3),
                "fallback_changed": fallback_changed,
                "fallback_regressed": fallback_regressed,
                "quality": quality,
                "expectations": {
                    "query_type": expectations["query_type"],
                    "expected_item_ids": expectations["expected_item_ids"],
                    "forbidden_item_ids": expectations["forbidden_item_ids"],
                    "expected_scope_label": expected_scope_label,
                    "expected_scope_match": expected_scope_match,
                    "expected_route": expected_route,
                    "expected_route_match": expected_route_match,
                    "expected_top_rank": expectations["expected_top_rank"],
                },
                "baseline_status": baseline.get("status"),
                "current_status": current.get("status"),
            }
        )

    unmatched_current = sum(len(records) for records in current_by_key.values())
    if unmatched_current:
        failures["unmatched_current"] = unmatched_current
    missing_required_capture_sets = sorted((required_capture_sets or set()) - set(matched_capture_sets))
    if missing_required_capture_sets:
        failures["missing_required_capture_set"] = len(missing_required_capture_sets)
    return {
        "summary": {
            "baseline_records": len(baseline_records),
            "current_records": len(current_records),
            "matched_records": sum(1 for row in comparisons if row["status"] != "missing_current"),
            "top_k": top_k,
            "min_jaccard": min_jaccard,
            "min_recall": min_recall,
            "min_mrr": min_mrr,
            "min_ndcg": min_ndcg,
            "fail_on_forbidden": fail_on_forbidden,
            "require_expected_scope": require_expected_scope,
            "require_expected_route": require_expected_route,
            "latency_delta_warn_ms": latency_delta_warn_ms,
            "require_capture_metadata": require_capture_metadata,
            "require_current_source_ranking_mode": require_current_source_ranking_mode,
            "required_capture_sets": sorted(required_capture_sets or []),
            "missing_required_capture_sets": missing_required_capture_sets,
            "capture_sets": {
                name: {
                    "matched_records": matched_capture_sets.get(name, 0),
                    "corpus_ids": sorted(values["corpus_ids"]),
                    "run_ids": sorted(values["run_ids"]),
                }
                for name, values in sorted(capture_sets.items())
            },
            "failure_counts": dict(failures),
        },
        "comparisons": comparisons,
    }
