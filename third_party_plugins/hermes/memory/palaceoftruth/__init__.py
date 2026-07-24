"""Palace of Truth memory plugin for Hermes.

Uses the Hermes-compatible Palace of Truth memory facade only:
  - POST /api/v1/memory/entries
  - GET /api/v1/memory/scopes
  - POST /api/v1/memory/retrieve-agent
  - POST /api/v1/memory/retrieve fallback

No admin-secret flows. No Palace operator APIs. No legacy ingest/search/jobs
endpoints.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import perf_counter, sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import UUID

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

DEFAULT_RETRIEVE_LIMIT = 5
DEFAULT_AGENT_CANDIDATE_LIMIT = 20
DEFAULT_AGENT_DISPLAY_LIMIT = 12
DEFAULT_CONTEXT_BUDGET_CHARS = 4000
DEFAULT_SEMANTIC_PREFETCH_ENABLED = False
DEFAULT_SEMANTIC_PREFETCH_TOP_K = 5
DEFAULT_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS = 1200
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_SOURCE = "hermes-agent"
DEFAULT_CREATED_BY_ROLE = "assistant"
MAX_MEMORY_BODY_CHARS = 24000
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
DEFAULT_CIRCUIT_FAILURE_THRESHOLD = 3
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = 30
DEFAULT_WRITE_QUOTAS_ENABLED = True
DEFAULT_MAX_WRITES_PER_TURN = 5
DEFAULT_MAX_WRITES_PER_SESSION = 100
DEFAULT_MAX_BULK_CALLS_PER_TURN = 2
DEFAULT_DEDUP_CACHE_TTL_SECONDS = 300
DEFAULT_OAUTH_CLIENT_KEY = "default"
DEFAULT_OAUTH_CLIENT_SCOPES = (
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
)
SEARCH_TOOL_NAME = "palace_search"
SEMANTIC_RECALL_TOOL_NAME = "palace_semantic_recall"
REMEMBER_TOOL_NAME = "palace_remember"
BULK_REMEMBER_TOOL_NAME = "palace_remember_bulk"
MEMORY_JOB_STATUS_TOOL_NAME = "palace_memory_job_status"
EXACT_SCOPE_RECALL_TOOL_NAME = "palace_exact_scope_recall"
SKILL_TAG_PREFIX = "skill-"
SCOPE_TYPES = {"session", "agent", "workspace", "tenant_shared"}
FACT_KINDS = {"world", "experience", "observation"}
RELATIONSHIP_POLICIES = {"immediate", "deferred", "skip"}
PALACE_MEMORY_ROUTE_SCOPES = {
    ("GET", "/api/v1/memory/whoami"): "read",
    ("GET", "/api/v1/memory/scopes"): "read",
    ("GET", "/api/v1/memory/scope-profile"): "read",
    ("POST", "/api/v1/memory/retrieve-agent"): "read",
    ("POST", "/api/v1/memory/retrieve"): "read",
    ("POST", "/api/v1/memory/semantic-recall"): "read",
    ("GET", "/api/v1/memory/jobs"): "read",
    ("POST", "/api/v1/memory/entries"): "write",
    ("POST", "/api/v1/memory/entries:batch"): "write",
}
PALACE_MEMORY_SCOPE_WRITE_GRANTS = {
    "agent": "write:agent",
    "workspace": "write:workspace",
    "session": "write:session",
}
_SELF_NEGATION_PHRASES = (
    "i don't have any stored knowledge",
    "i do not have any stored knowledge",
    "i don't know",
    "i do not know",
    "still no.",
    "still don't know",
    "still do not know",
    "couldn't find",
    "could not find",
    "can't find",
    "cannot find",
    "can't retrieve",
    "cannot retrieve",
    "returns nothing",
    "isn't surfacing",
    "is not surfacing",
    "no memory of",
    "doesn't appear in there either",
    "does not appear in there either",
)


def _utc_now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _trim(text: str, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _first_line(text: str, default: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _split_patterns(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_values = value
    elif isinstance(value, str):
        raw_values = value.split(",")
    else:
        return []
    patterns: list[str] = []
    for raw in raw_values:
        pattern = str(raw).strip()
        if pattern and pattern not in patterns:
            patterns.append(pattern)
    return patterns


def _split_scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    elif isinstance(value, str):
        raw_values = value.replace(",", " ").split()
    else:
        return DEFAULT_OAUTH_CLIENT_SCOPES
    scopes: list[str] = []
    for raw in raw_values:
        scope = str(raw).strip()
        if scope and scope not in scopes:
            scopes.append(scope)
    return tuple(scopes) or DEFAULT_OAUTH_CLIENT_SCOPES


def _config_bool(config: dict[str, Any], key: str, default: bool) -> bool:
    raw = config.get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _default_hermes_home() -> str:
    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    if hermes_home:
        return hermes_home
    return str(Path.home() / ".hermes")


def _merge_non_empty(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        merged[key] = value
    return merged


def _skill_name(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "id", "slug", "path"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return ""


def _normalize_skill_name(value: Any) -> str:
    raw = _skill_name(value)
    if not raw:
        return ""
    normalized_chars: list[str] = []
    previous_was_separator = False
    for char in raw.lower():
        if char.isalnum():
            normalized_chars.append(char)
            previous_was_separator = False
            continue
        if not previous_was_separator:
            normalized_chars.append("-")
            previous_was_separator = True
    return "".join(normalized_chars).strip("-")


def _normalize_active_skills(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = [value] if isinstance(value, (str, dict)) else value
    if not isinstance(raw_values, (list, tuple, set)):
        return []

    skills: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        skill = _normalize_skill_name(raw_value)
        if not skill or skill in seen:
            continue
        seen.add(skill)
        skills.append(skill)
    return skills


def _active_skill_names(value: Any) -> list[str]:
    if value is None:
        return []
    raw_values = [value] if isinstance(value, (str, dict)) else value
    if not isinstance(raw_values, (list, tuple, set)):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        name = _skill_name(raw_value)
        skill = _normalize_skill_name(raw_value)
        if not name or not skill or skill in seen:
            continue
        seen.add(skill)
        names.append(name)
    return names


def _score_value(result: dict[str, Any]) -> float | None:
    score = result.get("score")
    if isinstance(score, (int, float)):
        return float(score)
    return None


def _scope_label(scope: dict[str, str]) -> str:
    scope_type = scope["type"]
    if scope_type == "tenant_shared":
        return "tenant_shared"
    return f"{scope_type}/{scope['key']}"


def _safe_scope_labels(scopes: list[dict[str, Any]], *, limit: int = 12) -> list[str]:
    labels: list[str] = []
    for scope in scopes[:limit]:
        if not isinstance(scope, dict) or scope.get("type") not in SCOPE_TYPES:
            continue
        try:
            labels.append(_scope_label(scope))
        except KeyError:
            continue
    return labels


def _mcp_scope_for_memory_route(method: str, path: str) -> str | None:
    if method.upper() == "GET" and path.startswith("/api/v1/memory/jobs/"):
        return "read"
    scope = PALACE_MEMORY_ROUTE_SCOPES.get((method.upper(), path))
    if scope or not path.startswith("/api/v1/memory/"):
        return scope
    raise RuntimeError(
        "Palace of Truth memory route is missing an explicit MCP scope mapping: "
        f"{method.upper()} {path}"
    )


def _mcp_scopes_for_memory_route(method: str, path: str, payload: dict[str, Any] | None) -> list[str]:
    scope = _mcp_scope_for_memory_route(method, path)
    if scope is None:
        return []
    scopes = [scope]
    if method.upper() != "POST":
        return scopes
    if path == "/api/v1/memory/entries":
        scopes.extend(_scoped_write_grants_for_entry_payload(payload))
    elif path == "/api/v1/memory/entries:batch":
        if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
            raise RuntimeError("Palace memory batch write payload is missing entries")
        for entry in payload["entries"]:
            scopes.extend(_scoped_write_grants_for_entry_payload(entry))
    return _dedupe_preserving_order(scopes)


def _scoped_write_grants_for_entry_payload(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        raise RuntimeError("Palace memory write payload is missing")
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise RuntimeError("Palace memory write payload is missing scope")
    scope_type = scope.get("type")
    if scope_type == "tenant_shared":
        return []
    grant = PALACE_MEMORY_SCOPE_WRITE_GRANTS.get(str(scope_type))
    if grant is None:
        raise RuntimeError(f"Palace memory write payload has unsupported scope type: {scope_type}")
    return [grant]


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _scope_type_counts(scopes: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        scope_type = str(scope.get("type") or "unknown")
        counts[scope_type] = counts.get(scope_type, 0) + 1
    return counts


def _log_retrieval_diagnostic(level: int, event: str, **fields: Any) -> None:
    payload = {key: value for key, value in fields.items() if value not in (None, "", [], {})}
    logger.log(level, "Palace of Truth retrieval diagnostic event=%s fields=%s", event, payload)


class PalaceTransientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = retryable


class PalaceCircuitOpenError(RuntimeError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"Palace of Truth circuit is open; retry after {retry_after_seconds} seconds"
        )
        self.retry_after_seconds = retry_after_seconds
        self.retryable = True


class PalacePayloadTooLargeError(ValueError):
    pass


class PalaceRateLimitError(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.retryable = False


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = int(stripped)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        parsed = int((retry_at.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    return parsed if parsed > 0 else None


def _is_retryable_http_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _write_contract_summary(response: dict[str, Any]) -> dict[str, Any]:
    status = str(response.get("status") or "").strip() or "unknown"
    contract_status = str(response.get("contract_status") or status).strip() or status
    summary: dict[str, Any] = {
        "status": status,
        "contract_status": contract_status,
        "durable": contract_status == "completed" or status in {"complete", "duplicate"},
        "retryable": bool(response.get("retryable", False)),
    }
    for key in (
        "job_id",
        "poll_url",
        "poll_after_seconds",
        "retry_after_seconds",
        "accepted_as",
        "scope",
        "queue",
    ):
        value = response.get(key)
        if value not in (None, "", [], {}):
            summary[key] = value
    return summary


def _safe_exception_summary(exc: Exception) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "type": exc.__class__.__name__,
        "message": str(exc),
    }
    retry_after = getattr(exc, "retry_after_seconds", None)
    if isinstance(retry_after, int) and retry_after > 0:
        summary["retry_after_seconds"] = retry_after
    retryable = getattr(exc, "retryable", None)
    if isinstance(retryable, bool):
        summary["retryable"] = retryable
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        summary["status_code"] = status_code
    return summary


def _body_with_truncation_metadata(text: str) -> tuple[str, dict[str, Any]]:
    if len(text) <= MAX_MEMORY_BODY_CHARS:
        return text, {}
    return (
        _trim(text, MAX_MEMORY_BODY_CHARS),
        {
            "body_truncated": True,
            "original_body_chars": len(text),
            "stored_body_chars": MAX_MEMORY_BODY_CHARS,
        },
    )


def _reject_oversized_explicit_write(content: str) -> None:
    if len(content) <= MAX_MEMORY_BODY_CHARS:
        return
    raise PalacePayloadTooLargeError(
        "explicit Palace memory write exceeds "
        f"{MAX_MEMORY_BODY_CHARS} characters; shorten or split the memory"
    )


def _scope_key(scope: dict[str, Any]) -> str | None:
    key = scope.get("key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def _merge_retrieval_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_by_item_id: dict[str, int] = {}

    for result in results:
        item_id = result.get("item_id")
        if item_id in (None, ""):
            merged.append(result)
            continue

        item_key = str(item_id)
        existing_index = seen_by_item_id.get(item_key)
        if existing_index is None:
            seen_by_item_id[item_key] = len(merged)
            merged.append(result)
            continue

        existing = merged[existing_index]
        existing_score = _score_value(existing)
        incoming_score = _score_value(result)
        if incoming_score is not None and (
            existing_score is None or incoming_score > existing_score
        ):
            merged[existing_index] = result

    def _sort_key(entry: tuple[int, dict[str, Any]]) -> tuple[int, float, int]:
        index, result = entry
        score = _score_value(result)
        if score is None:
            return (1, 0.0, index)
        return (0, -score, index)

    return [
        result
        for _, result in sorted(enumerate(merged), key=_sort_key)
    ]


def _annotate_retrieval_results(
    scope_responses: list[tuple[dict[str, str], dict[str, Any]]]
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for scope, response in scope_responses:
        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            continue
        for result in raw_results:
            if not isinstance(result, dict):
                continue
            annotated_result = dict(result)
            annotated_result["_retrieved_scope_type"] = scope["type"]
            if "key" in scope:
                annotated_result["_retrieved_scope_key"] = scope["key"]
            annotated.append(annotated_result)
    return annotated


def _looks_like_conversation_turn(result: dict[str, Any]) -> bool:
    source_type = str(result.get("source_type") or "").strip().lower()
    chunk = str(result.get("chunk_text") or "")
    title = str(result.get("title") or "")
    return source_type == "note" and (
        "# Conversation Turn" in chunk or title.startswith("default: [")
    )


def _looks_like_negative_self_recall(result: dict[str, Any]) -> bool:
    if not _looks_like_conversation_turn(result):
        return False
    haystack = " ".join(
        [
            str(result.get("title") or ""),
            str(result.get("summary") or ""),
            str(result.get("chunk_text") or ""),
        ]
    ).lower()
    return any(phrase in haystack for phrase in _SELF_NEGATION_PHRASES)


def _scope_label_from_result(result: dict[str, Any]) -> str | None:
    returned_label = str(result.get("retrieved_scope_label") or "").strip()
    if returned_label:
        return returned_label

    scope_type = str(result.get("_retrieved_scope_type") or "").strip()
    scope_key = str(result.get("_retrieved_scope_key") or "").strip()
    if scope_type:
        if scope_type == "tenant_shared":
            return "tenant_shared"
        if not scope_key:
            return scope_type
        return f"{scope_type}/{scope_key}"

    tags = result.get("tags")
    if not isinstance(tags, list):
        return None

    normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    if "scope-tenant_shared" in normalized_tags:
        return "tenant_shared"

    tag_set = set(normalized_tags)
    for scoped_type in ("workspace", "agent", "session"):
        if f"scope-{scoped_type}" not in tag_set:
            continue
        prefix = f"{scoped_type}-"
        for tag in normalized_tags:
            if tag.startswith(prefix) and len(tag) > len(prefix):
                return f"{scoped_type}/{tag[len(prefix):]}"
        return scoped_type

    return None


def _result_item_id(result: dict[str, Any]) -> str | None:
    for key in ("item_id", "source_item_id"):
        raw = result.get(key)
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return None


def _result_visible_tags(result: dict[str, Any]) -> tuple[str, list[str]]:
    for key, label in (("matched_tags", "matched_tags"), ("tags", "tags")):
        raw_tags = result.get(key)
        if not isinstance(raw_tags, list):
            continue
        tags = list(dict.fromkeys(str(tag).strip() for tag in raw_tags if str(tag).strip()))
        if tags:
            return label, tags
    return "tags", []


def _item_api_url(base_url: str, item_id: str | None) -> str | None:
    if not base_url or not item_id:
        return None
    return f"{base_url.rstrip('/')}/api/v1/items/{item_id}"


def _format_result_evidence(result: dict[str, Any], *, base_url: str) -> str:
    parts: list[str] = []
    item_id = _result_item_id(result)
    if item_id:
        parts.append(f"item_id={item_id}")
    item_url = _item_api_url(base_url, item_id)
    if item_url:
        parts.append(f"item_url={item_url}")
    scope_label = _scope_label_from_result(result)
    if scope_label:
        parts.append(f"scope={scope_label}")
    tag_label, tags = _result_visible_tags(result)
    if tags:
        parts.append(f"{tag_label}={', '.join(tags[:12])}")
    score = result.get("score")
    if isinstance(score, (int, float)):
        parts.append(f"score={score:.2f}")
    if not parts:
        return ""
    return "  Evidence: " + "; ".join(parts)


def _scope_label_from_scope(scope: dict[str, Any] | None) -> str | None:
    if not isinstance(scope, dict):
        return None
    scope_type = str(scope.get("type") or "").strip()
    if scope_type not in SCOPE_TYPES:
        return None
    if scope_type == "tenant_shared":
        return "tenant_shared"
    scope_key = str(scope.get("key") or "").strip()
    return f"{scope_type}/{scope_key}" if scope_key else scope_type


def _format_semantic_result_evidence(result: dict[str, Any], *, base_url: str) -> str:
    parts: list[str] = []
    entry_id = _optional_str(result.get("entry_id"))
    if entry_id:
        parts.append(f"entry_id={entry_id}")
    source_item_id = _optional_str(result.get("source_item_id"))
    if source_item_id:
        parts.append(f"source_item_id={source_item_id}")
        source_url = _item_api_url(base_url, source_item_id)
        if source_url:
            parts.append(f"item_url={source_url}")
    scope_label = _scope_label_from_scope(result.get("scope"))
    if scope_label:
        parts.append(f"scope={scope_label}")
    source = _optional_str(result.get("source"))
    if source:
        parts.append(f"source={source}")
    source_url = _optional_str(result.get("source_url"))
    if source_url:
        parts.append(f"source_url={source_url}")
    fact_kind = _optional_str(result.get("fact_kind"))
    if fact_kind:
        parts.append(f"fact_kind={fact_kind}")
    temporal_status = _optional_str(result.get("temporal_status"))
    if temporal_status:
        parts.append(f"temporal_status={temporal_status}")
    for key in ("valid_from", "valid_until"):
        value = _optional_str(result.get(key))
        if value:
            parts.append(f"{key}={value}")
    tag_parts: list[str] = []
    for key in ("tags", "semantic_tags", "system_tags"):
        raw_tags = result.get(key)
        if not isinstance(raw_tags, list):
            continue
        tags = list(dict.fromkeys(str(tag).strip() for tag in raw_tags if str(tag).strip()))
        if tags:
            tag_parts.append(f"{key}={', '.join(tags[:12])}")
    parts.extend(tag_parts)
    score = result.get("score")
    if isinstance(score, (int, float)):
        parts.append(f"score={score:.2f}")
    if not parts:
        return ""
    return "  Provenance: " + "; ".join(parts)


def _scope_type_from_result(result: dict[str, Any]) -> str | None:
    scope_label = _scope_label_from_result(result)
    if not scope_label:
        return None
    if scope_label == "tenant_shared":
        return "tenant_shared"
    return scope_label.split("/", 1)[0]


def _has_shared_hits(results: list[dict[str, Any]]) -> bool:
    for result in results:
        if _scope_type_from_result(result) == "tenant_shared":
            return True
    return False


def _has_non_conversation_memory_hits(results: list[dict[str, Any]]) -> bool:
    return any(not _looks_like_conversation_turn(result) for result in results)


def _presentation_sorted_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefer_shared_hits = _has_shared_hits(results)
    prefer_memory_hits = _has_non_conversation_memory_hits(results)

    filtered: list[dict[str, Any]] = []
    for result in results:
        if (
            prefer_memory_hits
            and _looks_like_negative_self_recall(result)
        ):
            continue
        filtered.append(result)

    def _priority(index: int, result: dict[str, Any]) -> tuple[int, float, int]:
        score = _score_value(result) or 0.0
        scope_type = _scope_type_from_result(result) or ""
        conversation_turn = _looks_like_conversation_turn(result)

        if prefer_shared_hits and scope_type == "tenant_shared":
            bucket = 0
        elif prefer_memory_hits and conversation_turn:
            bucket = 3
        elif prefer_memory_hits and scope_type == "workspace":
            bucket = 0
        else:
            bucket = 1
        return (bucket, -score, index)

    return [
        result
        for index, result in sorted(enumerate(filtered), key=lambda entry: _priority(*entry))
    ]


def _load_config(hermes_home: str) -> dict[str, Any]:
    config = {
        "base_url": os.environ.get("PALACEOFTRUTH_BASE_URL", "").strip(),
        "api_key": os.environ.get("PALACEOFTRUTH_API_KEY", "").strip(),
        "oauth_client_secret": os.environ.get(
            "PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET",
            os.environ.get("SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET", ""),
        ).strip(),
        "oauth_token_url": os.environ.get(
            "PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL",
            os.environ.get("SECONDBRAIN_MCP_OAUTH_TOKEN_URL", ""),
        ).strip(),
        "oauth_resource": os.environ.get(
            "PALACEOFTRUTH_MCP_OAUTH_RESOURCE",
            os.environ.get("SECONDBRAIN_MCP_OAUTH_RESOURCE", ""),
        ).strip(),
        "oauth_audience": os.environ.get(
            "PALACEOFTRUTH_MCP_OAUTH_AUDIENCE",
            os.environ.get("SECONDBRAIN_MCP_OAUTH_AUDIENCE", ""),
        ).strip(),
        "oauth_client_key": os.environ.get(
            "PALACEOFTRUTH_MCP_CLIENT_KEY",
            os.environ.get("SECONDBRAIN_MCP_CLIENT_KEY", DEFAULT_OAUTH_CLIENT_KEY),
        ).strip()
        or DEFAULT_OAUTH_CLIENT_KEY,
        "oauth_client_scopes": os.environ.get(
            "PALACEOFTRUTH_MCP_CLIENT_SCOPES",
            os.environ.get("SECONDBRAIN_MCP_CLIENT_SCOPES", " ".join(DEFAULT_OAUTH_CLIENT_SCOPES)),
        ).strip(),
        "scope_type": os.environ.get("PALACEOFTRUTH_DEFAULT_SCOPE_TYPE", "agent").strip()
        or "agent",
        "scope_key": os.environ.get("PALACEOFTRUTH_DEFAULT_SCOPE_KEY", "").strip(),
        "retrieve_limit": _env_int("PALACEOFTRUTH_RETRIEVE_LIMIT", DEFAULT_RETRIEVE_LIMIT),
        "agent_candidate_limit": _env_int(
            "PALACEOFTRUTH_AGENT_CANDIDATE_LIMIT",
            DEFAULT_AGENT_CANDIDATE_LIMIT,
        ),
        "agent_broad_candidate_limit": _env_int(
            "PALACEOFTRUTH_AGENT_BROAD_CANDIDATE_LIMIT",
            DEFAULT_AGENT_CANDIDATE_LIMIT,
        ),
        "agent_display_limit": _env_int(
            "PALACEOFTRUTH_AGENT_DISPLAY_LIMIT",
            DEFAULT_AGENT_DISPLAY_LIMIT,
        ),
        "context_budget_chars": _env_int(
            "PALACEOFTRUTH_CONTEXT_BUDGET_CHARS",
            DEFAULT_CONTEXT_BUDGET_CHARS,
        ),
        "semantic_prefetch_enabled": _env_bool(
            "PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED",
            DEFAULT_SEMANTIC_PREFETCH_ENABLED,
        ),
        "semantic_prefetch_top_k": _env_int(
            "PALACEOFTRUTH_SEMANTIC_PREFETCH_TOP_K",
            DEFAULT_SEMANTIC_PREFETCH_TOP_K,
        ),
        "semantic_prefetch_candidate_limit": _env_int(
            "PALACEOFTRUTH_SEMANTIC_PREFETCH_CANDIDATE_LIMIT",
            DEFAULT_AGENT_CANDIDATE_LIMIT,
        ),
        "semantic_prefetch_recall_max_tokens": _env_int(
            "PALACEOFTRUTH_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS",
            DEFAULT_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS,
        ),
        "semantic_prefetch_context_budget_chars": _env_int(
            "PALACEOFTRUTH_SEMANTIC_PREFETCH_CONTEXT_BUDGET_CHARS",
            DEFAULT_CONTEXT_BUDGET_CHARS,
        ),
        "include_tenant_shared": _env_bool("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", False),
        "include_broad_corpus": _env_bool("PALACEOFTRUTH_INCLUDE_BROAD_CORPUS", False),
        "include_agent_scope_patterns": _split_patterns(
            os.environ.get("PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS", "")
        ),
        "agent_scope_pattern_limit": _env_int("PALACEOFTRUTH_AGENT_SCOPE_PATTERN_LIMIT", 5),
        "timeout_seconds": _env_int(
            "PALACEOFTRUTH_REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
        ),
        "retry_attempts": _env_int("PALACEOFTRUTH_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS),
        "retry_backoff_seconds": _env_float(
            "PALACEOFTRUTH_RETRY_BACKOFF_SECONDS",
            DEFAULT_RETRY_BACKOFF_SECONDS,
        ),
        "circuit_failure_threshold": _env_int(
            "PALACEOFTRUTH_CIRCUIT_FAILURE_THRESHOLD",
            DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
        ),
        "circuit_cooldown_seconds": _env_int(
            "PALACEOFTRUTH_CIRCUIT_COOLDOWN_SECONDS",
            DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
        ),
        "write_quotas_enabled": _env_bool(
            "PALACEOFTRUTH_WRITE_QUOTAS_ENABLED",
            DEFAULT_WRITE_QUOTAS_ENABLED,
        ),
        "max_writes_per_turn": _env_int(
            "PALACEOFTRUTH_MAX_WRITES_PER_TURN",
            DEFAULT_MAX_WRITES_PER_TURN,
        ),
        "max_writes_per_session": _env_int(
            "PALACEOFTRUTH_MAX_WRITES_PER_SESSION",
            DEFAULT_MAX_WRITES_PER_SESSION,
        ),
        "max_bulk_calls_per_turn": _env_int(
            "PALACEOFTRUTH_MAX_BULK_CALLS_PER_TURN",
            DEFAULT_MAX_BULK_CALLS_PER_TURN,
        ),
        "dedup_cache_ttl_seconds": _env_int(
            "PALACEOFTRUTH_DEDUP_CACHE_TTL_SECONDS",
            DEFAULT_DEDUP_CACHE_TTL_SECONDS,
        ),
        "source": os.environ.get("PALACEOFTRUTH_SOURCE", DEFAULT_SOURCE).strip()
        or DEFAULT_SOURCE,
        "created_by_role": os.environ.get(
            "PALACEOFTRUTH_CREATED_BY_ROLE", DEFAULT_CREATED_BY_ROLE
        ).strip()
        or DEFAULT_CREATED_BY_ROLE,
    }

    config_path = Path(hermes_home) / "palaceoftruth.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(file_cfg, dict):
                config = _merge_non_empty(config, file_cfg)
        except Exception as exc:
            logger.warning("Palace of Truth config load failed from %s: %s", config_path, exc)

    return config


class PalaceOfTruthMemoryProvider(MemoryProvider):
    """Minimal Hermes memory plugin backed by Palace of Truth."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._base_url = ""
        self._api_key = ""
        self._oauth_client_secret = ""
        self._oauth_token_url = ""
        self._oauth_resource = ""
        self._oauth_audience = ""
        self._oauth_client_key = DEFAULT_OAUTH_CLIENT_KEY
        self._oauth_client_scopes: tuple[str, ...] = DEFAULT_OAUTH_CLIENT_SCOPES
        self._bearer_token = ""
        self._bearer_expires_at: datetime | None = None
        self._bearer_lock = threading.Lock()
        self._scope_type = "agent"
        self._scope_key = ""
        self._retrieve_limit = DEFAULT_RETRIEVE_LIMIT
        self._agent_candidate_limit = DEFAULT_AGENT_CANDIDATE_LIMIT
        self._agent_broad_candidate_limit = DEFAULT_AGENT_CANDIDATE_LIMIT
        self._agent_display_limit = DEFAULT_AGENT_DISPLAY_LIMIT
        self._context_budget_chars = DEFAULT_CONTEXT_BUDGET_CHARS
        self._semantic_prefetch_enabled = DEFAULT_SEMANTIC_PREFETCH_ENABLED
        self._semantic_prefetch_top_k = DEFAULT_SEMANTIC_PREFETCH_TOP_K
        self._semantic_prefetch_candidate_limit = DEFAULT_AGENT_CANDIDATE_LIMIT
        self._semantic_prefetch_recall_max_tokens = DEFAULT_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS
        self._semantic_prefetch_context_budget_chars = DEFAULT_CONTEXT_BUDGET_CHARS
        self._include_tenant_shared = False
        self._include_broad_corpus = False
        self._include_agent_scope_patterns: list[str] = []
        self._agent_scope_pattern_limit = 5
        self._timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        self._retry_attempts = DEFAULT_RETRY_ATTEMPTS
        self._retry_backoff_seconds = DEFAULT_RETRY_BACKOFF_SECONDS
        self._circuit_failure_threshold = DEFAULT_CIRCUIT_FAILURE_THRESHOLD
        self._circuit_cooldown_seconds = DEFAULT_CIRCUIT_COOLDOWN_SECONDS
        self._circuit_failure_count = 0
        self._circuit_opened_until = 0.0
        self._circuit_lock = threading.Lock()
        self._write_quotas_enabled = DEFAULT_WRITE_QUOTAS_ENABLED
        self._max_writes_per_turn = DEFAULT_MAX_WRITES_PER_TURN
        self._max_writes_per_session = DEFAULT_MAX_WRITES_PER_SESSION
        self._max_bulk_calls_per_turn = DEFAULT_MAX_BULK_CALLS_PER_TURN
        self._dedup_cache_ttl_seconds = DEFAULT_DEDUP_CACHE_TTL_SECONDS
        self._turn_write_count = 0
        self._turn_bulk_call_count = 0
        self._session_write_count = 0
        self._write_quota_lock = threading.Lock()
        self._dedup_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._source = DEFAULT_SOURCE
        self._created_by_role = DEFAULT_CREATED_BY_ROLE
        self._session_id = ""
        self._agent_identity = ""
        self._agent_workspace = ""
        self._active_skills: list[str] = []
        self._active_skill_names: list[str] = []
        self._platform = "cli"
        self._writes_disabled = False
        self._tenant_id = ""
        self._tenant_id_lock = threading.Lock()
        self._prefetch_cache: dict[str, str] = {
            "query": "",
            "session_id": "",
            "workspace": "",
            "text": "",
        }
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: threading.Thread | None = None
        self._sync_thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return "palaceoftruth"

    def is_available(self) -> bool:
        config = _load_config(_default_hermes_home())
        base_url = str(config.get("base_url", "")).strip()
        api_key = str(config.get("api_key", "")).strip()
        oauth_client_secret = str(config.get("oauth_client_secret", "")).strip()
        return bool(base_url.startswith(("http://", "https://")) and (api_key or oauth_client_secret))

    def get_config_schema(self):
        return [
            {
                "key": "base_url",
                "description": "Palace of Truth API base URL",
                "required": True,
                "default": "https://api.palaceoftruth.example.com",
            },
            {
                "key": "api_key",
                "description": "Palace of Truth tenant API key",
                "secret": True,
                "required": False,
                "env_var": "PALACEOFTRUTH_API_KEY",
            },
            {
                "key": "oauth_client_secret",
                "description": "Palace MCP OAuth client secret; preferred over api_key when present",
                "secret": True,
                "required": False,
                "env_var": "PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET",
            },
            {
                "key": "oauth_token_url",
                "description": "Palace MCP OAuth token endpoint",
                "default": "<base_url>/api/v1/memory/mcp/oauth/token",
                "env_var": "PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL",
            },
            {
                "key": "oauth_resource",
                "description": "OAuth resource for Palace backend API calls",
                "default": "<token_endpoint_origin>/api/v1",
                "env_var": "PALACEOFTRUTH_MCP_OAUTH_RESOURCE",
            },
            {
                "key": "oauth_audience",
                "description": "OAuth audience fallback when oauth_resource is unset",
                "env_var": "PALACEOFTRUTH_MCP_OAUTH_AUDIENCE",
            },
            {
                "key": "oauth_client_key",
                "description": "Palace MCP OAuth client id",
                "default": DEFAULT_OAUTH_CLIENT_KEY,
                "env_var": "PALACEOFTRUTH_MCP_CLIENT_KEY",
            },
            {
                "key": "oauth_client_scopes",
                "description": "Space or comma separated OAuth client-credentials scopes",
                "default": " ".join(DEFAULT_OAUTH_CLIENT_SCOPES),
                "env_var": "PALACEOFTRUTH_MCP_CLIENT_SCOPES",
            },
            {
                "key": "scope_type",
                "description": "Default recall/write scope type",
                "default": "agent",
                "choices": sorted(SCOPE_TYPES),
            },
            {
                "key": "scope_key",
                "description": "Default scope key when the scope type needs one",
            },
            {
                "key": "retrieve_limit",
                "description": "Default retrieve result limit",
                "default": str(DEFAULT_RETRIEVE_LIMIT),
            },
            {
                "key": "agent_candidate_limit",
                "description": "Route-aware selected-scope candidate budget",
                "default": str(DEFAULT_AGENT_CANDIDATE_LIMIT),
            },
            {
                "key": "agent_broad_candidate_limit",
                "description": "Route-aware broad-corpus candidate budget",
                "default": str(DEFAULT_AGENT_CANDIDATE_LIMIT),
            },
            {
                "key": "agent_display_limit",
                "description": "Maximum route-aware memories rendered before context budgeting",
                "default": str(DEFAULT_AGENT_DISPLAY_LIMIT),
            },
            {
                "key": "context_budget_chars",
                "description": "Approximate maximum recalled context characters",
                "default": str(DEFAULT_CONTEXT_BUDGET_CHARS),
            },
            {
                "key": "semantic_prefetch_enabled",
                "description": "Use strict-scope semantic recall for Hermes pre-turn context",
                "default": "false",
            },
            {
                "key": "semantic_prefetch_top_k",
                "description": "Maximum semantic memories requested for pre-turn context",
                "default": str(DEFAULT_SEMANTIC_PREFETCH_TOP_K),
            },
            {
                "key": "semantic_prefetch_candidate_limit",
                "description": "Semantic recall candidate budget for pre-turn context",
                "default": str(DEFAULT_AGENT_CANDIDATE_LIMIT),
            },
            {
                "key": "semantic_prefetch_recall_max_tokens",
                "description": "Semantic recall token budget for pre-turn context",
                "default": str(DEFAULT_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS),
            },
            {
                "key": "semantic_prefetch_context_budget_chars",
                "description": "Rendered semantic pre-turn context character budget",
                "default": str(DEFAULT_CONTEXT_BUDGET_CHARS),
            },
            {
                "key": "include_tenant_shared",
                "description": "Include tenant-shared memories during recall",
                "default": "false",
            },
            {
                "key": "include_broad_corpus",
                "description": "Include broad non-private corpus recall",
                "default": "false",
            },
            {
                "key": "include_agent_scope_patterns",
                "description": "Optional comma-separated delegated agent scope patterns, for example agent/*",
                "default": "",
            },
            {
                "key": "agent_scope_pattern_limit",
                "description": "Maximum discovered agent scopes selected from pattern matches",
                "default": "5",
            },
            {
                "key": "timeout_seconds",
                "description": "HTTP timeout in seconds",
                "default": str(DEFAULT_TIMEOUT_SECONDS),
            },
            {
                "key": "retry_attempts",
                "description": "Maximum attempts for transient Palace failures",
                "default": str(DEFAULT_RETRY_ATTEMPTS),
            },
            {
                "key": "retry_backoff_seconds",
                "description": "Base retry backoff in seconds",
                "default": str(DEFAULT_RETRY_BACKOFF_SECONDS),
            },
            {
                "key": "circuit_failure_threshold",
                "description": "Transient failures before temporarily opening the circuit",
                "default": str(DEFAULT_CIRCUIT_FAILURE_THRESHOLD),
            },
            {
                "key": "circuit_cooldown_seconds",
                "description": "Circuit cooldown in seconds after repeated transient failures",
                "default": str(DEFAULT_CIRCUIT_COOLDOWN_SECONDS),
            },
            {
                "key": "write_quotas_enabled",
                "description": (
                    "Enable local write quotas and idempotency-key dedup before POSTing "
                    "to Palace. Existing deployments may set false to opt out."
                ),
                "default": "true",
            },
            {
                "key": "max_writes_per_turn",
                "description": "Maximum Palace write POST attempts per Hermes turn",
                "default": str(DEFAULT_MAX_WRITES_PER_TURN),
            },
            {
                "key": "max_writes_per_session",
                "description": "Maximum Palace write POST attempts per Hermes session",
                "default": str(DEFAULT_MAX_WRITES_PER_SESSION),
            },
            {
                "key": "max_bulk_calls_per_turn",
                "description": "Maximum palace_remember_bulk POST attempts per Hermes turn",
                "default": str(DEFAULT_MAX_BULK_CALLS_PER_TURN),
            },
            {
                "key": "dedup_cache_ttl_seconds",
                "description": "TTL for client-side idempotency-key dedup cache",
                "default": str(DEFAULT_DEDUP_CACHE_TTL_SECONDS),
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "palaceoftruth.json"
        existing: dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}
        existing.update(values)
        config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = str(kwargs.get("hermes_home", ""))
        self._config = _load_config(hermes_home)
        self._base_url = str(self._config.get("base_url", "")).strip().rstrip("/")
        self._api_key = str(self._config.get("api_key", "")).strip()
        self._oauth_client_secret = str(self._config.get("oauth_client_secret", "")).strip()
        self._oauth_token_url = str(self._config.get("oauth_token_url", "")).strip()
        if not self._oauth_token_url and self._base_url:
            self._oauth_token_url = f"{self._base_url}/api/v1/memory/mcp/oauth/token"
        self._oauth_resource = str(self._config.get("oauth_resource", "")).strip()
        self._oauth_audience = str(self._config.get("oauth_audience", "")).strip()
        self._oauth_client_key = (
            str(self._config.get("oauth_client_key", DEFAULT_OAUTH_CLIENT_KEY)).strip()
            or DEFAULT_OAUTH_CLIENT_KEY
        )
        self._oauth_client_scopes = _split_scopes(self._config.get("oauth_client_scopes"))
        self._bearer_token = ""
        self._bearer_expires_at = None
        self._scope_type = str(self._config.get("scope_type", "agent")).strip() or "agent"
        if self._scope_type not in SCOPE_TYPES:
            logger.warning(
                "Palace of Truth invalid scope_type=%s, falling back to tenant_shared",
                self._scope_type,
            )
            self._scope_type = "tenant_shared"
        self._scope_key = str(self._config.get("scope_key", "")).strip()
        self._retrieve_limit = min(
            50,
            max(1, int(self._config.get("retrieve_limit", DEFAULT_RETRIEVE_LIMIT))),
        )
        self._agent_candidate_limit = min(
            50,
            max(
                1,
                int(self._config.get("agent_candidate_limit", DEFAULT_AGENT_CANDIDATE_LIMIT)),
            ),
        )
        self._agent_broad_candidate_limit = min(
            50,
            max(
                1,
                int(
                    self._config.get(
                        "agent_broad_candidate_limit",
                        DEFAULT_AGENT_CANDIDATE_LIMIT,
                    )
                ),
            ),
        )
        self._agent_display_limit = min(
            50,
            max(
                1,
                int(self._config.get("agent_display_limit", DEFAULT_AGENT_DISPLAY_LIMIT)),
            ),
        )
        self._context_budget_chars = min(
            20000,
            max(
                200,
                int(self._config.get("context_budget_chars", DEFAULT_CONTEXT_BUDGET_CHARS)),
            ),
        )
        self._semantic_prefetch_enabled = _config_bool(
            self._config,
            "semantic_prefetch_enabled",
            default=DEFAULT_SEMANTIC_PREFETCH_ENABLED,
        )
        self._semantic_prefetch_top_k = min(
            50,
            max(
                1,
                int(self._config.get("semantic_prefetch_top_k", DEFAULT_SEMANTIC_PREFETCH_TOP_K)),
            ),
        )
        self._semantic_prefetch_candidate_limit = min(
            200,
            max(
                1,
                int(
                    self._config.get(
                        "semantic_prefetch_candidate_limit",
                        DEFAULT_AGENT_CANDIDATE_LIMIT,
                    )
                ),
            ),
        )
        self._semantic_prefetch_recall_max_tokens = min(
            20000,
            max(
                200,
                int(
                    self._config.get(
                        "semantic_prefetch_recall_max_tokens",
                        DEFAULT_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS,
                    )
                ),
            ),
        )
        self._semantic_prefetch_context_budget_chars = min(
            20000,
            max(
                200,
                int(
                    self._config.get(
                        "semantic_prefetch_context_budget_chars",
                        DEFAULT_CONTEXT_BUDGET_CHARS,
                    )
                ),
            ),
        )
        self._timeout_seconds = int(
            self._config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        )
        self._retry_attempts = min(
            5,
            max(1, int(self._config.get("retry_attempts", DEFAULT_RETRY_ATTEMPTS))),
        )
        self._retry_backoff_seconds = min(
            60.0,
            max(
                0.1,
                float(
                    self._config.get(
                        "retry_backoff_seconds",
                        DEFAULT_RETRY_BACKOFF_SECONDS,
                    )
                ),
            ),
        )
        self._circuit_failure_threshold = min(
            20,
            max(
                1,
                int(
                    self._config.get(
                        "circuit_failure_threshold",
                        DEFAULT_CIRCUIT_FAILURE_THRESHOLD,
                    )
                ),
            ),
        )
        self._circuit_cooldown_seconds = min(
            600,
            max(
                1,
                int(
                    self._config.get(
                        "circuit_cooldown_seconds",
                        DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
                    )
                ),
            ),
        )
        self._write_quotas_enabled = _config_bool(
            self._config,
            "write_quotas_enabled",
            default=DEFAULT_WRITE_QUOTAS_ENABLED,
        )
        self._max_writes_per_turn = min(
            100,
            max(
                1,
                int(self._config.get("max_writes_per_turn", DEFAULT_MAX_WRITES_PER_TURN)),
            ),
        )
        self._max_writes_per_session = min(
            10000,
            max(
                1,
                int(
                    self._config.get(
                        "max_writes_per_session",
                        DEFAULT_MAX_WRITES_PER_SESSION,
                    )
                ),
            ),
        )
        self._max_bulk_calls_per_turn = min(
            20,
            max(
                1,
                int(
                    self._config.get(
                        "max_bulk_calls_per_turn",
                        DEFAULT_MAX_BULK_CALLS_PER_TURN,
                    )
                ),
            ),
        )
        self._dedup_cache_ttl_seconds = min(
            3600,
            max(
                1,
                int(
                    self._config.get(
                        "dedup_cache_ttl_seconds",
                        DEFAULT_DEDUP_CACHE_TTL_SECONDS,
                    )
                ),
            ),
        )
        self._source = str(self._config.get("source", DEFAULT_SOURCE)).strip() or DEFAULT_SOURCE
        self._created_by_role = (
            str(self._config.get("created_by_role", DEFAULT_CREATED_BY_ROLE)).strip()
            or DEFAULT_CREATED_BY_ROLE
        )
        self._session_id = session_id
        self._agent_identity = str(kwargs.get("agent_identity", "")).strip()
        self._agent_workspace = str(kwargs.get("agent_workspace", "")).strip()
        self._include_tenant_shared = _config_bool(
            self._config,
            "include_tenant_shared",
            default=False,
        )
        self._include_broad_corpus = _config_bool(
            self._config,
            "include_broad_corpus",
            default=False,
        )
        self._include_agent_scope_patterns = _split_patterns(
            self._config.get("include_agent_scope_patterns")
        )
        self._agent_scope_pattern_limit = min(
            50,
            max(1, int(self._config.get("agent_scope_pattern_limit", 5))),
        )
        active_skills = (
            kwargs.get("active_skills")
            or kwargs.get("active_skill_names")
            or kwargs.get("skills")
        )
        self._active_skills = _normalize_active_skills(active_skills)
        self._active_skill_names = _active_skill_names(active_skills)
        self._platform = str(kwargs.get("platform", "cli")).strip() or "cli"
        agent_context = str(kwargs.get("agent_context", "")).strip()
        self._writes_disabled = agent_context not in {"", "primary"}
        self._tenant_id = ""
        self._reset_write_quota(session=True)

    def system_prompt_block(self) -> str:
        base = (
            "External memory provider active: Palace of Truth.\n"
            "Relevant long-term context may be recalled automatically before turns, "
            "and durable memory writes happen automatically after completed turns.\n"
            "Use palace_search when the user asks what you remember, asks you to "
            "look something up in memory, references prior context, or says Palace "
            "of Truth should know something. Do not answer that Palace has no "
            "memory unless you called palace_search for the user's query in this "
            "turn; if search was unavailable, say that explicitly. Use "
            "palace_remember for explicit durable memory saves."
        )
        if self._agent_workspace:
            base += (
                f"\nACTIVE PROJECT: {self._agent_workspace}\n"
                "Memories from other projects must not influence decisions or be revealed "
                "unless the user explicitly requests cross-project context. Treat any "
                "retrieved context from another project as non-authoritative background."
            )
        return base

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": SEARCH_TOOL_NAME,
                "description": (
                    "Search Palace of Truth long-term memory using the active "
                    "Hermes orchestrator scope and workspace. Tenant-shared "
                    "and broad non-private recall are used only when explicitly enabled."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The memory lookup query.",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": SEMANTIC_RECALL_TOOL_NAME,
                "description": (
                    "Strict-scope semantic recall from Palace memory entries. "
                    "Use temporal filters and fact_kind_filter when the user asks "
                    "for current, historical, or fact-kind-specific memory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The semantic memory lookup query.",
                        },
                        "scope_type": {
                            "type": "string",
                            "enum": sorted(SCOPE_TYPES),
                            "description": (
                                "Optional explicit scope. Defaults to the active "
                                "Hermes configured scope, not tenant-wide recall."
                            ),
                        },
                        "scope_key": {
                            "type": "string",
                            "description": "Scope key for agent, workspace, or session recall.",
                        },
                        "top_k": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 8,
                        },
                        "candidate_limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                        },
                        "score_threshold": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "recall_max_tokens": {
                            "type": "integer",
                            "minimum": 200,
                            "maximum": 20000,
                            "default": 1500,
                        },
                        "context_budget_chars": {
                            "type": "integer",
                            "minimum": 200,
                            "maximum": 20000,
                        },
                        "valid_at": {
                            "type": "string",
                            "description": "Optional ISO timestamp for temporal recall.",
                        },
                        "fact_kind_filter": {
                            "type": "array",
                            "items": {"type": "string", "enum": sorted(FACT_KINDS)},
                            "description": "Optional fact kinds to recall.",
                        },
                        "date_from": {"type": "string"},
                        "date_to": {"type": "string"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": REMEMBER_TOOL_NAME,
                "description": (
                    "Save a concise durable memory to Palace of Truth under the "
                    "active Hermes orchestrator scope. The result reports whether "
                    "the write is queued, durable, or degraded."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The durable memory to save.",
                        },
                        "target": {
                            "type": "string",
                            "enum": ["memory", "user"],
                            "description": (
                                "Use memory for operational/project facts and user "
                                "for stable user preferences."
                            ),
                            "default": "memory",
                        },
                        "valid_from": {
                            "type": "string",
                            "description": "Optional ISO timestamp when this memory starts being valid.",
                        },
                        "valid_until": {
                            "type": "string",
                            "description": "Optional ISO timestamp when this memory stops being valid.",
                        },
                        "supersedes_entry_id": {
                            "type": "string",
                            "description": "Optional prior semantic memory entry id this write supersedes.",
                        },
                        "fact_kind": {
                            "type": "string",
                            "enum": sorted(FACT_KINDS),
                            "description": "Optional semantic fact kind.",
                        },
                        "enable_ai_enrichment": {
                            "type": "boolean",
                            "default": False,
                            "description": "Opt in to Palace enrichment/extraction for this explicit write.",
                        },
                        "relationship_policy": {
                            "type": "string",
                            "enum": sorted(RELATIONSHIP_POLICIES),
                            "default": "immediate",
                            "description": "Relationship extraction policy for Palace ingestion.",
                        },
                    },
                    "required": ["content"],
                    "additionalProperties": False,
                },
            },
            {
                "name": BULK_REMEMBER_TOOL_NAME,
                "description": (
                    "Save up to 100 concise durable memories using Palace batch "
                    "ingestion. The ordered result reports accepted and failed "
                    "items separately."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contents": {
                            "type": "array",
                            "items": {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "object",
                                        "properties": {
                                            "content": {"type": "string"},
                                            "target": {
                                                "type": "string",
                                                "enum": ["memory", "user"],
                                            },
                                            "valid_from": {"type": "string"},
                                            "valid_until": {"type": "string"},
                                            "supersedes_entry_id": {"type": "string"},
                                            "fact_kind": {
                                                "type": "string",
                                                "enum": sorted(FACT_KINDS),
                                            },
                                            "enable_ai_enrichment": {"type": "boolean"},
                                            "relationship_policy": {
                                                "type": "string",
                                                "enum": sorted(RELATIONSHIP_POLICIES),
                                            },
                                        },
                                        "required": ["content"],
                                        "additionalProperties": False,
                                    },
                                ]
                            },
                            "minItems": 1,
                            "maxItems": 100,
                            "description": (
                                "Durable memories to save in order. Each item may be "
                                "a string or an object with temporal retention fields."
                            ),
                        },
                        "target": {
                            "type": "string",
                            "enum": ["memory", "user"],
                            "description": (
                                "Use memory for operational/project facts and user "
                                "for stable user preferences."
                            ),
                            "default": "memory",
                        },
                        "default_valid_from": {"type": "string"},
                        "default_valid_until": {"type": "string"},
                        "default_fact_kind": {
                            "type": "string",
                            "enum": sorted(FACT_KINDS),
                        },
                        "default_enable_ai_enrichment": {
                            "type": "boolean",
                            "default": False,
                        },
                        "default_relationship_policy": {
                            "type": "string",
                            "enum": sorted(RELATIONSHIP_POLICIES),
                            "default": "immediate",
                        },
                    },
                    "required": ["contents"],
                    "additionalProperties": False,
                },
            },
            {
                "name": MEMORY_JOB_STATUS_TOOL_NAME,
                "description": (
                    "Read the terminal or queued status of one previously accepted Palace "
                    "memory job. This is read-only and does not retry jobs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "The UUID job_id returned by palace_remember.",
                        }
                    },
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": EXACT_SCOPE_RECALL_TOOL_NAME,
                "description": (
                    "Search only the active configured Palace scope for canary verification. "
                    "This never broadens to workspace, tenant-shared, or sibling-agent scopes."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The exact canary identifier or memory lookup query.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if tool_name == SEARCH_TOOL_NAME:
            query = str(args.get("query") or "").strip()
            if not query:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "query is required",
                    }
                )
            try:
                text = self._retrieve_text(query, self._session_id)
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "query": query,
                        "error": _safe_exception_summary(exc),
                    }
                )
            return json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "result": text or "No Palace of Truth memory matched this query.",
                }
            )

        if tool_name == SEMANTIC_RECALL_TOOL_NAME:
            query = str(args.get("query") or "").strip()
            if not query:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "query is required",
                    }
                )
            try:
                payload = self._build_semantic_recall_payload(query, args, self._session_id)
                response = self._request_json(
                    "POST",
                    "/api/v1/memory/semantic-recall",
                    payload,
                )
                text = self._format_semantic_recall_response(response)
            except Exception as exc:
                if "HTTP 404" not in str(exc):
                    return json.dumps(
                        {
                            "ok": False,
                            "query": query,
                            "error": _safe_exception_summary(exc),
                        }
                    )
                try:
                    text = self._retrieve_text(query, self._session_id)
                except Exception as fallback_exc:
                    return json.dumps(
                        {
                            "ok": False,
                            "query": query,
                            "fallback_used": True,
                            "error": _safe_exception_summary(fallback_exc),
                        }
                    )
                return json.dumps(
                    {
                        "ok": True,
                        "query": query,
                        "fallback_used": True,
                        "result": text
                        or "No Palace of Truth memory matched this query.",
                    }
                )
            return json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "fallback_used": False,
                    "result": text
                    or "No Palace of Truth semantic memory matched this query.",
                    "trace": response.get("trace"),
                }
            )

        if tool_name == REMEMBER_TOOL_NAME:
            content = str(args.get("content") or "").strip()
            target = str(args.get("target") or "memory").strip().lower() or "memory"
            if not content:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "content is required",
                    }
                )
            if target not in {"memory", "user"}:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "target must be memory or user",
                    }
                )
            if self._writes_disabled:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "writes are disabled for this agent context",
                    }
                )
            tenant_id = self._resolve_tenant_id()
            if not tenant_id:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "could not resolve Palace of Truth tenant",
                    }
                )
            try:
                payload = self._build_memory_write_payload(
                    action="add",
                    target=target,
                    content=content,
                    tenant_id=tenant_id,
                    valid_from=_optional_str(args.get("valid_from")),
                    valid_until=_optional_str(args.get("valid_until")),
                    supersedes_entry_id=_optional_str(args.get("supersedes_entry_id")),
                    fact_kind=_optional_str(args.get("fact_kind")),
                    enable_ai_enrichment=_optional_bool(args.get("enable_ai_enrichment")),
                    relationship_policy=_optional_str(args.get("relationship_policy"))
                    or "immediate",
                )
                response = self._post_memory_entries("/api/v1/memory/entries", payload)
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "target": target,
                        "error": _safe_exception_summary(exc),
                    }
                )
            contract = _write_contract_summary(response)
            return json.dumps(
                {
                    "ok": True,
                    "target": target,
                    "scope": payload.get("scope"),
                    "durability": contract,
                    "response": response,
                }
            )

        if tool_name == BULK_REMEMBER_TOOL_NAME:
            raw_contents = args.get("contents")
            if not isinstance(raw_contents, list):
                return json.dumps(
                    {
                        "ok": False,
                        "error": "contents must be an array",
                    }
                )
            if len(raw_contents) > 100:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "contents is limited to 100 entries",
                    }
                )
            target = str(args.get("target") or "memory").strip().lower() or "memory"
            if target not in {"memory", "user"}:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "target must be memory or user",
                    }
                )
            if self._writes_disabled:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "writes are disabled for this agent context",
                    }
                )
            tenant_id = self._resolve_tenant_id()
            if not tenant_id:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "could not resolve Palace of Truth tenant",
                    }
                )
            try:
                contents = self._normalize_bulk_memory_entries(raw_contents, args, target)
                entries = [
                    self._build_memory_write_payload(
                        action="add",
                        target=entry["target"],
                        content=entry["content"],
                        tenant_id=tenant_id,
                        valid_from=entry.get("valid_from"),
                        valid_until=entry.get("valid_until"),
                        supersedes_entry_id=entry.get("supersedes_entry_id"),
                        fact_kind=entry.get("fact_kind"),
                        enable_ai_enrichment=bool(entry.get("enable_ai_enrichment")),
                        relationship_policy=str(entry.get("relationship_policy") or "immediate"),
                    )
                    for entry in contents
                ]
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "target": target,
                        "error": _safe_exception_summary(exc),
                    }
                )
            try:
                response = self._post_memory_entries(
                    "/api/v1/memory/entries:batch",
                    {"entries": entries},
                    is_bulk=True,
                )
            except Exception as exc:
                return json.dumps(
                    {
                        "ok": False,
                        "target": target,
                        "scope": entries[0].get("scope") if entries else None,
                        "error": _safe_exception_summary(exc),
                    }
                )
            return json.dumps(
                {
                    "ok": response.get("status") in {"accepted", "partial"},
                    "target": target,
                    "scope": entries[0].get("scope") if entries else None,
                    "accepted": response.get("accepted", 0),
                    "failed": response.get("failed", 0),
                    "retryable": bool(response.get("retryable", False)),
                    "retry_after_seconds": response.get("retry_after_seconds"),
                    "poll_after_seconds": response.get("poll_after_seconds"),
                    "results": response.get("results", []),
                    "response": response,
                }
            )

        if tool_name == MEMORY_JOB_STATUS_TOOL_NAME:
            raw_job_id = str(args.get("job_id") or "").strip()
            try:
                job_id = str(UUID(raw_job_id))
            except (TypeError, ValueError, AttributeError):
                return json.dumps({"ok": False, "error": "job_id must be a UUID"})
            try:
                response = self._request_json("GET", f"/api/v1/memory/jobs/{job_id}")
            except Exception as exc:
                return json.dumps(
                    {"ok": False, "job_id": job_id, "error": _safe_exception_summary(exc)}
                )
            return json.dumps(
                {
                    "ok": True,
                    "job_id": job_id,
                    "status": response.get("status"),
                    "contract_status": response.get("contract_status"),
                    "retryable": bool(response.get("retryable", False)),
                    "poll_after_seconds": response.get("poll_after_seconds"),
                    "job": response,
                }
            )

        if tool_name == EXACT_SCOPE_RECALL_TOOL_NAME:
            query = str(args.get("query") or "").strip()
            if not query:
                return json.dumps({"ok": False, "error": "query is required"})
            try:
                raw_limit = args.get("limit", self._retrieve_limit)
                limit = int(raw_limit)
                if not 1 <= limit <= 20:
                    raise ValueError("limit must be between 1 and 20")
                scope = self._build_scope(self._session_id)
                if scope is None:
                    raise RuntimeError("active Palace scope is not configured")
                response = self._request_json(
                    "POST",
                    "/api/v1/memory/retrieve",
                    {"query": _trim(query, 2000), "limit": limit, "scope": scope},
                )
                annotated = _annotate_retrieval_results([(scope, response)])
                text = self._format_exact_scope_recall(scope, annotated)
            except Exception as exc:
                return json.dumps(
                    {"ok": False, "query": query, "error": _safe_exception_summary(exc)}
                )
            return json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "scope": scope,
                    "result": text or "No Palace of Truth memory matched this query in the active scope.",
                }
            )

        return json.dumps(
            {
                "ok": False,
                "error": f"unknown Palace of Truth memory tool: {tool_name}",
            }
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        query = (query or "").strip()
        active_session = session_id or self._session_id
        active_workspace = self._agent_workspace
        if not query or not self._has_api_auth():
            return ""
        with self._prefetch_lock:
            if (
                self._prefetch_cache["query"] == query
                and self._prefetch_cache["session_id"] == active_session
                and self._prefetch_cache["workspace"] == active_workspace
            ):
                return self._prefetch_cache["text"]
        text = self._prefetch_text(query, active_session)
        with self._prefetch_lock:
            self._prefetch_cache = {
                "query": query,
                "session_id": active_session,
                "workspace": active_workspace,
                "text": text,
            }
        return text

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        query = (query or "").strip()
        active_session = session_id or self._session_id
        active_workspace = self._agent_workspace
        if not query or not self._has_api_auth():
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return

        def _worker() -> None:
            text = self._prefetch_text(query, active_session)
            with self._prefetch_lock:
                self._prefetch_cache = {
                    "query": query,
                    "session_id": active_session,
                    "workspace": active_workspace,
                    "text": text,
                }

        self._prefetch_thread = threading.Thread(target=_worker, daemon=True)
        self._prefetch_thread.start()

    def _prefetch_text(self, query: str, session_id: str) -> str:
        if not self._semantic_prefetch_enabled:
            return self._retrieve_text(query, session_id)
        try:
            return self._semantic_prefetch_text(query, session_id)
        except Exception as exc:
            if "HTTP 404" in str(exc):
                _log_retrieval_diagnostic(
                    logging.WARNING,
                    "semantic_prefetch_unavailable",
                    endpoint="/api/v1/memory/semantic-recall",
                    reason="semantic_recall_route_unavailable_fail_closed",
                )
                return ""
            raise

    def _semantic_prefetch_text(self, query: str, session_id: str) -> str:
        started_at = perf_counter()
        if (
            self._scope_type == "agent"
            and self._agent_identity
            and self._scope_key
            and self._scope_key != self._agent_identity
        ):
            raise ValueError(
                "semantic prefetch agent scope must match the active Hermes agent_identity; "
                "sibling-agent semantic recall is not exposed through pre-turn context"
            )
        payload = self._build_semantic_recall_payload(
            query,
            {
                "top_k": self._semantic_prefetch_top_k,
                "candidate_limit": self._semantic_prefetch_candidate_limit,
                "recall_max_tokens": self._semantic_prefetch_recall_max_tokens,
                "context_budget_chars": self._semantic_prefetch_context_budget_chars,
            },
            session_id,
        )
        response = self._request_json("POST", "/api/v1/memory/semantic-recall", payload)
        trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
        items = response.get("items") if isinstance(response.get("items"), list) else []
        active_scope = {"type": payload["scope_type"], "key": payload.get("scope_key")}
        _log_retrieval_diagnostic(
            logging.INFO,
            "semantic_prefetch_success" if items else "semantic_prefetch_empty",
            endpoint="/api/v1/memory/semantic-recall",
            elapsed_ms=round((perf_counter() - started_at) * 1000),
            timeout_seconds=self._timeout_seconds,
            searched_scope=_scope_label_from_scope(trace.get("searched_scope")) or _scope_label(active_scope),
            result_count=len(items),
            budget_truncated=trace.get("budget_truncated") or trace.get("context_budget_truncated"),
        )
        if not items:
            quiet_recall = self._scope_quiet_recall(active_scope)
            _log_retrieval_diagnostic(
                logging.INFO,
                "semantic_prefetch_empty_rendering",
                searched_scope=_scope_label(active_scope),
                quiet_recall=quiet_recall,
            )
            if quiet_recall:
                return ""
            return (
                "Palace of Truth semantic recall searched "
                f"{_scope_label(active_scope)} and found no matching semantic memory."
            )

        original_display_limit = self._agent_display_limit
        original_context_budget = self._context_budget_chars
        try:
            self._agent_display_limit = self._semantic_prefetch_top_k
            self._context_budget_chars = self._semantic_prefetch_context_budget_chars
            return self._format_semantic_recall_response(response)
        finally:
            self._agent_display_limit = original_display_limit
            self._context_budget_chars = original_context_budget

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        if self._writes_disabled or not self._has_api_auth():
            return
        user_text = (user_content or "").strip()
        assistant_text = (assistant_content or "").strip()
        if not user_text and not assistant_text:
            return
        active_session = session_id or self._session_id
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=1.0)

        def _worker() -> None:
            try:
                tenant_id = self._resolve_tenant_id()
                if not tenant_id:
                    return
                payload = self._build_entry_payload(
                    user_text,
                    assistant_text,
                    active_session,
                    tenant_id=tenant_id,
                )
                self._post_memory_entries("/api/v1/memory/entries", payload)
            except Exception as exc:
                logger.warning("Palace of Truth sync failed: %s", exc)
            finally:
                self._reset_write_quota()

        self._sync_thread = threading.Thread(target=_worker, daemon=True)
        self._sync_thread.start()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        del parent_session_id, reset, kwargs
        self._session_id = (new_session_id or "").strip()
        with self._prefetch_lock:
            self._prefetch_cache = {
                "query": "",
                "session_id": "",
                "workspace": "",
                "text": "",
            }
        with self._tenant_id_lock:
            self._tenant_id = ""
        self._reset_write_quota(session=True)

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if self._writes_disabled or not self._has_api_auth():
            return
        normalized_action = (action or "").strip().lower()
        normalized_target = (target or "").strip().lower()
        content_text = (content or "").strip()
        if normalized_action not in {"add", "replace"}:
            return
        if normalized_target not in {"memory", "user"}:
            return
        if not content_text:
            return
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=1.0)

        def _worker() -> None:
            try:
                tenant_id = self._resolve_tenant_id()
                if not tenant_id:
                    return
                payload = self._build_memory_write_payload(
                    action=normalized_action,
                    target=normalized_target,
                    content=content_text,
                    tenant_id=tenant_id,
                )
                self._post_memory_entries("/api/v1/memory/entries", payload)
            except Exception as exc:
                logger.warning("Palace of Truth memory mirror failed: %s", exc)

        self._sync_thread = threading.Thread(target=_worker, daemon=True)
        self._sync_thread.start()

    def _reset_write_quota(self, *, session: bool = False) -> None:
        with self._write_quota_lock:
            self._turn_write_count = 0
            self._turn_bulk_call_count = 0
            if session:
                self._session_write_count = 0

    def _write_dedup_cache_key(self, path: str, payload: dict[str, Any]) -> str:
        idempotency_key = payload.get("idempotency_key")
        if isinstance(idempotency_key, str) and idempotency_key:
            return f"{path}:{idempotency_key}"
        entries = payload.get("entries")
        if isinstance(entries, list):
            keys: list[str] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    return ""
                entry_key = entry.get("idempotency_key")
                if not isinstance(entry_key, str) or not entry_key:
                    return ""
                keys.append(entry_key)
            if keys:
                digest = hashlib.sha256(
                    json.dumps(keys, sort_keys=True).encode("utf-8")
                ).hexdigest()
                return f"{path}:batch:{digest}"
        return ""

    def _dedup_cache_get(self, cache_key: str) -> dict[str, Any] | None:
        if not self._write_quotas_enabled or not cache_key:
            return None
        now = perf_counter()
        with self._write_quota_lock:
            cached = self._dedup_cache.get(cache_key)
            if not cached:
                return None
            response, expires_at = cached
            if expires_at <= now:
                self._dedup_cache.pop(cache_key, None)
                return None
            return dict(response)

    def _dedup_cache_put(self, cache_key: str, response: dict[str, Any]) -> None:
        if not self._write_quotas_enabled or not cache_key:
            return
        expires_at = perf_counter() + self._dedup_cache_ttl_seconds
        with self._write_quota_lock:
            self._dedup_cache[cache_key] = (dict(response), expires_at)

    def _reserve_write_quota(self, *, is_bulk: bool) -> None:
        if not self._write_quotas_enabled:
            return
        with self._write_quota_lock:
            if self._turn_write_count >= self._max_writes_per_turn:
                logger.warning(
                    "Palace write cap reached: %d/%d writes this turn",
                    self._turn_write_count,
                    self._max_writes_per_turn,
                )
                raise PalaceRateLimitError(
                    "per-turn write cap exceeded "
                    f"({self._max_writes_per_turn}); raise max_writes_per_turn "
                    "in palaceoftruth.json to allow more"
                )
            if self._session_write_count >= self._max_writes_per_session:
                logger.warning(
                    "Palace write cap reached: %d/%d writes this session",
                    self._session_write_count,
                    self._max_writes_per_session,
                )
                raise PalaceRateLimitError(
                    "per-session write cap exceeded "
                    f"({self._max_writes_per_session}); raise max_writes_per_session "
                    "in palaceoftruth.json to allow more"
                )
            if is_bulk and self._turn_bulk_call_count >= self._max_bulk_calls_per_turn:
                logger.warning(
                    "Palace bulk write cap reached: %d/%d bulk calls this turn",
                    self._turn_bulk_call_count,
                    self._max_bulk_calls_per_turn,
                )
                raise PalaceRateLimitError(
                    "per-turn bulk-call cap exceeded "
                    f"({self._max_bulk_calls_per_turn}); raise max_bulk_calls_per_turn "
                    "in palaceoftruth.json to allow more"
                )
            self._turn_write_count += 1
            self._session_write_count += 1
            if is_bulk:
                self._turn_bulk_call_count += 1

    def _post_memory_entries(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        is_bulk: bool = False,
    ) -> dict[str, Any]:
        cache_key = self._write_dedup_cache_key(path, payload)
        cached = self._dedup_cache_get(cache_key)
        if cached is not None:
            return cached
        self._reserve_write_quota(is_bulk=is_bulk)
        response = self._request_json("POST", path, payload)
        self._dedup_cache_put(cache_key, response)
        return response

    def shutdown(self) -> None:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.0)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_circuit_open()
        body = None
        mcp_scopes = _mcp_scopes_for_memory_route(method, path, payload)
        headers = self._auth_headers(mcp_scopes)
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request_path = path
        if params:
            query = urlencode(
                [
                    (key, item)
                    for key, value in params.items()
                    if value is not None
                    for item in (value if isinstance(value, list) else [value])
                ]
            )
            if query:
                request_path = f"{path}?{query}"
        request = Request(
            f"{self._base_url}{request_path}",
            data=body,
            method=method,
            headers=headers,
        )

        last_error: Exception | None = None
        attempts = max(1, self._retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    raw = response.read().decode("utf-8")
                self._record_request_success()
                break
            except HTTPError as exc:
                retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
                if not _is_retryable_http_status(exc.code):
                    exc.read()
                    raise RuntimeError(
                        f"Palace of Truth {method} {path} failed with HTTP {exc.code}"
                    ) from exc
                last_error = PalaceTransientError(
                    f"Palace of Truth {method} {path} retryable HTTP {exc.code}",
                    status_code=exc.code,
                    retry_after_seconds=retry_after,
                )
            except URLError as exc:
                last_error = PalaceTransientError(
                    f"Palace of Truth {method} {path} transient network failure",
                )

            if attempt >= attempts:
                assert last_error is not None
                self._record_request_failure(last_error)
                raise last_error
            delay = self._retry_delay_seconds(attempt, last_error)
            logger.warning(
                "Palace of Truth request retrying method=%s path=%s attempt=%s "
                "retry_after_seconds=%s error_class=%s",
                method,
                path,
                attempt,
                getattr(last_error, "retry_after_seconds", None),
                last_error.__class__.__name__ if last_error else None,
            )
            sleep(delay)
        else:
            raw = ""

        if not raw.strip():
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Palace of Truth {method} {path} returned non-object JSON")
        return parsed

    def _has_api_auth(self) -> bool:
        return bool(self._base_url and (self._oauth_client_secret or self._api_key))

    def _auth_headers(self, mcp_scopes: list[str]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._oauth_client_secret:
            headers["Authorization"] = f"Bearer {self._active_bearer_token()}"
            return headers
        if self._api_key:
            headers["X-API-Key"] = self._api_key
            if mcp_scopes:
                headers["X-MCP-Scope"] = mcp_scopes[0]
                headers["X-MCP-Scopes"] = ",".join(mcp_scopes)
            return headers
        raise RuntimeError("Palace of Truth API key or OAuth client secret is required")

    def _active_bearer_token(self) -> str:
        with self._bearer_lock:
            if self._bearer_token and self._bearer_expires_at:
                if self._bearer_expires_at > datetime.now(tz=UTC) + timedelta(seconds=30):
                    return self._bearer_token
            return self._mint_oauth_token()

    def _oauth_resource_for_backend_api(self) -> str:
        configured_resource = self._oauth_resource or self._oauth_audience
        if configured_resource:
            parsed = urlsplit(configured_resource)
            if parsed.path.rstrip("/") != "/mcp":
                return configured_resource
            logger.warning(
                "Ignoring legacy MCP OAuth resource %s for Hermes backend API calls",
                configured_resource,
            )
        token_url = self._oauth_token_url or f"{self._base_url}/api/v1/memory/mcp/oauth/token"
        parsed_token_url = urlsplit(token_url)
        return urlunsplit(("https", parsed_token_url.netloc, "/api/v1", "", ""))

    def _mint_oauth_token(self) -> str:
        if not self._oauth_client_secret or not self._oauth_token_url:
            raise RuntimeError("Palace OAuth client secret and token URL are required")
        form_body = urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._oauth_client_key,
                "client_secret": self._oauth_client_secret,
                "scope": " ".join(self._oauth_client_scopes),
                "resource": self._oauth_resource_for_backend_api(),
            }
        ).encode("utf-8")
        request = Request(
            self._oauth_token_url,
            data=form_body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            exc.read()
            raise RuntimeError(
                f"Palace OAuth token endpoint failed with HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise RuntimeError("Palace OAuth token endpoint network failure") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Palace OAuth token endpoint returned non-JSON response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Palace OAuth token endpoint returned non-object JSON")
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError("Palace OAuth token endpoint did not return access_token")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise RuntimeError("Palace OAuth token endpoint did not return a valid expires_in")
        self._bearer_token = access_token.strip()
        self._bearer_expires_at = datetime.now(tz=UTC) + timedelta(seconds=expires_in)
        return self._bearer_token

    def _raise_if_circuit_open(self) -> None:
        with self._circuit_lock:
            if not self._circuit_opened_until:
                return
            now = perf_counter()
            if now >= self._circuit_opened_until:
                self._circuit_opened_until = 0.0
                self._circuit_failure_count = 0
                return
            retry_after = max(1, int(self._circuit_opened_until - now))
            raise PalaceCircuitOpenError(retry_after)

    def _record_request_success(self) -> None:
        with self._circuit_lock:
            self._circuit_failure_count = 0
            self._circuit_opened_until = 0.0

    def _record_request_failure(self, exc: Exception) -> None:
        with self._circuit_lock:
            self._circuit_failure_count += 1
            if self._circuit_failure_count < self._circuit_failure_threshold:
                return
            retry_after = getattr(exc, "retry_after_seconds", None)
            cooldown = (
                retry_after
                if isinstance(retry_after, int) and retry_after > 0
                else self._circuit_cooldown_seconds
            )
            self._circuit_opened_until = perf_counter() + cooldown
            logger.warning(
                "Palace of Truth circuit opened after transient failures "
                "failure_count=%s retry_after_seconds=%s",
                self._circuit_failure_count,
                cooldown,
            )

    def _retry_delay_seconds(self, attempt: int, exc: Exception | None) -> float:
        retry_after = getattr(exc, "retry_after_seconds", None)
        if isinstance(retry_after, int) and retry_after > 0:
            return float(min(retry_after, self._circuit_cooldown_seconds))
        return min(
            self._retry_backoff_seconds * (2 ** max(0, attempt - 1)),
            float(self._circuit_cooldown_seconds),
        )

    def _resolve_tenant_id(self) -> str | None:
        with self._tenant_id_lock:
            if self._tenant_id:
                return self._tenant_id
            try:
                response = self._request_json("GET", "/api/v1/memory/whoami")
            except Exception as exc:
                logger.warning(
                    "Palace of Truth tenant resolution failed; skipping write: %s",
                    exc,
                )
                return None

            tenant_id = str(response.get("tenant_id", "")).strip()
            if not tenant_id:
                logger.warning(
                    "Palace of Truth tenant resolution failed; skipping write: empty tenant_id"
                )
                return None

            self._tenant_id = tenant_id
            return tenant_id

    def _build_scope(self, session_id: str) -> dict[str, str] | None:
        scope_type = self._scope_type
        if scope_type == "tenant_shared":
            return {"type": "tenant_shared"}

        scope_key = self._scope_key
        if not scope_key:
            if scope_type == "agent":
                scope_key = self._agent_identity or "hermes"
            elif scope_type == "workspace":
                scope_key = self._agent_workspace or self._agent_identity or "workspace"
            elif scope_type == "session":
                scope_key = session_id or self._session_id or "session"

        if not scope_key:
            return None
        return {"type": scope_type, "key": scope_key}

    def _build_retrieve_scopes(
        self,
        session_id: str,
        discovered_scopes: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, str]]:
        primary_scope = self._build_scope(session_id)
        if primary_scope is None:
            return []

        scopes: list[dict[str, str]] = []
        seen_labels: set[str] = set()

        def _append_scope(scope: dict[str, str]) -> None:
            try:
                label = _scope_label(scope)
            except KeyError:
                return
            if label in seen_labels:
                return
            seen_labels.add(label)
            scopes.append(scope)

        if self._is_bound_hermes_oauth_client():
            # Palace binds hermes-* OAuth clients to a canonical agent scope.
            # Even if a deployment carries a stale workspace retrieval default,
            # fallback must use the runtime's bound agent identity.
            agent_scope_key = self._agent_identity
            if not agent_scope_key and primary_scope["type"] == "agent":
                agent_scope_key = _scope_key(primary_scope)
            if agent_scope_key:
                _append_scope({"type": "agent", "key": agent_scope_key})
            return scopes

        _append_scope(primary_scope)
        for workspace_key in self._workspace_scope_keys_for_agent_retrieve(
            primary_scope,
            discovered_scopes or [],
        ):
            _append_scope({"type": "workspace", "key": workspace_key})
        if self._include_tenant_shared and primary_scope["type"] != "tenant_shared":
            _append_scope({"type": "tenant_shared"})
        return scopes

    def _is_bound_hermes_oauth_client(self) -> bool:
        return bool(
            self._oauth_client_secret
            and self._oauth_client_key.startswith("hermes-")
        )

    def _discover_memory_scopes(self) -> list[dict[str, Any]]:
        response = self._request_json(
            "GET",
            "/api/v1/memory/scopes",
            params={"limit": 100, "sample_limit": 5},
        )
        scopes = response.get("scopes")
        if not isinstance(scopes, list):
            return []

        discovered: list[dict[str, Any]] = []
        for entry in scopes:
            if not isinstance(entry, dict):
                continue
            scope = entry.get("scope")
            if not isinstance(scope, dict):
                continue
            scope_type = str(scope.get("type") or "").strip()
            if scope_type not in SCOPE_TYPES:
                continue
            if scope_type == "tenant_shared":
                discovered_scope: dict[str, Any] = {"type": "tenant_shared"}
                if isinstance(entry.get("profile"), dict):
                    discovered_scope["profile"] = entry["profile"]
                discovered.append(discovered_scope)
                continue
            key = _scope_key(scope)
            if key:
                discovered_scope = {"type": scope_type, "key": key}
                if isinstance(entry.get("profile"), dict):
                    discovered_scope["profile"] = entry["profile"]
                discovered.append(discovered_scope)
        return discovered

    def _scope_quiet_recall(self, scope: dict[str, Any]) -> bool:
        try:
            response = self._request_json(
                "GET",
                "/api/v1/memory/scope-profile",
                params={
                    "scope_type": scope.get("type"),
                    "scope_key": scope.get("key"),
                },
            )
        except Exception as exc:
            _log_retrieval_diagnostic(
                logging.WARNING,
                "semantic_prefetch_scope_profile_failed",
                error_class=exc.__class__.__name__,
            )
            return False
        return _optional_bool(response.get("quiet_recall"), default=False)

    def _workspace_scope_keys_for_agent_retrieve(
        self,
        primary_scope: dict[str, str] | None,
        discovered_scopes: list[dict[str, Any]],
    ) -> list[str]:
        keys: list[str] = []
        if primary_scope and primary_scope["type"] == "workspace":
            key = _scope_key(primary_scope)
            if key:
                keys.append(key)
        if self._agent_workspace and self._agent_workspace not in keys:
            keys.append(self._agent_workspace)
        if self._agent_workspace:
            return keys
        for scope in discovered_scopes:
            if scope.get("type") != "workspace":
                continue
            key = _scope_key(scope)
            if key and key not in keys:
                keys.append(key)
        return keys

    def _format_agent_retrieve_response(
        self,
        response: dict[str, Any],
        *,
        discovered_scopes: list[dict[str, Any]],
    ) -> str:
        raw_results = response.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            return ""

        lines = ["Recalled context from Palace of Truth:"]
        trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
        searched_scopes = trace.get("searched_scopes") if isinstance(trace, dict) else []
        searched_labels: list[str] = []
        if isinstance(searched_scopes, list):
            for scope in searched_scopes:
                if not isinstance(scope, dict) or scope.get("type") not in SCOPE_TYPES:
                    continue
                try:
                    searched_labels.append(_scope_label(scope))
                except KeyError:
                    continue
        if searched_labels:
            lines.append("- Retrieval searched scopes: " + ", ".join(searched_labels) + ".")

        discovered_labels: list[str] = []
        for scope in discovered_scopes[:8]:
            try:
                discovered_labels.append(_scope_label(scope))
            except KeyError:
                continue
        if discovered_labels:
            lines.append("- Available memory scopes include: " + ", ".join(discovered_labels) + ".")
        if trace.get("broad_corpus_searched"):
            lines.append("- Retrieval also searched the broad non-private Palace corpus.")
        if trace.get("fallback_used"):
            lines.append("- Retrieval fell back to a broader Palace route.")
        budget_parts = []
        for label, key in (
            ("selected candidates", "selected_scope_candidate_limit"),
            ("broad candidates", "broad_candidate_limit"),
            ("display", "display_limit"),
        ):
            value = trace.get(key)
            if isinstance(value, int):
                budget_parts.append(f"{label}: {value}")
        if isinstance(trace.get("context_budget_chars"), int):
            budget_parts.append(f"context chars: {trace['context_budget_chars']}")
        if budget_parts:
            lines.append("- Retrieval budgets: " + ", ".join(budget_parts) + ".")
        if trace.get("budget_truncated") or trace.get("context_budget_truncated"):
            lines.append("- Retrieval returned the highest-ranked memories within the configured budget.")
        warnings = trace.get("completeness_warnings")
        if isinstance(warnings, list):
            for warning in warnings:
                warning_text = str(warning or "").strip()
                if warning_text:
                    lines.append(f"- Completeness warning: {warning_text}")

        used_chars = sum(len(line) for line in lines)
        sorted_results = _presentation_sorted_results(
            [result for result in raw_results if isinstance(result, dict)]
        )
        for result in sorted_results[: self._agent_display_limit]:
            if not isinstance(result, dict):
                continue
            title = _trim(str(result.get("title") or "Untitled memory"), 120)
            chunk = _trim(
                str(
                    result.get("chunk_text")
                    or result.get("summary")
                    or result.get("body")
                    or ""
                ).replace("\n", " "),
                500,
            )
            score = result.get("score")
            source_type = str(result.get("source_type") or "").strip()
            scope_label = _scope_label_from_result(result)
            qualifiers = [part for part in (source_type, scope_label) if part]
            title_with_context = title
            if qualifiers:
                title_with_context = f"{title} [{', '.join(qualifiers)}]"
            if isinstance(score, (int, float)):
                result_line = f"- [{score:.2f}] {title_with_context}"
            else:
                result_line = f"- {title_with_context}"
            evidence_line = _format_result_evidence(result, base_url=self._base_url)
            chunk_line = f"  {chunk}" if chunk else ""
            added_chars = len(result_line) + len(evidence_line) + len(chunk_line)
            if used_chars + added_chars > self._context_budget_chars and len(lines) > 1:
                lines.append("- Additional memories were omitted to stay within the context budget.")
                break
            lines.append(result_line)
            if evidence_line:
                lines.append(evidence_line)
            if chunk:
                lines.append(f"  Snippet: {chunk}")
            used_chars += added_chars
        return "\n".join(lines)

    def _format_exact_scope_recall(
        self,
        scope: dict[str, str],
        results: list[dict[str, Any]],
    ) -> str:
        if not results:
            return ""
        lines = [f"Exact-scope recall from Palace of Truth: {_scope_label(scope)}."]
        for result in _presentation_sorted_results(results):
            title = _trim(str(result.get("title") or "Untitled memory"), 120)
            score = result.get("score")
            lines.append(f"- [{score:.2f}] {title}" if isinstance(score, (int, float)) else f"- {title}")
            evidence_line = _format_result_evidence(result, base_url=self._base_url)
            if evidence_line:
                lines.append(evidence_line)
            snippet = _trim(
                str(result.get("chunk_text") or result.get("summary") or result.get("body") or "").replace("\n", " "),
                500,
            )
            if snippet:
                lines.append(f"  Snippet: {snippet}")
        return "\n".join(lines)

    def _build_semantic_recall_payload(
        self,
        query: str,
        args: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        explicit_scope_type = _optional_str(args.get("scope_type"))
        explicit_scope_key = _optional_str(args.get("scope_key"))
        active_scope = self._build_scope(session_id) or {
            "type": "agent",
            "key": self._agent_identity or "hermes",
        }
        if explicit_scope_type and explicit_scope_type not in SCOPE_TYPES:
            raise ValueError(f"scope_type must be one of {', '.join(sorted(SCOPE_TYPES))}")
        if explicit_scope_type:
            scope: dict[str, str] = {"type": explicit_scope_type}
            if explicit_scope_type != "tenant_shared":
                if not explicit_scope_key:
                    raise ValueError("scope_key is required for non-tenant_shared semantic recall")
                scope["key"] = explicit_scope_key
            self._validate_semantic_recall_scope(scope, active_scope)
        else:
            scope = active_scope

        def _bounded_int(key: str, default: int | None, minimum: int, maximum: int) -> int | None:
            raw = args.get(key)
            if raw is None:
                return default
            value = int(raw)
            if value < minimum or value > maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}")
            return value

        fact_kind_filter = args.get("fact_kind_filter")
        if fact_kind_filter is None:
            normalized_fact_kinds = None
        elif isinstance(fact_kind_filter, list):
            normalized_fact_kinds = []
            for raw_kind in fact_kind_filter:
                fact_kind = _optional_str(raw_kind)
                if not fact_kind:
                    continue
                if fact_kind not in FACT_KINDS:
                    raise ValueError("fact_kind_filter contains an unsupported fact kind")
                if fact_kind not in normalized_fact_kinds:
                    normalized_fact_kinds.append(fact_kind)
        else:
            raise ValueError("fact_kind_filter must be an array")

        score_threshold = args.get("score_threshold")
        if score_threshold is not None:
            score_threshold = float(score_threshold)
            if score_threshold < 0 or score_threshold > 1:
                raise ValueError("score_threshold must be between 0 and 1")

        payload: dict[str, Any] = {
            "query": _trim(query, 2000),
            "scope_type": scope["type"],
            "scope_key": scope.get("key"),
            "top_k": _bounded_int("top_k", 8, 1, 50),
            "candidate_limit": _bounded_int("candidate_limit", None, 1, 200),
            "score_threshold": score_threshold,
            "recall_max_tokens": _bounded_int("recall_max_tokens", 1500, 200, 20000),
            "context_budget_chars": _bounded_int("context_budget_chars", None, 200, 20000),
            "valid_at": _optional_str(args.get("valid_at")),
            "fact_kind_filter": normalized_fact_kinds,
            "date_from": _optional_str(args.get("date_from")),
            "date_to": _optional_str(args.get("date_to")),
        }
        return {key: value for key, value in payload.items() if value is not None}

    def _validate_semantic_recall_scope(
        self,
        requested_scope: dict[str, str],
        active_scope: dict[str, str],
    ) -> None:
        requested_type = requested_scope.get("type")
        requested_key = requested_scope.get("key")
        active_type = active_scope.get("type")
        active_key = active_scope.get("key")

        if requested_type == active_type and requested_key == active_key:
            return
        if requested_type == "tenant_shared" and self._include_tenant_shared:
            return
        if requested_type == "workspace":
            allowed_workspace_keys = {
                key
                for key in (self._agent_workspace, active_key if active_type == "workspace" else None)
                if key
            }
            if requested_key in allowed_workspace_keys:
                return
        raise ValueError(
            "semantic recall scope must match the active Hermes scope; tenant_shared "
            "requires PALACEOFTRUTH_INCLUDE_TENANT_SHARED=true, and sibling-agent "
            "semantic recall is not exposed through this tool"
        )

    def _format_semantic_recall_response(self, response: dict[str, Any]) -> str:
        raw_items = response.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return ""

        lines = ["Recalled semantic memory from Palace of Truth:"]
        trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
        searched_scope = _scope_label_from_scope(trace.get("searched_scope"))
        if searched_scope:
            lines.append(f"- Semantic recall searched scope: {searched_scope}.")
        budget_parts = []
        for label, key in (
            ("candidates", "candidate_limit"),
            ("display", "display_limit"),
        ):
            value = trace.get(key)
            if isinstance(value, int):
                budget_parts.append(f"{label}: {value}")
        if trace.get("recall_max_tokens"):
            budget_parts.append(f"recall tokens: {trace['recall_max_tokens']}")
        if budget_parts:
            lines.append("- Semantic recall budgets: " + ", ".join(budget_parts) + ".")
        if trace.get("valid_at"):
            lines.append(f"- Temporal reference: valid_at={trace['valid_at']}.")
        if trace.get("fact_kind_filter"):
            lines.append("- Fact kind filter: " + ", ".join(trace["fact_kind_filter"]) + ".")
        if trace.get("budget_truncated"):
            lines.append("- Semantic recall returned the highest-ranked memories within budget.")

        used_chars = sum(len(line) for line in lines)
        for result in raw_items[: self._agent_display_limit]:
            if not isinstance(result, dict):
                continue
            title = _trim(str(result.get("title") or "Untitled semantic memory"), 120)
            chunk = _trim(
                str(result.get("body") or result.get("summary") or "").replace("\n", " "),
                500,
            )
            score = result.get("score")
            scope_label = _scope_label_from_scope(result.get("scope"))
            fact_kind = _optional_str(result.get("fact_kind"))
            temporal_status = _optional_str(result.get("temporal_status"))
            qualifiers = [part for part in (scope_label, fact_kind, temporal_status) if part]
            title_with_context = f"{title} [{', '.join(qualifiers)}]" if qualifiers else title
            result_line = (
                f"- [{score:.2f}] {title_with_context}"
                if isinstance(score, (int, float))
                else f"- {title_with_context}"
            )
            evidence_line = _format_semantic_result_evidence(result, base_url=self._base_url)
            chunk_line = f"  Snippet: {chunk}" if chunk else ""
            added_chars = len(result_line) + len(evidence_line) + len(chunk_line)
            if used_chars + added_chars > self._context_budget_chars and len(lines) > 1:
                lines.append("- Additional semantic memories were omitted to stay within the context budget.")
                break
            lines.append(result_line)
            if evidence_line:
                lines.append(evidence_line)
            if chunk:
                lines.append(chunk_line)
            used_chars += added_chars
        return "\n".join(lines)

    def _retrieve_text(self, query: str, session_id: str) -> str:
        started_at = perf_counter()
        request_payload = {
            "query": _trim(query, 2000),
            "limit": self._retrieve_limit,
        }
        primary_scope = self._build_scope(session_id)
        discovered_scopes: list[dict[str, Any]] = []
        try:
            discovered_scopes = self._discover_memory_scopes()
        except Exception as exc:
            _log_retrieval_diagnostic(
                logging.WARNING,
                "scope_discovery_failed",
                error_class=exc.__class__.__name__,
                timeout_seconds=self._timeout_seconds,
            )

        agent_scope_key = None
        if primary_scope and primary_scope["type"] == "agent":
            agent_scope_key = _scope_key(primary_scope)
        elif self._agent_identity:
            agent_scope_key = self._agent_identity

        if not self._is_bound_hermes_oauth_client():
            try:
                route_started_at = perf_counter()
                workspace_scope_keys = self._workspace_scope_keys_for_agent_retrieve(
                    primary_scope,
                    discovered_scopes,
                )
                response = self._request_json(
                    "POST",
                    "/api/v1/memory/retrieve-agent",
                    {
                        **request_payload,
                        "candidate_limit": self._agent_candidate_limit,
                        "broad_candidate_limit": self._agent_broad_candidate_limit,
                        "display_limit": self._agent_display_limit,
                        "context_budget_chars": self._context_budget_chars,
                        "agent_scope_key": agent_scope_key,
                        "include_agent_scope_patterns": self._include_agent_scope_patterns,
                        "agent_scope_pattern_limit": self._agent_scope_pattern_limit,
                        "workspace_scope_keys": workspace_scope_keys,
                        "include_tenant_shared": self._include_tenant_shared,
                        "tenant_shared_policy": "fallback_only"
                        if self._include_tenant_shared
                        else "never",
                        "include_broad_corpus": self._include_broad_corpus,
                        "broad_corpus_policy": "enabled"
                        if self._include_broad_corpus
                        else "disabled",
                        "workspace_strict": bool(workspace_scope_keys)
                        and not self._include_agent_scope_patterns,
                    },
                )
                trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
                searched_scopes = trace.get("searched_scopes") if isinstance(trace, dict) else []
                searched_scope_labels = _safe_scope_labels(
                    searched_scopes if isinstance(searched_scopes, list) else []
                )
                _log_retrieval_diagnostic(
                    logging.INFO,
                    "route_aware_success",
                    endpoint="/api/v1/memory/retrieve-agent",
                    elapsed_ms=round((perf_counter() - route_started_at) * 1000),
                    timeout_seconds=self._timeout_seconds,
                    discovered_scope_count=len(discovered_scopes),
                    discovered_scope_type_counts=_scope_type_counts(discovered_scopes),
                    searched_scope_count=len(searched_scope_labels),
                    searched_scopes=searched_scope_labels,
                    result_count=len(response.get("results") or []),
                    fallback_used=trace.get("fallback_used"),
                    budget_truncated=trace.get("budget_truncated")
                    or trace.get("context_budget_truncated"),
                )
                text = self._format_agent_retrieve_response(
                    response,
                    discovered_scopes=discovered_scopes,
                )
                if text:
                    return text
            except Exception as exc:
                fallback_scopes = self._build_retrieve_scopes(session_id, discovered_scopes)
                _log_retrieval_diagnostic(
                    logging.WARNING,
                    "route_aware_failed",
                    endpoint="/api/v1/memory/retrieve-agent",
                    elapsed_ms=round((perf_counter() - started_at) * 1000),
                    timeout_seconds=self._timeout_seconds,
                    error_class=exc.__class__.__name__,
                    discovered_scope_count=len(discovered_scopes),
                    discovered_scope_type_counts=_scope_type_counts(discovered_scopes),
                    fallback_scope_count=len(fallback_scopes),
                    fallback_scopes=_safe_scope_labels(fallback_scopes),
                )

        scope_responses: list[tuple[dict[str, str], dict[str, Any]]] = []
        fallback_scopes = self._build_retrieve_scopes(session_id, discovered_scopes)
        failed_scopes: list[str] = []
        for scope in fallback_scopes:
            try:
                response = self._request_json(
                    "POST",
                    "/api/v1/memory/retrieve",
                    {
                        **request_payload,
                        "scope": scope,
                    },
                )
            except Exception as exc:
                failed_scopes.append(_scope_label(scope))
                logger.debug(
                    "Palace of Truth retrieve failed for scope %s: %s",
                    _scope_label(scope),
                    exc,
                )
                continue
            scope_responses.append((scope, response))

        if not scope_responses:
            _log_retrieval_diagnostic(
                logging.WARNING,
                "fallback_failed",
                endpoint="/api/v1/memory/retrieve",
                elapsed_ms=round((perf_counter() - started_at) * 1000),
                timeout_seconds=self._timeout_seconds,
                fallback_scope_count=len(fallback_scopes),
                fallback_scopes=_safe_scope_labels(fallback_scopes),
                failed_scope_count=len(failed_scopes),
                failed_scopes=failed_scopes,
            )
            return ""

        merged_results = _merge_retrieval_results(_annotate_retrieval_results(scope_responses))
        _log_retrieval_diagnostic(
            logging.INFO,
            "fallback_complete",
            endpoint="/api/v1/memory/retrieve",
            elapsed_ms=round((perf_counter() - started_at) * 1000),
            timeout_seconds=self._timeout_seconds,
            fallback_scope_count=len(fallback_scopes),
            fallback_scopes=_safe_scope_labels(fallback_scopes),
            successful_scope_count=len(scope_responses),
            successful_scopes=[_scope_label(scope) for scope, _ in scope_responses],
            failed_scope_count=len(failed_scopes),
            failed_scopes=failed_scopes,
            result_count=len(merged_results),
        )
        if not merged_results:
            return ""

        traces = [
            response.get("trace")
            for _, response in scope_responses
            if isinstance(response.get("trace"), dict)
        ]
        lines = ["Recalled context from Palace of Truth:"]
        if len(scope_responses) > 1:
            lines.append(
                "- Retrieval searched scopes: "
                + ", ".join(_scope_label(scope) for scope, _ in scope_responses)
                + "."
            )
        if any(trace.get("fallback_used") for trace in traces):
            lines.append("- Retrieval fell back to a broader Palace route.")
        warnings: list[str] = []
        for trace in traces:
            warning = str(trace.get("completeness_warning") or "").strip()
            if warning and warning not in warnings:
                warnings.append(warning)
        for warning in warnings:
            lines.append(f"- Completeness warning: {warning}")

        for result in _presentation_sorted_results(merged_results)[: self._retrieve_limit]:
            title = _trim(str(result.get("title") or "Untitled memory"), 120)
            chunk = _trim(
                str(
                    result.get("chunk_text")
                    or result.get("summary")
                    or result.get("body")
                    or ""
                ).replace("\n", " "),
                500,
            )
            score = result.get("score")
            source_type = str(result.get("source_type") or "").strip()
            scope_label = _scope_label_from_result(result)
            qualifiers = [part for part in (source_type, scope_label) if part]
            title_with_context = title
            if qualifiers:
                title_with_context = f"{title} [{', '.join(qualifiers)}]"
            if isinstance(score, (int, float)):
                lines.append(f"- [{score:.2f}] {title_with_context}")
            else:
                lines.append(f"- {title_with_context}")
            evidence_line = _format_result_evidence(result, base_url=self._base_url)
            if evidence_line:
                lines.append(evidence_line)
            if chunk:
                lines.append(f"  Snippet: {chunk}")

        return "\n".join(lines)

    def _build_entry_payload(
        self,
        user_content: str,
        assistant_content: str,
        session_id: str,
        *,
        tenant_id: str,
    ) -> dict[str, Any]:
        scope = self._build_scope(session_id)
        title_seed = _first_line(user_content, _first_line(assistant_content, "Conversation turn"))
        title = _trim(f"{self._agent_identity or 'hermes'}: {title_seed}", 160)
        body = "\n\n".join(
            [
                "# Conversation Turn",
                "",
                "## User",
                user_content or "(empty)",
                "",
                "## Assistant",
                assistant_content or "(empty)",
            ]
        ).strip()
        body_text, truncation_metadata = _body_with_truncation_metadata(body)
        summary = _trim(
            _first_line(assistant_content, _first_line(user_content, "Conversation turn")),
            280,
        )
        idempotency_key = hashlib.sha256(
            json.dumps(
                {
                    "session_id": session_id or self._session_id,
                    "scope": scope,
                    "agent_identity": self._agent_identity,
                    "user": user_content,
                    "assistant": assistant_content,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "title": title,
            "body": body_text,
            "summary": summary,
            "source": self._source,
            "created_at": _utc_now(),
            "created_by_role": self._created_by_role,
            "metadata": {
                "provider": "palaceoftruth",
                "session_id": session_id or self._session_id,
                "agent_identity": self._agent_identity,
                "agent_workspace": self._agent_workspace,
                "platform": self._platform,
                **truncation_metadata,
            },
            "idempotency_key": idempotency_key,
        }
        if scope is not None:
            payload["scope"] = scope
        return payload

    def _normalize_bulk_memory_entries(
        self,
        raw_contents: list[Any],
        args: dict[str, Any],
        default_target: str,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        default_relationship_policy = (
            _optional_str(args.get("default_relationship_policy")) or "immediate"
        )
        if default_relationship_policy not in RELATIONSHIP_POLICIES:
            raise ValueError("default_relationship_policy is unsupported")
        default_fact_kind = _optional_str(args.get("default_fact_kind"))
        if default_fact_kind and default_fact_kind not in FACT_KINDS:
            raise ValueError("default_fact_kind is unsupported")

        for raw in raw_contents:
            if isinstance(raw, dict):
                content = _optional_str(raw.get("content"))
                target = _optional_str(raw.get("target")) or default_target
                fact_kind = _optional_str(raw.get("fact_kind")) or default_fact_kind
                relationship_policy = (
                    _optional_str(raw.get("relationship_policy")) or default_relationship_policy
                )
                entry = {
                    "content": content,
                    "target": target,
                    "valid_from": _optional_str(raw.get("valid_from"))
                    or _optional_str(args.get("default_valid_from")),
                    "valid_until": _optional_str(raw.get("valid_until"))
                    or _optional_str(args.get("default_valid_until")),
                    "supersedes_entry_id": _optional_str(raw.get("supersedes_entry_id")),
                    "fact_kind": fact_kind,
                    "enable_ai_enrichment": _optional_bool(
                        raw.get("enable_ai_enrichment"),
                        _optional_bool(args.get("default_enable_ai_enrichment")),
                    ),
                    "relationship_policy": relationship_policy,
                }
            else:
                entry = {
                    "content": _optional_str(raw),
                    "target": default_target,
                    "valid_from": _optional_str(args.get("default_valid_from")),
                    "valid_until": _optional_str(args.get("default_valid_until")),
                    "supersedes_entry_id": None,
                    "fact_kind": default_fact_kind,
                    "enable_ai_enrichment": _optional_bool(
                        args.get("default_enable_ai_enrichment")
                    ),
                    "relationship_policy": default_relationship_policy,
                }
            if not entry["content"]:
                raise ValueError("contents must contain 1 to 100 non-empty strings or objects")
            if entry["target"] not in {"memory", "user"}:
                raise ValueError("target must be memory or user")
            if entry["fact_kind"] and entry["fact_kind"] not in FACT_KINDS:
                raise ValueError("fact_kind is unsupported")
            if entry["relationship_policy"] not in RELATIONSHIP_POLICIES:
                raise ValueError("relationship_policy is unsupported")
            entries.append(entry)
        if not entries:
            raise ValueError("contents must contain 1 to 100 non-empty strings or objects")
        return entries

    def _build_memory_write_payload(
        self,
        *,
        action: str,
        target: str,
        content: str,
        tenant_id: str,
        valid_from: str | None = None,
        valid_until: str | None = None,
        supersedes_entry_id: str | None = None,
        fact_kind: str | None = None,
        enable_ai_enrichment: bool = False,
        relationship_policy: str = "immediate",
    ) -> dict[str, Any]:
        _reject_oversized_explicit_write(content)
        if fact_kind and fact_kind not in FACT_KINDS:
            raise ValueError("fact_kind is unsupported")
        if relationship_policy not in RELATIONSHIP_POLICIES:
            raise ValueError("relationship_policy is unsupported")
        scope = self._build_scope(self._session_id)
        target_label = "user profile" if target == "user" else "memory"
        title = _trim(
            f"{self._agent_identity or 'hermes'} {target_label}: "
            f"{_first_line(content, 'Memory entry')}",
            160,
        )
        summary = _trim(_first_line(content, "Memory entry"), 280)
        idempotency_key = hashlib.sha256(
            json.dumps(
                {
                    "kind": "memory_tool",
                    "action": action,
                    "target": target,
                    "scope": scope,
                    "agent_identity": self._agent_identity,
                    "content": content,
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "supersedes_entry_id": supersedes_entry_id,
                    "fact_kind": fact_kind,
                    "enable_ai_enrichment": enable_ai_enrichment,
                    "relationship_policy": relationship_policy,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        tags = [
            "hermes-memory-tool",
            f"hermes-memory-target-{target}",
            f"hermes-memory-action-{action}",
            *[f"{SKILL_TAG_PREFIX}{skill}" for skill in self._active_skills],
        ]
        memory_tool_metadata: dict[str, Any] = {
            "action": action,
            "target": target,
        }
        for key, value in (
            ("valid_from", valid_from),
            ("valid_until", valid_until),
            ("supersedes_entry_id", supersedes_entry_id),
            ("fact_kind", fact_kind),
        ):
            if value:
                memory_tool_metadata[key] = value
        if enable_ai_enrichment:
            memory_tool_metadata["enable_ai_enrichment"] = True
        if relationship_policy != "immediate":
            memory_tool_metadata["relationship_policy"] = relationship_policy

        metadata: dict[str, Any] = {
            "provider": "palaceoftruth",
            "session_id": self._session_id,
            "agent_identity": self._agent_identity,
            "agent_workspace": self._agent_workspace,
            "platform": self._platform,
            "memory_tool": memory_tool_metadata,
        }
        if self._active_skills:
            metadata["active_skills"] = list(self._active_skill_names)

        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "title": title,
            "body": content,
            "summary": summary,
            "source": f"{self._source}-memory-tool",
            "created_at": _utc_now(),
            "created_by_role": "system",
            "tags": tags,
            "metadata": metadata,
            "idempotency_key": idempotency_key,
            "enable_ai_enrichment": enable_ai_enrichment,
            "relationship_policy": relationship_policy,
        }
        if valid_from:
            payload["valid_from"] = valid_from
        if valid_until:
            payload["valid_until"] = valid_until
        if supersedes_entry_id:
            payload["supersedes_entry_id"] = supersedes_entry_id
        if fact_kind:
            payload["fact_kind"] = fact_kind
        if scope is not None:
            payload["scope"] = scope
        return payload


def register(ctx) -> None:
    ctx.register_memory_provider(PalaceOfTruthMemoryProvider())
