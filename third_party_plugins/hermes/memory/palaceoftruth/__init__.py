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
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

DEFAULT_RETRIEVE_LIMIT = 5
DEFAULT_AGENT_CANDIDATE_LIMIT = 20
DEFAULT_AGENT_DISPLAY_LIMIT = 12
DEFAULT_CONTEXT_BUDGET_CHARS = 4000
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_SOURCE = "hermes-agent"
DEFAULT_CREATED_BY_ROLE = "assistant"
SEARCH_TOOL_NAME = "palace_search"
REMEMBER_TOOL_NAME = "palace_remember"
SKILL_TAG_PREFIX = "skill-"
SCOPE_TYPES = {"session", "agent", "workspace", "tenant_shared"}
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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


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
        "include_tenant_shared": _env_bool("PALACEOFTRUTH_INCLUDE_TENANT_SHARED", False),
        "include_broad_corpus": _env_bool("PALACEOFTRUTH_INCLUDE_BROAD_CORPUS", False),
        "timeout_seconds": _env_int(
            "PALACEOFTRUTH_REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS
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
        self._scope_type = "agent"
        self._scope_key = ""
        self._retrieve_limit = DEFAULT_RETRIEVE_LIMIT
        self._agent_candidate_limit = DEFAULT_AGENT_CANDIDATE_LIMIT
        self._agent_broad_candidate_limit = DEFAULT_AGENT_CANDIDATE_LIMIT
        self._agent_display_limit = DEFAULT_AGENT_DISPLAY_LIMIT
        self._context_budget_chars = DEFAULT_CONTEXT_BUDGET_CHARS
        self._include_tenant_shared = False
        self._include_broad_corpus = False
        self._timeout_seconds = DEFAULT_TIMEOUT_SECONDS
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
        return bool(base_url.startswith(("http://", "https://")) and api_key)

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
                "required": True,
                "env_var": "PALACEOFTRUTH_API_KEY",
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
                "key": "timeout_seconds",
                "description": "HTTP timeout in seconds",
                "default": str(DEFAULT_TIMEOUT_SECONDS),
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
        self._timeout_seconds = int(
            self._config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
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
                "name": REMEMBER_TOOL_NAME,
                "description": (
                    "Save a concise durable memory to Palace of Truth under the "
                    "active Hermes orchestrator scope."
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
                    },
                    "required": ["content"],
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
            text = self._retrieve_text(query, self._session_id)
            return json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "result": text or "No Palace of Truth memory matched this query.",
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
            payload = self._build_memory_write_payload(
                action="add",
                target=target,
                content=content,
                tenant_id=tenant_id,
            )
            response = self._request_json("POST", "/api/v1/memory/entries", payload)
            return json.dumps(
                {
                    "ok": True,
                    "target": target,
                    "scope": payload.get("scope"),
                    "response": response,
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
        if not query or not self._base_url or not self._api_key:
            return ""
        with self._prefetch_lock:
            if (
                self._prefetch_cache["query"] == query
                and self._prefetch_cache["session_id"] == active_session
                and self._prefetch_cache["workspace"] == active_workspace
            ):
                return self._prefetch_cache["text"]
        text = self._retrieve_text(query, active_session)
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
        if not query or not self._base_url or not self._api_key:
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return

        def _worker() -> None:
            text = self._retrieve_text(query, active_session)
            with self._prefetch_lock:
                self._prefetch_cache = {
                    "query": query,
                    "session_id": active_session,
                    "workspace": active_workspace,
                    "text": text,
                }

        self._prefetch_thread = threading.Thread(target=_worker, daemon=True)
        self._prefetch_thread.start()

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        if self._writes_disabled or not self._base_url or not self._api_key:
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
                self._request_json("POST", "/api/v1/memory/entries", payload)
            except Exception as exc:
                logger.warning("Palace of Truth sync failed: %s", exc)

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

    def on_memory_write(self, action: str, target: str, content: str) -> None:
        if self._writes_disabled or not self._base_url or not self._api_key:
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
                self._request_json("POST", "/api/v1/memory/entries", payload)
            except Exception as exc:
                logger.warning("Palace of Truth memory mirror failed: %s", exc)

        self._sync_thread = threading.Thread(target=_worker, daemon=True)
        self._sync_thread.start()

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
        body = None
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
        }
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
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Palace of Truth {method} {path} failed: {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(f"Palace of Truth {method} {path} failed: {exc}") from exc
        if not raw.strip():
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Palace of Truth {method} {path} returned non-object JSON")
        return parsed

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

        _append_scope(primary_scope)
        for workspace_key in self._workspace_scope_keys_for_agent_retrieve(
            primary_scope,
            discovered_scopes or [],
        ):
            _append_scope({"type": "workspace", "key": workspace_key})
        if self._include_tenant_shared and primary_scope["type"] != "tenant_shared":
            _append_scope({"type": "tenant_shared"})
        return scopes

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
                discovered.append({"type": "tenant_shared"})
                continue
            key = _scope_key(scope)
            if key:
                discovered.append({"type": scope_type, "key": key})
        return discovered

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
                    "workspace_scope_keys": workspace_scope_keys,
                    "include_tenant_shared": self._include_tenant_shared,
                    "tenant_shared_policy": "fallback_only"
                    if self._include_tenant_shared
                    else "never",
                    "include_broad_corpus": self._include_broad_corpus,
                    "broad_corpus_policy": "enabled"
                    if self._include_broad_corpus
                    else "disabled",
                    "workspace_strict": bool(workspace_scope_keys),
                },
            )
            trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
            searched_scopes = trace.get("searched_scopes") if isinstance(trace, dict) else []
            searched_scope_labels = _safe_scope_labels(searched_scopes if isinstance(searched_scopes, list) else [])
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
                budget_truncated=trace.get("budget_truncated") or trace.get("context_budget_truncated"),
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
            "body": _trim(body, 24000),
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
            },
            "idempotency_key": idempotency_key,
        }
        if scope is not None:
            payload["scope"] = scope
        return payload

    def _build_memory_write_payload(
        self, *, action: str, target: str, content: str, tenant_id: str
    ) -> dict[str, Any]:
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
        metadata: dict[str, Any] = {
            "provider": "palaceoftruth",
            "session_id": self._session_id,
            "agent_identity": self._agent_identity,
            "agent_workspace": self._agent_workspace,
            "platform": self._platform,
            "memory_tool": {
                "action": action,
                "target": target,
            },
        }
        if self._active_skills:
            metadata["active_skills"] = list(self._active_skill_names)

        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "title": title,
            "body": _trim(content, 24000),
            "summary": summary,
            "source": f"{self._source}-memory-tool",
            "created_at": _utc_now(),
            "created_by_role": "system",
            "tags": tags,
            "metadata": metadata,
            "idempotency_key": idempotency_key,
        }
        if scope is not None:
            payload["scope"] = scope
        return payload


def register(ctx) -> None:
    ctx.register_memory_provider(PalaceOfTruthMemoryProvider())
