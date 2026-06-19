from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.embedding_profile import resolve_embedding_profile
from app.services.retrieval_replay import ReplayInputError, compare_captures, read_capture_file


class EmbeddingProfileEvalInputError(ValueError):
    pass


@dataclass(frozen=True)
class EmbeddingProfileCapture:
    name: str
    records: list[dict[str, Any]]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LiveCaptureMaterialization:
    profile_specs: list[str]
    profile_metadata_specs: list[str]
    manifest_path: Path
    manifest: dict[str, Any]


def parse_profile_spec(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise EmbeddingProfileEvalInputError("profile specs must use name=path")
    name, path = spec.split("=", 1)
    name = name.strip()
    if not name:
        raise EmbeddingProfileEvalInputError("profile name must not be empty")
    if not path.strip():
        raise EmbeddingProfileEvalInputError(f"profile {name} path must not be empty")
    return name, Path(path)


def parse_profile_metadata(values: list[str] | None) -> dict[str, dict[str, Any]]:
    metadata: dict[str, dict[str, Any]] = {}
    for value in values or []:
        name, path = parse_profile_spec(value)
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise EmbeddingProfileEvalInputError(f"cannot read profile metadata: {path}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingProfileEvalInputError(
                f"profile metadata must be JSON object for {name}: {exc.msg}"
            ) from exc
        if not isinstance(parsed, dict):
            raise EmbeddingProfileEvalInputError(f"profile metadata for {name} must be a JSON object")
        metadata[name] = parsed
    return metadata


def read_profile_captures(
    profile_specs: list[str],
    *,
    profile_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[EmbeddingProfileCapture]:
    profiles: list[EmbeddingProfileCapture] = []
    seen: set[str] = set()
    for spec in profile_specs:
        name, path = parse_profile_spec(spec)
        if name in seen:
            raise EmbeddingProfileEvalInputError(f"duplicate profile name: {name}")
        seen.add(name)
        try:
            records = read_capture_file(path)
        except ReplayInputError as exc:
            raise EmbeddingProfileEvalInputError(str(exc)) from exc
        profiles.append(
            EmbeddingProfileCapture(
                name=name,
                records=records,
                metadata=(profile_metadata or {}).get(name, {}),
            )
        )
    if len(profiles) < 2:
        raise EmbeddingProfileEvalInputError("at least two profiles are required")
    return profiles


def materialize_live_capture_pack(pack_path: Path, output_dir: Path) -> LiveCaptureMaterialization:
    """Render a compact live-capture pack into replay NDJSON files.

    The pack keeps shared query expectations in one JSON document while each
    profile owns only the captured result rows. This makes checked-in eval packs
    auditable and avoids calling providers or mutating a live corpus.
    """
    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EmbeddingProfileEvalInputError(f"cannot read live capture pack: {pack_path}") from exc
    except json.JSONDecodeError as exc:
        raise EmbeddingProfileEvalInputError(f"live capture pack must be JSON: {exc.msg}") from exc
    if not isinstance(pack, dict) or pack.get("schema_version") != 1:
        raise EmbeddingProfileEvalInputError("live capture pack schema_version must be 1")

    capture_set = _required_string(pack, "capture_set")
    corpus_id = _required_string(pack, "corpus_id")
    profiles = _required_list(pack, "profiles")
    queries = _required_list(pack, "queries")
    if not queries:
        raise EmbeddingProfileEvalInputError("live capture pack must include at least one query")

    output_dir.mkdir(parents=True, exist_ok=True)
    profile_specs: list[str] = []
    profile_metadata_specs: list[str] = []
    manifest_profiles: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_filenames: dict[str, str] = {}

    for profile in profiles:
        if not isinstance(profile, dict):
            raise EmbeddingProfileEvalInputError("live capture pack profiles must be JSON objects")
        name = _required_string(profile, "name")
        if name in seen_names:
            raise EmbeddingProfileEvalInputError(f"duplicate profile name: {name}")
        seen_names.add(name)
        filename = _safe_filename(name)
        if filename in seen_filenames:
            raise EmbeddingProfileEvalInputError(
                f"profile names {seen_filenames[filename]!r} and {name!r} map to the same output filename"
            )
        seen_filenames[filename] = name
        run_id = str(profile.get("run_id") or name)
        ablation_label = str(profile.get("ablation_label") or name)
        metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}

        records: list[dict[str, Any]] = []
        for query in queries:
            if not isinstance(query, dict):
                raise EmbeddingProfileEvalInputError("live capture pack queries must be JSON objects")
            query_profiles = query.get("profiles")
            if not isinstance(query_profiles, dict) or name not in query_profiles:
                raise EmbeddingProfileEvalInputError(
                    f"query {query.get('query_fingerprint') or '<unknown>'} missing profile {name}"
                )
            profile_capture = query_profiles[name]
            if not isinstance(profile_capture, dict):
                raise EmbeddingProfileEvalInputError(f"profile capture for {name} must be a JSON object")
            results = profile_capture.get("results")
            if not isinstance(results, list):
                raise EmbeddingProfileEvalInputError(
                    f"profile capture for {name} query {query.get('query_fingerprint')} must include results"
                )
            records.append(_record_from_live_query(query, profile_capture, capture_set, corpus_id, run_id, ablation_label))

        capture_path = output_dir / f"{filename}.ndjson"
        with capture_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

        metadata_path = output_dir / f"{filename}.metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        profile_specs.append(f"{name}={capture_path}")
        profile_metadata_specs.append(f"{name}={metadata_path}")
        manifest_profiles.append(
            {
                "name": name,
                "capture_path": str(capture_path),
                "metadata_path": str(metadata_path),
                "record_count": len(records),
            }
        )

    manifest = {
        "schema_version": 1,
        "source_pack": str(pack_path),
        "capture_set": capture_set,
        "corpus_id": corpus_id,
        "query_count": len(queries),
        "profiles": manifest_profiles,
        "pause_conditions": pack.get("pause_conditions") or [],
        "recommendation_prompt": pack.get("recommendation_prompt"),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return LiveCaptureMaterialization(
        profile_specs=profile_specs,
        profile_metadata_specs=profile_metadata_specs,
        manifest_path=manifest_path,
        manifest=manifest,
    )


def compare_embedding_profiles(
    profiles: list[EmbeddingProfileCapture],
    *,
    baseline_profile: str,
    top_k: int = 5,
    latency_delta_warn_ms: float = 500.0,
    min_recall: float | None = None,
    min_precision: float | None = None,
    min_mrr: float | None = None,
    min_ndcg: float | None = None,
    max_top1_change_rate: float | None = None,
) -> dict[str, Any]:
    baseline = next((profile for profile in profiles if profile.name == baseline_profile), None)
    if baseline is None:
        known = ", ".join(profile.name for profile in profiles)
        raise EmbeddingProfileEvalInputError(f"unknown baseline profile {baseline_profile}; known profiles: {known}")

    profile_reports: dict[str, Any] = {}
    failure_counts: Counter[str] = Counter()
    for profile in profiles:
        if profile.name == baseline_profile:
            profile_reports[profile.name] = _baseline_report(profile, top_k=top_k)
            continue

        comparison = compare_captures(
            baseline.records,
            profile.records,
            top_k=top_k,
            latency_delta_warn_ms=latency_delta_warn_ms,
            min_recall=min_recall,
            min_mrr=min_mrr,
            min_ndcg=min_ndcg,
            fail_on_forbidden=True,
        )
        profile_report = _profile_report(profile, comparison, top_k=top_k)
        if min_precision is not None and profile_report["metrics"]["precision_at_k"] is not None:
            if profile_report["metrics"]["precision_at_k"] < min_precision:
                profile_report["threshold_failures"].append("precision_below_threshold")
                failure_counts[f"{profile.name}:precision_below_threshold"] += 1
        if max_top1_change_rate is not None and profile_report["metrics"]["top1_change_rate"] > max_top1_change_rate:
            profile_report["threshold_failures"].append("top1_change_rate_above_threshold")
            failure_counts[f"{profile.name}:top1_change_rate_above_threshold"] += 1
        for key, count in comparison["summary"]["failure_counts"].items():
            failure_counts[f"{profile.name}:{key}"] += count
        profile_reports[profile.name] = profile_report

    return {
        "schema_version": 1,
        "summary": {
            "baseline_profile": baseline_profile,
            "profile_count": len(profiles),
            "profiles": [profile.name for profile in profiles],
            "top_k": top_k,
            "latency_delta_warn_ms": latency_delta_warn_ms,
            "thresholds": {
                "min_recall": min_recall,
                "min_precision": min_precision,
                "min_mrr": min_mrr,
                "min_ndcg": min_ndcg,
                "max_top1_change_rate": max_top1_change_rate,
            },
            "passed": not failure_counts,
            "failure_counts": dict(failure_counts),
        },
        "profiles": profile_reports,
    }


def build_native_image_provider_capture_report(
    *,
    profile_name: str,
    image_references: list[str],
    vectors: list[list[float]],
    latency_ms: float | None = None,
) -> dict[str, Any]:
    """Summarize a native-image provider run without persisting vectors.

    This is intentionally report-only: it validates the configured profile,
    counts returned vector dimensions, and records rollout gates, but it does
    not write to `embedding_profile_vectors` or alter retrieval defaults.
    """
    profile = resolve_embedding_profile(
        profile_name=profile_name,
        experimental_profiles_enabled=True,
    )
    if profile.profile_kind != "native_image" or profile.input_modality != "image":
        raise EmbeddingProfileEvalInputError(
            f"profile {profile.profile_name!r} is not a native image profile"
        )
    if len(vectors) != len(image_references):
        raise EmbeddingProfileEvalInputError(
            f"provider returned {len(vectors)} vectors for {len(image_references)} image references"
        )

    dimension_counts: Counter[int] = Counter(len(vector) for vector in vectors)
    mismatched_dimensions = {
        str(dimensions): count
        for dimensions, count in sorted(dimension_counts.items())
        if dimensions != profile.dimensions
    }
    return {
        "schema_version": 1,
        "report_kind": "native_image_provider_capture",
        "report_only": True,
        "storage_mutation": False,
        "default_change": False,
        "profile": {
            "profile_name": profile.profile_name,
            "provider": profile.provider,
            "model": profile.model,
            "dimensions": profile.dimensions,
            "profile_kind": profile.profile_kind,
            "input_modality": profile.input_modality,
            "enabled_by_default": profile.enabled_by_default,
            "fallback_profile_name": profile.fallback_profile_name,
        },
        "capture": {
            "input_count": len(image_references),
            "vector_count": len(vectors),
            "dimension_counts": {str(dimensions): count for dimensions, count in sorted(dimension_counts.items())},
            "mismatched_dimensions": mismatched_dimensions,
            "latency_ms": latency_ms,
        },
        "readiness": {
            "passed": not mismatched_dimensions,
            "default_enablement_blocked": True,
            "required_before_default_enablement": [
                "compare this provider capture against the SAR-611 dogfood pack",
                "accept top-rank drift explicitly",
                "keep text-description retrieval as the fallback until rollout approval",
            ],
        },
    }


def _baseline_report(profile: EmbeddingProfileCapture, *, top_k: int) -> dict[str, Any]:
    return {
        "role": "baseline",
        "metadata": profile.metadata,
        "record_count": len(profile.records),
        "query_dimensions": _query_dimensions_for_records(profile.records, top_k=top_k),
        "modality_mix": _modality_mix_for_records(profile.records, top_k=top_k),
        "provenance": _provenance_summary_for_records(profile.records, top_k=top_k),
        "metrics": {
            "top_k": top_k,
            "recall_at_k": None,
            "precision_at_k": None,
            "mrr": None,
            "ndcg_at_k": None,
            "top1_stability": 1.0,
            "top1_change_rate": 0.0,
            "overlap_at_k": 1.0,
            "average_latency_ms": _average_latency(profile.records),
            "latency_delta_ms": 0.0,
            "forbidden_hit_count": 0,
        },
        "threshold_failures": [],
    }


def _profile_report(profile: EmbeddingProfileCapture, comparison: dict[str, Any], *, top_k: int) -> dict[str, Any]:
    comparisons = [row for row in comparison["comparisons"] if row["status"] != "missing_current"]
    qualities = [row["quality"] for row in comparisons]
    top1_changes = sum(1 for row in comparisons if row["status"] == "top1_changed")
    return {
        "role": "candidate",
        "metadata": profile.metadata,
        "record_count": len(profile.records),
        "query_dimensions": _query_dimensions_for_comparisons(comparisons, top_k=top_k),
        "modality_mix": _modality_mix_for_records(profile.records, top_k=top_k),
        "provenance": _provenance_summary_for_records(profile.records, top_k=top_k),
        "metrics": {
            "top_k": top_k,
            "matched_records": len(comparisons),
            "recall_at_k": _average_optional([row.get("recall") for row in qualities]),
            "precision_at_k": _precision_at_k(qualities, top_k=top_k),
            "mrr": _average_optional([row.get("mrr") for row in qualities]),
            "ndcg_at_k": _average_optional([row.get("ndcg") for row in qualities]),
            "top1_stability": round(1.0 - (top1_changes / len(comparisons)), 4) if comparisons else None,
            "top1_change_rate": round(top1_changes / len(comparisons), 4) if comparisons else 1.0,
            "overlap_at_k": _average_optional([row.get(f"jaccard_at_{top_k}") for row in comparisons]),
            "average_latency_ms": _average_latency(profile.records),
            "average_latency_delta_ms": _average_optional([row.get("latency_delta_ms") for row in comparisons]),
            "forbidden_hit_count": sum(len(row.get("forbidden_hits") or []) for row in qualities),
        },
        "threshold_failures": [],
        "comparison_summary": comparison["summary"],
    }


def _query_dimensions_for_records(records: list[dict[str, Any]], *, top_k: int) -> dict[str, Any]:
    dimensions: dict[str, dict[str, Any]] = {}
    for record in records:
        expectations = record.get("expectations") if isinstance(record.get("expectations"), dict) else {}
        query_type = str(expectations.get("query_type") or record.get("query_type") or "unspecified")
        expected_ids = _string_list(expectations.get("expected_item_ids") or record.get("expected_item_ids"))
        forbidden_ids = _string_list(expectations.get("forbidden_item_ids") or record.get("forbidden_item_ids"))
        quality = _quality_for_record(record, expected_ids=expected_ids, forbidden_ids=forbidden_ids, top_k=top_k)
        bucket = dimensions.setdefault(
            query_type,
            {
                "record_count": 0,
                "recall_at_k_values": [],
                "mrr_values": [],
                "ndcg_at_k_values": [],
                "forbidden_hit_count": 0,
            },
        )
        bucket["record_count"] += 1
        if quality["recall"] is not None:
            bucket["recall_at_k_values"].append(quality["recall"])
        if quality["mrr"] is not None:
            bucket["mrr_values"].append(quality["mrr"])
        if quality["ndcg"] is not None:
            bucket["ndcg_at_k_values"].append(quality["ndcg"])
        bucket["forbidden_hit_count"] += len(quality["forbidden_hits"])
    return _render_query_dimensions(dimensions)


def _query_dimensions_for_comparisons(comparisons: list[dict[str, Any]], *, top_k: int) -> dict[str, Any]:
    dimensions: dict[str, dict[str, Any]] = {}
    for row in comparisons:
        expectations = row.get("expectations") if isinstance(row.get("expectations"), dict) else {}
        query_type = str(expectations.get("query_type") or "unspecified")
        quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
        bucket = dimensions.setdefault(
            query_type,
            {
                "record_count": 0,
                "top1_change_count": 0,
                "recall_at_k_values": [],
                "mrr_values": [],
                "ndcg_at_k_values": [],
                "forbidden_hit_count": 0,
            },
        )
        bucket["record_count"] += 1
        if row.get("status") == "top1_changed":
            bucket["top1_change_count"] += 1
        if quality.get("recall") is not None:
            bucket["recall_at_k_values"].append(quality["recall"])
        if quality.get("mrr") is not None:
            bucket["mrr_values"].append(quality["mrr"])
        if quality.get("ndcg") is not None:
            bucket["ndcg_at_k_values"].append(quality["ndcg"])
        bucket["forbidden_hit_count"] += len(quality.get("forbidden_hits") or [])
    rendered = _render_query_dimensions(dimensions)
    for query_type, bucket in dimensions.items():
        record_count = bucket["record_count"]
        rendered[query_type]["top1_change_rate"] = (
            round(bucket.get("top1_change_count", 0) / record_count, 4) if record_count else None
        )
    return rendered


def _render_query_dimensions(dimensions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for query_type, bucket in sorted(dimensions.items()):
        rendered[query_type] = {
            "record_count": bucket["record_count"],
            "recall_at_k": _average_optional(bucket["recall_at_k_values"]),
            "mrr": _average_optional(bucket["mrr_values"]),
            "ndcg_at_k": _average_optional(bucket["ndcg_at_k_values"]),
            "forbidden_hit_count": bucket["forbidden_hit_count"],
        }
    return rendered


def _modality_mix_for_records(records: list[dict[str, Any]], *, top_k: int) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for record in records:
        for row in record.get("results", [])[:top_k]:
            if not isinstance(row, dict):
                continue
            modality = row.get("modality") or row.get("source_type") or "unknown"
            counts[str(modality)] += 1
    return dict(sorted(counts.items()))


def _provenance_summary_for_records(records: list[dict[str, Any]], *, top_k: int) -> dict[str, int]:
    summary = {
        "result_count": 0,
        "with_caption": 0,
        "with_ocr_text": 0,
        "with_source_item_id": 0,
        "with_source_span": 0,
    }
    for record in records:
        for row in record.get("results", [])[:top_k]:
            if not isinstance(row, dict):
                continue
            summary["result_count"] += 1
            provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
            if row.get("caption") or provenance.get("caption"):
                summary["with_caption"] += 1
            if row.get("ocr_text") or provenance.get("ocr_text"):
                summary["with_ocr_text"] += 1
            if row.get("source_item_id") or provenance.get("source_item_id"):
                summary["with_source_item_id"] += 1
            if isinstance(row.get("source_span"), dict) or isinstance(provenance.get("source_span"), dict):
                summary["with_source_span"] += 1
    return summary


def _record_from_live_query(
    query: dict[str, Any],
    profile_capture: dict[str, Any],
    capture_set: str,
    corpus_id: str,
    run_id: str,
    ablation_label: str,
) -> dict[str, Any]:
    query_fingerprint = _required_string(query, "query_fingerprint")
    request = {
        "query_mode": query.get("query_mode") or "fingerprint",
        "query_fingerprint": query_fingerprint,
        "limit": query.get("limit", 5),
        "scope": query.get("scope") or {"type": "workspace", "key": "eval"},
        "tags": query.get("tags") or [],
    }
    record = {
        "schema_version": 1,
        "capture": {
            "set": capture_set,
            "corpus_id": corpus_id,
            "run_id": run_id,
            "ablation_label": ablation_label,
        },
        "endpoint": query.get("endpoint") or "/api/v1/memory/retrieve",
        "tenant_id": query.get("tenant_id") or "test",
        "status": profile_capture.get("status") or "ok",
        "latency_ms": profile_capture.get("latency_ms"),
        "request": request,
        "fallback_used": bool(profile_capture.get("fallback_used", False)),
        "expectations": query.get("expectations") or {},
        "results": profile_capture["results"],
    }
    for key in ("trace", "source_ranking_mode"):
        if key in profile_capture:
            record[key] = profile_capture[key]
    return record


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingProfileEvalInputError(f"live capture pack missing {key}")
    return value


def _required_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise EmbeddingProfileEvalInputError(f"live capture pack {key} must be a list")
    return value


def _safe_filename(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in value.strip().lower())
    return safe.strip("-") or "profile"


def _quality_for_record(
    record: dict[str, Any],
    *,
    expected_ids: list[str],
    forbidden_ids: list[str],
    top_k: int,
) -> dict[str, Any]:
    result_item_ids = [
        str(row["item_id"])
        for row in record.get("results", [])[:top_k]
        if isinstance(row, dict) and row.get("item_id") is not None
    ]
    expected = list(dict.fromkeys(expected_ids))
    expected_set = set(expected)
    forbidden_set = set(forbidden_ids)
    hits = [item_id for item_id in result_item_ids if item_id in expected_set]
    first_hit_rank = next((rank for rank, item_id in enumerate(result_item_ids, start=1) if item_id in expected_set), None)
    return {
        "expected_count": len(expected),
        "hit_count": len(set(hits)),
        "recall": round(len(set(hits)) / len(expected), 4) if expected else None,
        "mrr": round(1.0 / first_hit_rank, 4) if first_hit_rank else (0.0 if expected else None),
        "ndcg": _ndcg(result_item_ids, expected),
        "forbidden_hits": [item_id for item_id in result_item_ids if item_id in forbidden_set],
    }


def _ndcg(result_item_ids: list[str], expected_item_ids: list[str]) -> float | None:
    if not expected_item_ids:
        return None
    expected_set = set(expected_item_ids)
    dcg = sum(1.0 / math.log2(rank + 1) for rank, item_id in enumerate(result_item_ids, start=1) if item_id in expected_set)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, len(expected_item_ids) + 1))
    return round(dcg / idcg, 4) if idcg else None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _average_latency(records: list[dict[str, Any]]) -> float | None:
    return _average_optional([record.get("latency_ms") for record in records])


def _average_optional(values: list[Any]) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 4)


def _precision_at_k(qualities: list[dict[str, Any]], *, top_k: int) -> float | None:
    precisions: list[float] = []
    for quality in qualities:
        expected_count = quality.get("expected_count")
        hit_count = quality.get("hit_count")
        if not expected_count or hit_count is None:
            continue
        precisions.append(float(hit_count) / top_k)
    if not precisions:
        return None
    return round(sum(precisions) / len(precisions), 4)
