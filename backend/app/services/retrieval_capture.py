from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.config import settings


SENSITIVE_TRACE_KEYS = {"query", "chunk_text", "summary", "content", "text", "body"}
QUERY_MODES = {"fingerprint", "redacted", "raw"}
logger = logging.getLogger(__name__)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query_fingerprint(query: str) -> str:
    normalized = " ".join(query.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def redact_query(query: str, max_chars: int) -> str:
    compact = " ".join(query.strip().split())
    if len(compact) > max_chars:
        compact = f"{compact[:max_chars]}..."
    words = compact.split(" ")
    return " ".join(f"<term:{len(word)}>" if word else "" for word in words)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _safe_trace(value: Any) -> Any:
    payload = _jsonable(value)
    if not isinstance(payload, dict):
        return payload
    return _redact_sensitive_keys(payload)


def _redact_sensitive_keys(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            if str(key).lower() in SENSITIVE_TRACE_KEYS:
                redacted[str(key)] = "<redacted>"
            elif str(key).lower() == "steps" and isinstance(nested, list):
                redacted[str(key)] = [
                    {"title": str(step.get("title", ""))}
                    for step in nested
                    if isinstance(step, dict) and step.get("title")
                ]
            else:
                redacted[str(key)] = _redact_sensitive_keys(nested)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_keys(item) for item in value]
    return value


def _result_summary(result: Any, rank: int) -> dict[str, Any]:
    payload = _jsonable(result)
    if not isinstance(payload, dict):
        return {"rank": rank}
    summary = {
        "rank": rank,
        "item_id": payload.get("item_id"),
        "chunk_index": payload.get("chunk_index"),
        "score": payload.get("score"),
        "source_type": payload.get("source_type"),
        "tags": payload.get("tags") or [],
    }
    for key in ("retrieved_scope_label", "scope_label", "route", "source_provenance", "source_project"):
        if payload.get(key) is not None:
            summary[key] = payload.get(key)
    return summary


def _expectation_payload(
    *,
    expected_item_ids: list[str] | None = None,
    forbidden_item_ids: list[str] | None = None,
    query_type: str | None = None,
    expected_scope_label: str | None = None,
    expected_route: str | None = None,
    expected_top_rank: str | None = None,
) -> dict[str, Any] | None:
    payload = {
        "expected_item_ids": expected_item_ids,
        "forbidden_item_ids": forbidden_item_ids,
        "query_type": query_type,
        "expected_scope_label": expected_scope_label,
        "expected_route": expected_route,
        "expected_top_rank": expected_top_rank,
    }
    return {key: _jsonable(value) for key, value in payload.items() if value not in (None, [])} or None


def _query_payload(query: str) -> dict[str, Any]:
    mode = settings.retrieval_capture_query_mode
    if mode not in QUERY_MODES:
        mode = "fingerprint"
    payload: dict[str, Any] = {
        "query_mode": mode,
        "query_fingerprint": query_fingerprint(query),
    }
    if mode == "redacted":
        payload["query_redacted"] = redact_query(query, settings.retrieval_capture_max_query_chars)
    elif mode == "raw":
        payload["query_text"] = query[: settings.retrieval_capture_max_query_chars]
    return payload


def build_capture_record(
    *,
    endpoint: str,
    tenant_id: str,
    query: str,
    request_params: dict[str, Any],
    results: list[Any] | None,
    latency_ms: float,
    trace: Any = None,
    status: str = "ok",
    error_class: str | None = None,
    expected_item_ids: list[str] | None = None,
    forbidden_item_ids: list[str] | None = None,
    query_type: str | None = None,
    expected_scope_label: str | None = None,
    expected_route: str | None = None,
    expected_top_rank: str | None = None,
) -> dict[str, Any]:
    result_rows = [_result_summary(result, rank) for rank, result in enumerate(results or [], start=1)]
    record: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": _utc_iso(),
        "endpoint": endpoint,
        "tenant_id": tenant_id,
        "app_version": settings.app_version or None,
        "status": status,
        "error_class": error_class,
        "latency_ms": round(latency_ms, 3),
        "request": {**_query_payload(query), **_jsonable(request_params)},
        "results": result_rows,
        "result_count": len(result_rows),
    }
    expectations = _expectation_payload(
        expected_item_ids=expected_item_ids,
        forbidden_item_ids=forbidden_item_ids,
        query_type=query_type,
        expected_scope_label=expected_scope_label,
        expected_route=expected_route,
        expected_top_rank=expected_top_rank,
    )
    if expectations is not None:
        record["expectations"] = expectations
    if trace is not None:
        record["trace"] = _safe_trace(trace)
        if isinstance(record["trace"], dict):
            record["fallback_used"] = record["trace"].get("fallback_used")
            record["requested_scope_type"] = record["trace"].get("requested_scope_type")
            record["requested_scope_key"] = record["trace"].get("requested_scope_key")
    return record


def capture_retrieval(record: dict[str, Any]) -> bool:
    if not settings.retrieval_capture_enabled:
        return False
    path = Path(settings.retrieval_capture_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        return True
    except OSError:
        logger.exception("failed to write retrieval capture record to %s", path)
        return False
