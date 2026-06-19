from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.server.transport_security import TransportSecuritySettings

from app.services.codex_memory_privacy import scan_codex_memory_privacy


ScopeType = Literal["session", "agent", "workspace", "tenant_shared"]
WakeupBriefScopeType = Literal["tenant", "wing"]
CheckpointScopeType = Literal["session", "agent", "workspace"]
CheckpointKind = Literal["stop", "precompact", "manual", "handoff"]
MemoryJobStatusFilter = Literal[
    "queued",
    "processing",
    "complete",
    "completed",
    "duplicate",
    "failed",
    "cancelled",
]


PALACE_API_KEY_ENVS = ("PALACEOFTRUTH_API_KEY", "SECONDBRAIN_API_KEY", "API_KEY")
PALACE_MCP_BEARER_TOKEN_ENVS = ("PALACEOFTRUTH_MCP_BEARER_TOKEN", "SECONDBRAIN_MCP_BEARER_TOKEN")
PALACE_MCP_OAUTH_CLIENT_SECRET_ENVS = (
    "PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET",
    "SECONDBRAIN_MCP_OAUTH_CLIENT_SECRET",
)
PALACE_MCP_OAUTH_TOKEN_URL_ENVS = ("PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL", "SECONDBRAIN_MCP_OAUTH_TOKEN_URL")
PALACE_API_BASE_URL_ENVS = ("PALACEOFTRUTH_API_BASE_URL", "SECONDBRAIN_API_BASE_URL")
PALACE_MCP_TIMEOUT_ENVS = ("PALACEOFTRUTH_MCP_TIMEOUT_SECONDS", "SECONDBRAIN_MCP_TIMEOUT_SECONDS")
PALACE_MCP_TRANSPORT_ENVS = ("PALACEOFTRUTH_MCP_TRANSPORT", "SECONDBRAIN_MCP_TRANSPORT")
PALACE_MCP_HOST_ENVS = ("PALACEOFTRUTH_MCP_HOST", "SECONDBRAIN_MCP_HOST")
PALACE_MCP_PORT_ENVS = ("PALACEOFTRUTH_MCP_PORT", "SECONDBRAIN_MCP_PORT")
PALACE_MCP_PATH_ENVS = ("PALACEOFTRUTH_MCP_PATH", "SECONDBRAIN_MCP_PATH")
PALACE_MCP_ALLOWED_HOSTS_ENVS = ("PALACEOFTRUTH_MCP_ALLOWED_HOSTS", "SECONDBRAIN_MCP_ALLOWED_HOSTS")
PALACE_MCP_ALLOWED_ORIGINS_ENVS = ("PALACEOFTRUTH_MCP_ALLOWED_ORIGINS", "SECONDBRAIN_MCP_ALLOWED_ORIGINS")
PALACE_MCP_CLIENT_KEY_ENVS = ("PALACEOFTRUTH_MCP_CLIENT_KEY", "SECONDBRAIN_MCP_CLIENT_KEY")
PALACE_MCP_CLIENT_NAME_ENVS = ("PALACEOFTRUTH_MCP_CLIENT_NAME", "SECONDBRAIN_MCP_CLIENT_NAME")
PALACE_MCP_CLIENT_SCOPES_ENVS = ("PALACEOFTRUTH_MCP_CLIENT_SCOPES", "SECONDBRAIN_MCP_CLIENT_SCOPES")
PALACE_MCP_APP_VERSION_ENVS = ("PALACEOFTRUTH_MCP_APP_VERSION", "SECONDBRAIN_MCP_APP_VERSION")
PALACE_MCP_CHECKPOINT_DISABLED_ENVS = (
    "PALACEOFTRUTH_MCP_CHECKPOINT_CAPTURE_DISABLED",
    "SECONDBRAIN_MCP_CHECKPOINT_CAPTURE_DISABLED",
)

McpOperationScope = Literal["read", "write", "admin", "local_only", "destructive_prohibited"]
ALL_MCP_OPERATION_SCOPES: tuple[McpOperationScope, ...] = (
    "read",
    "write",
    "admin",
    "local_only",
    "destructive_prohibited",
)
WRITE_OPERATIONS = {"create_memory_entry", "capture_checkpoint", "backfill_deferred_relationships"}
SECRET_PARAM_KEYS = {
    "api_key",
    "key_hash",
    "webhook_url",
    "body",
    "evidence_snippets",
    "query",
    "summary",
    "metadata",
}


def _env_value(names: tuple[str, ...], default: str | None = None) -> tuple[str | None, str | None]:
    for name in names:
        if name in os.environ:
            return os.environ[name].strip(), name
    return default, None


def _env_names_for_error(names: tuple[str, ...]) -> str:
    return ", ".join(names[:-1]) + f", or {names[-1]}"


def _validate_iso8601(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    try:
        datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO 8601 timestamp") from exc
    return cleaned


def _normalize_created_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return _validate_iso8601(value, "created_at")


def _normalize_optional_timestamp(field_name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _validate_iso8601(value, field_name)


def _validate_uuid_text(field_name: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    try:
        uuid.UUID(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc
    return cleaned


def _build_scope(scope_type: ScopeType, scope_key: str | None) -> dict[str, str]:
    if scope_type == "tenant_shared":
        if scope_key is not None:
            raise ValueError("scope_key must be omitted when scope_type is tenant_shared")
        return {"type": "tenant_shared"}
    if scope_key is None or not scope_key.strip():
        raise ValueError(f"scope_key is required when scope_type is {scope_type}")
    return {"type": scope_type, "key": scope_key.strip()}


def _env_truthy(names: tuple[str, ...]) -> bool:
    raw, _ = _env_value(names)
    if raw is None:
        return False
    return raw.lower() in {"1", "true", "yes", "on"}


def _normalize_checkpoint_evidence(evidence_snippets: list[str], *, max_snippets: int = 12) -> list[str]:
    normalized: list[str] = []
    for snippet in evidence_snippets:
        cleaned = " ".join(snippet.strip().split())
        if cleaned:
            normalized.append(cleaned[:1000])
        if len(normalized) >= max_snippets:
            break
    return normalized


def _ensure_checkpoint_text_is_safe(*, summary: str, evidence_snippets: list[str], metadata: dict[str, Any] | None) -> None:
    parts = [summary, *evidence_snippets]
    if metadata is not None:
        parts.append(json.dumps(metadata, sort_keys=True, default=str))
    scan = scan_codex_memory_privacy("\n".join(parts))
    if scan.has_findings:
        kinds = sorted({finding.kind for finding in scan.findings})
        raise ValueError(
            "checkpoint capture rejected possible raw secret content; "
            f"finding_kinds={', '.join(kinds)}"
        )


def _checkpoint_idempotency_key(
    *,
    checkpoint_kind: CheckpointKind,
    title: str,
    summary: str,
    evidence_snippets: list[str],
    scope_type: CheckpointScopeType,
    scope_key: str,
    source_url: str | None,
    created_at: str | None,
) -> str:
    identity = {
        "checkpoint_kind": checkpoint_kind,
        "created_at": created_at,
        "evidence_sha256": hashlib.sha256("\n".join(evidence_snippets).encode()).hexdigest(),
        "scope_key": scope_key,
        "scope_type": scope_type,
        "source_url": source_url,
        "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(),
        "title": title,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"checkpoint:{hashlib.sha256(canonical.encode()).hexdigest()[:53]}"


def _build_checkpoint_body(*, summary: str, evidence_snippets: list[str]) -> str:
    lines = ["Summary:", summary.strip()]
    if evidence_snippets:
        lines.extend(["", "Evidence snippets:"])
        lines.extend(f"- {snippet}" for snippet in evidence_snippets)
    return "\n".join(lines)


def _join_tags(tags: list[str] | None) -> str | None:
    if not tags:
        return None
    cleaned = [tag.strip() for tag in tags if tag.strip()]
    return ",".join(cleaned) if cleaned else None


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = response.text.strip()
        return body or response.reason_phrase
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return json.dumps(payload)


@dataclass(slots=True)
class SecondBrainMcpSettings:
    api_base_url: str
    api_key: str | None
    bearer_token: str | None = None
    oauth_client_secret: str | None = None
    oauth_token_url: str | None = None
    timeout_seconds: float = 30.0
    client_key: str = "default"
    client_name: str = "Palace MCP adapter"
    client_scopes: tuple[McpOperationScope, ...] = ALL_MCP_OPERATION_SCOPES
    app_version: str | None = None

    @classmethod
    def from_env(cls) -> "SecondBrainMcpSettings":
        api_key, _ = _env_value(PALACE_API_KEY_ENVS, "")
        bearer_token, _ = _env_value(PALACE_MCP_BEARER_TOKEN_ENVS)
        oauth_client_secret, _ = _env_value(PALACE_MCP_OAUTH_CLIENT_SECRET_ENVS)

        api_base_url, api_base_url_env = _env_value(PALACE_API_BASE_URL_ENVS, "http://127.0.0.1:8000")
        if not api_base_url:
            assert api_base_url_env is not None
            raise RuntimeError(f"{api_base_url_env} must not be blank")

        timeout_raw, timeout_env = _env_value(PALACE_MCP_TIMEOUT_ENVS, "30")
        assert timeout_raw is not None
        timeout_label = timeout_env or PALACE_MCP_TIMEOUT_ENVS[0]
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise RuntimeError(f"{timeout_label} must be numeric") from exc
        if timeout_seconds <= 0:
            raise RuntimeError(f"{timeout_label} must be greater than zero")

        client_key, client_key_env = _env_value(PALACE_MCP_CLIENT_KEY_ENVS, "default")
        client_name, client_name_env = _env_value(PALACE_MCP_CLIENT_NAME_ENVS, "Palace MCP adapter")
        assert client_key is not None and client_name is not None
        if not client_key.strip():
            raise RuntimeError(f"{client_key_env or PALACE_MCP_CLIENT_KEY_ENVS[0]} must not be blank")
        if not client_name.strip():
            raise RuntimeError(f"{client_name_env or PALACE_MCP_CLIENT_NAME_ENVS[0]} must not be blank")

        scope_raw, scope_env = _env_value(PALACE_MCP_CLIENT_SCOPES_ENVS)
        client_scopes = ALL_MCP_OPERATION_SCOPES
        if scope_raw is not None:
            cleaned_scopes = tuple(part.strip() for part in scope_raw.split(",") if part.strip())
            invalid = sorted(set(cleaned_scopes) - set(ALL_MCP_OPERATION_SCOPES))
            if invalid:
                label = scope_env or PALACE_MCP_CLIENT_SCOPES_ENVS[0]
                raise RuntimeError(f"{label} includes unsupported MCP scopes: {', '.join(invalid)}")
            client_scopes = cleaned_scopes or ALL_MCP_OPERATION_SCOPES

        app_version, _ = _env_value(PALACE_MCP_APP_VERSION_ENVS)
        oauth_token_url, _ = _env_value(PALACE_MCP_OAUTH_TOKEN_URL_ENVS)
        if oauth_token_url is None:
            oauth_token_url = f"{api_base_url.rstrip('/')}/api/v1/memory/mcp/oauth/token"
        if not api_key and not bearer_token and not oauth_client_secret:
            raise RuntimeError(
                f"{_env_names_for_error(PALACE_API_KEY_ENVS)}, "
                f"{_env_names_for_error(PALACE_MCP_BEARER_TOKEN_ENVS)}, or "
                f"{_env_names_for_error(PALACE_MCP_OAUTH_CLIENT_SECRET_ENVS)} is required for the MCP adapter"
            )
        return cls(
            api_base_url=api_base_url.rstrip("/"),
            api_key=api_key or None,
            bearer_token=bearer_token or None,
            oauth_client_secret=oauth_client_secret or None,
            oauth_token_url=oauth_token_url or None,
            timeout_seconds=timeout_seconds,
            client_key=client_key.strip(),
            client_name=client_name.strip(),
            client_scopes=client_scopes,  # type: ignore[arg-type]
            app_version=app_version or None,
        )


class SecondBrainApiClient:
    def __init__(
        self,
        settings: SecondBrainMcpSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=settings.timeout_seconds,
            headers={"User-Agent": "palaceoftruth-mcp/0.1.0"},
        )
        self._owns_client = client is None
        self._tenant_id: str | None = None
        self._bearer_token = settings.bearer_token
        self._bearer_expires_at: datetime | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=await self._auth_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _extract_error_detail(exc.response)
            raise RuntimeError(
                f"Palace API error {exc.response.status_code} for {exc.request.method} "
                f"{exc.request.url.path}: {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Failed to reach Palace API at {self.settings.api_base_url}: {exc}") from exc

        if response.content == b"":
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Palace API returned a non-JSON response for {method} {path}"
            ) from exc

    async def _auth_headers(self) -> dict[str, str]:
        if self.settings.api_key:
            return {"X-API-Key": self.settings.api_key}
        token = await self._active_bearer_token()
        return {"Authorization": f"Bearer {token}"}

    async def _active_bearer_token(self) -> str:
        if self._bearer_token and (
            self._bearer_expires_at is None or self._bearer_expires_at > datetime.now(timezone.utc) + timedelta(seconds=30)
        ):
            return self._bearer_token
        if not self.settings.oauth_client_secret or not self.settings.oauth_token_url:
            raise RuntimeError("MCP bearer token or OAuth client secret is required")
        response = await self._client.post(
            self.settings.oauth_token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.client_key,
                "client_secret": self.settings.oauth_client_secret,
                "scope": " ".join(self.settings.client_scopes),
            },
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token.strip():
            raise RuntimeError("Palace OAuth token endpoint did not return access_token")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise RuntimeError("Palace OAuth token endpoint did not return a valid expires_in")
        self._bearer_token = access_token
        self._bearer_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return access_token

    async def record_mcp_request_audit(
        self,
        *,
        operation: str,
        required_scope: McpOperationScope | None,
        params_summary: dict[str, Any],
        status: Literal["success", "error", "denied"],
        latency_ms: int | None,
        error_class: str | None,
    ) -> None:
        payload = {
            "client": {
                "client_key": self.settings.client_key,
                "display_name": self.settings.client_name,
                "allowed_scopes": list(self.settings.client_scopes),
                "metadata": {"transport": "mcp"},
            },
            "operation": operation,
            "required_scope": required_scope,
            "params_summary": params_summary,
            "status": status,
            "latency_ms": latency_ms,
            "error_class": error_class,
            "app_version": self.settings.app_version,
        }
        await self._request_json("POST", "/api/v1/memory/mcp/audit", json_body=payload)

    async def whoami(self) -> dict[str, Any]:
        payload = await self._request_json("GET", "/api/v1/memory/whoami")
        tenant_id = payload.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise RuntimeError("Palace API /memory/whoami did not return a tenant_id")
        self._tenant_id = tenant_id
        return payload

    async def tenant_id(self) -> str:
        if self._tenant_id is None:
            await self.whoami()
        assert self._tenant_id is not None
        return self._tenant_id

    async def create_memory_entry(
        self,
        *,
        title: str,
        body: str,
        source: str,
        created_at: str | None,
        summary: str | None,
        tags: list[str] | None,
        scope_type: ScopeType,
        scope_key: str | None,
        source_url: str | None,
        created_by_role: str | None,
        metadata: dict[str, Any] | None,
        idempotency_key: str | None,
        webhook_url: str | None,
        enable_ai_enrichment: bool,
        relationship_policy: str,
    ) -> dict[str, Any]:
        payload = {
            "tenant_id": await self.tenant_id(),
            "title": title,
            "body": body,
            "source": source,
            "created_at": _normalize_created_at(created_at),
            "summary": summary,
            "tags": tags or [],
            "scope": _build_scope(scope_type, scope_key),
            "source_url": source_url,
            "created_by_role": created_by_role,
            "metadata": metadata,
            "idempotency_key": idempotency_key,
            "webhook_url": webhook_url,
            "enable_ai_enrichment": enable_ai_enrichment,
            "relationship_policy": relationship_policy,
        }
        return await self._request_json("POST", "/api/v1/memory/entries", json_body=payload)

    async def get_memory_job(self, job_id: str) -> dict[str, Any]:
        return await self._request_json("GET", f"/api/v1/memory/jobs/{job_id}")

    async def list_memory_entries(
        self,
        *,
        scope_type: ScopeType,
        scope_key: str | None,
        tags: list[str] | None,
        tags_mode: Literal["any", "all"],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        scope = _build_scope(scope_type, scope_key)
        params: dict[str, Any] = {
            "scope_type": scope["type"],
            "tags_mode": tags_mode,
            "limit": limit,
        }
        if "key" in scope:
            params["scope_key"] = scope["key"]
        if tags:
            params["tags"] = tags
        normalized_cursor = _normalize_optional_timestamp("cursor", cursor)
        if normalized_cursor:
            params["cursor"] = normalized_cursor
        return await self._request_json("GET", "/api/v1/memory/entries", params=params)

    async def list_memory_scopes(
        self,
        *,
        limit: int,
        sample_limit: int,
    ) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            "/api/v1/memory/scopes",
            params={"limit": limit, "sample_limit": sample_limit},
        )

    async def list_memory_jobs(
        self,
        *,
        status: MemoryJobStatusFilter | None,
        page: int,
        per_page: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if status is not None:
            params["status"] = status
        return await self._request_json("GET", "/api/v1/memory/jobs", params=params)

    async def get_graph(
        self,
        *,
        item_id: str | None,
        include_orphans: bool,
        node_limit: int,
        edge_limit: int,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "include_orphans": include_orphans,
            "node_limit": node_limit,
            "edge_limit": edge_limit,
        }
        if item_id is not None:
            params["item_id"] = _validate_uuid_text("item_id", item_id)
        return await self._request_json("GET", "/api/v1/graph", params=params)

    async def get_item_relationships(self, *, item_id: str) -> dict[str, Any]:
        item_id = _validate_uuid_text("item_id", item_id)
        return await self._request_json("GET", f"/api/v1/items/{item_id}/related")

    async def list_temporal_facts(
        self,
        *,
        current_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "current_only": current_only,
            "limit": limit,
        }
        return await self._request_json("GET", "/api/v1/palace/facts", params=params)

    async def get_palace_room(self, *, room_id: str) -> dict[str, Any]:
        room_id = _validate_uuid_text("room_id", room_id)
        return await self._request_json("GET", f"/api/v1/palace/rooms/{room_id}")

    async def backfill_deferred_relationships(
        self,
        *,
        limit: int,
        defer_seconds: int,
    ) -> dict[str, Any]:
        payload = {
            "limit": limit,
            "defer_seconds": defer_seconds,
        }
        return await self._request_json("POST", "/api/v1/memory/relationships/backfill", json_body=payload)

    async def get_wakeup_brief(
        self,
        *,
        scope_type: WakeupBriefScopeType,
        scope_key: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"scope_type": scope_type}
        if scope_key is not None:
            params["scope_key"] = scope_key
        return await self._request_json("GET", "/api/v1/memory/wakeup-brief", params=params)

    async def retrieve_memory(
        self,
        *,
        query: str,
        limit: int,
        tags: list[str] | None,
        tags_mode: Literal["any", "all"],
        min_score: float | None,
        date_from: str | None,
        date_to: str | None,
        scope_type: ScopeType,
        scope_key: str | None,
        room_id: str | None,
        include_neighbor_chunks: bool = False,
        neighbor_chunk_window: int = 1,
        context_budget_chars: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "limit": limit,
            "include_neighbor_chunks": include_neighbor_chunks,
            "neighbor_chunk_window": neighbor_chunk_window,
            "context_budget_chars": context_budget_chars,
            "tags": tags,
            "tags_mode": tags_mode,
            "min_score": min_score,
            "date_from": _normalize_optional_timestamp("date_from", date_from),
            "date_to": _normalize_optional_timestamp("date_to", date_to),
            "scope": _build_scope(scope_type, scope_key),
            "room_id": room_id,
        }
        return await self._request_json("POST", "/api/v1/memory/retrieve", json_body=payload)

    async def retrieve_agent_memory(
        self,
        *,
        query: str,
        agent_scope_key: str | None,
        workspace_scope_keys: list[str] | None,
        session_scope_key: str | None,
        include_tenant_shared: bool,
        include_broad_corpus: bool,
        limit: int,
        candidate_limit: int | None,
        broad_candidate_limit: int | None,
        display_limit: int | None,
        context_budget_chars: int | None,
        tags: list[str] | None,
        tags_mode: Literal["any", "all"],
        min_score: float | None,
        date_from: str | None,
        date_to: str | None,
        include_agent_scope_keys: list[str] | None = None,
        include_all_permitted_agent_scopes: bool = False,
        access_reason: str | None = None,
        tenant_shared_policy: Literal["always", "fallback_only", "never"] = "always",
        broad_corpus_policy: Literal["default", "enabled", "disabled"] = "default",
        workspace_strict: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "agent_scope_key": agent_scope_key,
            "include_agent_scope_keys": include_agent_scope_keys or [],
            "include_all_permitted_agent_scopes": include_all_permitted_agent_scopes,
            "access_reason": access_reason,
            "workspace_scope_keys": workspace_scope_keys or [],
            "session_scope_key": session_scope_key,
            "include_tenant_shared": include_tenant_shared,
            "tenant_shared_policy": tenant_shared_policy,
            "include_broad_corpus": include_broad_corpus,
            "broad_corpus_policy": broad_corpus_policy,
            "workspace_strict": workspace_strict,
            "limit": limit,
            "candidate_limit": candidate_limit,
            "broad_candidate_limit": broad_candidate_limit,
            "display_limit": display_limit,
            "context_budget_chars": context_budget_chars,
            "tags": tags,
            "tags_mode": tags_mode,
            "min_score": min_score,
            "date_from": _normalize_optional_timestamp("date_from", date_from),
            "date_to": _normalize_optional_timestamp("date_to", date_to),
        }
        return await self._request_json("POST", "/api/v1/memory/retrieve-agent", json_body=payload)

    async def retrieve_memory_trajectory(
        self,
        *,
        query: str,
        trajectory_subject: str | None,
        agent_scope_key: str | None,
        workspace_scope_keys: list[str] | None,
        session_scope_key: str | None,
        include_tenant_shared: bool,
        include_broad_corpus: bool,
        limit: int,
        candidate_limit: int | None,
        display_limit: int | None,
        context_budget_chars: int | None,
        tags: list[str] | None,
        tags_mode: Literal["any", "all"],
        min_score: float | None,
        date_from: str | None,
        date_to: str | None,
        include_agent_scope_keys: list[str] | None = None,
        include_all_permitted_agent_scopes: bool = False,
        access_reason: str | None = None,
        tenant_shared_policy: Literal["always", "fallback_only", "never"] = "always",
        broad_corpus_policy: Literal["default", "enabled", "disabled"] = "disabled",
        workspace_strict: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "trajectory_subject": trajectory_subject,
            "agent_scope_key": agent_scope_key,
            "include_agent_scope_keys": include_agent_scope_keys or [],
            "include_all_permitted_agent_scopes": include_all_permitted_agent_scopes,
            "access_reason": access_reason,
            "workspace_scope_keys": workspace_scope_keys or [],
            "session_scope_key": session_scope_key,
            "include_tenant_shared": include_tenant_shared,
            "tenant_shared_policy": tenant_shared_policy,
            "include_broad_corpus": include_broad_corpus,
            "broad_corpus_policy": broad_corpus_policy,
            "workspace_strict": workspace_strict,
            "limit": limit,
            "candidate_limit": candidate_limit,
            "display_limit": display_limit,
            "context_budget_chars": context_budget_chars,
            "tags": tags,
            "tags_mode": tags_mode,
            "min_score": min_score,
            "date_from": _normalize_optional_timestamp("date_from", date_from),
            "date_to": _normalize_optional_timestamp("date_to", date_to),
        }
        return await self._request_json("POST", "/api/v1/memory/trajectory", json_body=payload)

    async def get_retrieval_doctor(
        self,
        *,
        agent_scope_key: str | None,
        workspace_scope_keys: list[str] | None,
        session_scope_key: str | None,
        include_tenant_shared: bool,
        include_broad_corpus: bool,
        candidate_limit: int,
        broad_candidate_limit: int | None,
        display_limit: int,
        context_budget_chars: int | None,
        sample_probes: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload = {
            "agent_scope_key": agent_scope_key,
            "workspace_scope_keys": workspace_scope_keys or [],
            "session_scope_key": session_scope_key,
            "include_tenant_shared": include_tenant_shared,
            "include_broad_corpus": include_broad_corpus,
            "candidate_limit": candidate_limit,
            "broad_candidate_limit": broad_candidate_limit,
            "display_limit": display_limit,
            "context_budget_chars": context_budget_chars,
            "sample_probes": sample_probes or [],
        }
        return await self._request_json("POST", "/api/v1/memory/retrieval-doctor", json_body=payload)

    async def search_items(
        self,
        *,
        query: str,
        limit: int,
        source_type: str | None,
        tags: list[str] | None,
        tags_mode: Literal["any", "all"],
        date_from: str | None,
        date_to: str | None,
        min_score: float | None,
    ) -> dict[str, Any]:
        payload = {
            "query": query,
            "limit": limit,
            "source_type": source_type,
            "tags": tags,
            "tags_mode": tags_mode,
            "date_from": _normalize_optional_timestamp("date_from", date_from),
            "date_to": _normalize_optional_timestamp("date_to", date_to),
            "min_score": min_score,
        }
        return await self._request_json("POST", "/api/v1/search", json_body=payload)

    async def list_tags(self, *, prefix: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if prefix is not None:
            params["q"] = prefix
        return await self._request_json("GET", "/api/v1/tags", params=params or None)

    async def list_items(
        self,
        *,
        page: int,
        per_page: int,
        source_type: str | None,
        tags: list[str] | None,
        date_from: str | None,
        date_to: str | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": per_page,
        }
        if source_type:
            params["source_type"] = source_type
        joined_tags = _join_tags(tags)
        if joined_tags:
            params["tags"] = joined_tags
        normalized_date_from = _normalize_optional_timestamp("date_from", date_from)
        normalized_date_to = _normalize_optional_timestamp("date_to", date_to)
        if normalized_date_from:
            params["date_from"] = normalized_date_from
        if normalized_date_to:
            params["date_to"] = normalized_date_to
        return await self._request_json("GET", "/api/v1/items", params=params)


@dataclass(slots=True)
class SecondBrainMcpRuntime:
    settings: SecondBrainMcpSettings
    api: SecondBrainApiClient


@asynccontextmanager
async def app_lifespan(_: FastMCP) -> AsyncIterator[SecondBrainMcpRuntime]:
    settings = SecondBrainMcpSettings.from_env()
    api = SecondBrainApiClient(settings)
    try:
        yield SecondBrainMcpRuntime(settings=settings, api=api)
    finally:
        await api.aclose()


def _runtime(ctx: Context[ServerSession, SecondBrainMcpRuntime]) -> SecondBrainMcpRuntime:
    return ctx.request_context.lifespan_context


def _operation_scope(operation: str) -> McpOperationScope:
    if operation in WRITE_OPERATIONS:
        return "write"
    return "read"


def _summarize_params(values: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key, value in values.items():
        if key == "ctx":
            continue
        if key in SECRET_PARAM_KEYS:
            summary[key] = {"redacted": True, "present": value is not None}
        elif isinstance(value, str):
            summary[key] = value if len(value) <= 80 else {"length": len(value)}
        elif isinstance(value, list):
            summary[key] = {"count": len(value)}
        elif isinstance(value, dict):
            summary[key] = {"keys": sorted(str(item) for item in value.keys())}
        else:
            summary[key] = value
    return summary


def _summarize_trace_for_audit(trace: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "searched_scopes",
        "caller_agent_scope_key",
        "requested_agent_scope_keys",
        "authorized_agent_scope_keys",
        "denied_agent_scope_keys",
        "delegated_agent_policy_id",
        "delegated_agent_policy_source",
        "delegated_agent_decision",
        "delegated_agent_deny_reasons",
        "access_reason_required",
        "access_reason_present",
        "result_counts_by_scope",
        "workspace_strict",
        "workspace_scope_exhausted",
        "tenant_shared_policy",
        "tenant_shared_fallback_used",
        "broad_corpus_policy",
        "broad_corpus_searched",
        "broad_corpus_skipped_reason",
        "selected_scope_query_count",
        "selected_scope_result_count",
        "broad_result_count",
        "deduped_result_count",
        "budget_truncated",
        "context_budget_truncated",
        "completeness_warnings",
    )
    return {
        key: trace[key]
        for key in allowed_keys
        if key in trace and trace[key] not in (None, [], {})
    }


def _summarize_result_for_audit(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    summary: dict[str, Any] = {}
    trace = result.get("trace")
    if isinstance(trace, dict):
        trace_summary = _summarize_trace_for_audit(trace)
        if trace_summary:
            summary["trace"] = trace_summary
    if "total" in result:
        summary["total"] = result["total"]
    results = result.get("results")
    if isinstance(results, list):
        summary["returned_result_count"] = len(results)
        scope_labels = []
        for row in results:
            if not isinstance(row, dict):
                continue
            label = row.get("retrieved_scope_label") or row.get("source_project")
            if isinstance(label, str) and label and label not in scope_labels:
                scope_labels.append(label)
        if scope_labels:
            summary["returned_scope_labels"] = scope_labels[:20]
    return summary


async def _record_audit_safely(
    runtime: SecondBrainMcpRuntime,
    *,
    operation: str,
    required_scope: McpOperationScope,
    params_summary: dict[str, Any],
    status: Literal["success", "error", "denied"],
    latency_ms: int | None,
    error_class: str | None,
) -> None:
    try:
        await runtime.api.record_mcp_request_audit(
            operation=operation,
            required_scope=required_scope,
            params_summary=params_summary,
            status=status,
            latency_ms=latency_ms,
            error_class=error_class,
        )
    except Exception:
        # Audit is best-effort from the adapter side so a logging outage does not
        # break existing local MCP clients. The server-side endpoint remains durable.
        return


async def _run_mcp_operation(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    *,
    operation: str,
    params: dict[str, Any],
    call: Callable[[], Awaitable[Any]],
) -> Any:
    runtime = _runtime(ctx)
    required_scope = _operation_scope(operation)
    params_summary = _summarize_params(params)
    params_summary["audit_request_id"] = str(uuid.uuid4())
    start = time.monotonic()
    if required_scope not in runtime.settings.client_scopes:
        latency_ms = int((time.monotonic() - start) * 1000)
        await _record_audit_safely(
            runtime,
            operation=operation,
            required_scope=required_scope,
            params_summary=params_summary,
            status="denied",
            latency_ms=latency_ms,
            error_class="PermissionError",
        )
        raise PermissionError(f"MCP client is not allowed to call {operation}; missing {required_scope} scope")
    try:
        result = await call()
    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        await _record_audit_safely(
            runtime,
            operation=operation,
            required_scope=required_scope,
            params_summary=params_summary,
            status="error",
            latency_ms=latency_ms,
            error_class=type(exc).__name__,
        )
        raise
    latency_ms = int((time.monotonic() - start) * 1000)
    if operation == "retrieve_agent_memory":
        result_summary = _summarize_result_for_audit(result)
        if result_summary:
            params_summary["result_summary"] = result_summary
    await _record_audit_safely(
        runtime,
        operation=operation,
        required_scope=required_scope,
        params_summary=params_summary,
        status="success",
        latency_ms=latency_ms,
        error_class=None,
    )
    return result


mcp = FastMCP(
    "Palace of Truth",
    instructions=(
        "Palace of Truth MCP adapter over the canonical REST memory and search API. "
        "Use palace_search, palace_remember, palace_checkpoint, and palace_context "
        "as Codex-friendly aliases for common agent memory workflows. "
        "Use create_memory_entry for durable memory writes, retrieve_memory for scoped recall, "
        "capture_checkpoint for safe Codex/Hermes checkpoint writes, "
        "list_memory_entries for deterministic scoped memory enumeration, "
        "list_memory_scopes, retrieve_agent_memory, and retrieve_memory_trajectory for route-aware agent recall, "
        "get_wakeup_brief for startup context, list_memory_jobs for read-only job health, "
        "get_graph/get_item_relationships/list_temporal_facts/get_palace_room for read-only graph, fact, "
        "relationship, and room/tunnel inspection, "
        "backfill_deferred_relationships after bulk deferred memory writes, "
        "and search_items/list_items/list_tags for corpus discovery."
    ),
    json_response=True,
    stateless_http=True,
    lifespan=app_lifespan,
)


async def _connection_info_json(ctx: Context[ServerSession, SecondBrainMcpRuntime]) -> str:
    async def call() -> str:
        runtime = _runtime(ctx)
        tenant = await runtime.api.tenant_id()
        return json.dumps(
            {
                "api_base_url": runtime.settings.api_base_url,
                "tenant_id": tenant,
            },
            indent=2,
        )

    return await _run_mcp_operation(
        ctx,
        operation="connection_info",
        params={},
        call=call,
    )


@mcp.resource("palaceoftruth://connection")
async def palace_connection_info(ctx: Context[ServerSession, SecondBrainMcpRuntime]) -> str:
    """Return the active Palace API target and authenticated tenant."""
    return await _connection_info_json(ctx)


@mcp.resource("secondbrain://connection")
async def connection_info(ctx: Context[ServerSession, SecondBrainMcpRuntime]) -> str:
    """Compatibility alias for the active Palace API target and authenticated tenant."""
    return await _connection_info_json(ctx)


@mcp.tool()
async def whoami(ctx: Context[ServerSession, SecondBrainMcpRuntime]) -> dict[str, Any]:
    """Validate the configured API key and return the authenticated tenant."""
    return await _run_mcp_operation(
        ctx,
        operation="whoami",
        params={},
        call=lambda: _runtime(ctx).api.whoami(),
    )


@mcp.tool()
async def create_memory_entry(
    title: str,
    body: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    source: str = "mcp",
    created_at: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    scope_type: ScopeType = "tenant_shared",
    scope_key: str | None = None,
    source_url: str | None = None,
    created_by_role: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    webhook_url: str | None = None,
    enable_ai_enrichment: bool = False,
    relationship_policy: str = "immediate",
) -> dict[str, Any]:
    """Store durable agent memory in Palace without requiring the caller to provide tenant_id."""
    return await _run_mcp_operation(
        ctx,
        operation="create_memory_entry",
        params={key: value for key, value in locals().items() if key != "call"},
        call=lambda: _runtime(ctx).api.create_memory_entry(
            title=title,
            body=body,
            source=source,
            created_at=created_at,
            summary=summary,
            tags=tags,
            scope_type=scope_type,
            scope_key=scope_key,
            source_url=source_url,
            created_by_role=created_by_role,
            metadata=metadata,
            idempotency_key=idempotency_key,
            webhook_url=webhook_url,
            enable_ai_enrichment=enable_ai_enrichment,
            relationship_policy=relationship_policy,
        ),
    )


@mcp.tool()
async def capture_checkpoint(
    title: str,
    summary: str,
    evidence_snippets: list[str],
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    scope_type: CheckpointScopeType,
    scope_key: str,
    checkpoint_kind: CheckpointKind = "manual",
    source: str = "mcp-checkpoint",
    created_at: str | None = None,
    tags: list[str] | None = None,
    source_url: str | None = None,
    created_by_role: str | None = "agent",
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    relationship_policy: Literal["deferred", "immediate", "skip"] = "deferred",
    queue_relationship_backfill: bool = True,
    backfill_limit: int = 25,
    backfill_defer_seconds: int = 15,
    dry_run: bool = False,
    read_after_write: bool = True,
    enable_ai_enrichment: bool = False,
) -> dict[str, Any]:
    """Capture a safe Codex/Hermes checkpoint summary as scoped Palace memory."""

    async def call() -> dict[str, Any]:
        if _env_truthy(PALACE_MCP_CHECKPOINT_DISABLED_ENVS):
            raise RuntimeError("checkpoint capture is disabled by PALACEOFTRUTH_MCP_CHECKPOINT_CAPTURE_DISABLED")

        cleaned_summary = summary.strip()
        if not cleaned_summary:
            raise ValueError("summary must not be blank")
        normalized_evidence = _normalize_checkpoint_evidence(evidence_snippets)
        if not normalized_evidence:
            raise ValueError("evidence_snippets must include at least one non-blank snippet")
        cleaned_scope = _build_scope(scope_type, scope_key)
        cleaned_scope_key = cleaned_scope["key"]
        _ensure_checkpoint_text_is_safe(
            summary=cleaned_summary,
            evidence_snippets=normalized_evidence,
            metadata=metadata,
        )
        checkpoint_tags = [
            "checkpoint",
            "codex-checkpoint",
            f"checkpoint-{checkpoint_kind}",
            *(tags or []),
        ]
        deduped_tags = list(dict.fromkeys(tag.strip() for tag in checkpoint_tags if tag.strip()))
        final_idempotency_key = idempotency_key or _checkpoint_idempotency_key(
            checkpoint_kind=checkpoint_kind,
            title=title,
            summary=cleaned_summary,
            evidence_snippets=normalized_evidence,
            scope_type=scope_type,
            scope_key=cleaned_scope_key,
            source_url=source_url,
            created_at=created_at,
        )
        if len(final_idempotency_key) > 64:
            raise ValueError("idempotency_key must be 64 characters or fewer")

        checkpoint_metadata = {
            "checkpoint": {
                "schema_version": 1,
                "kind": checkpoint_kind,
                "evidence_snippet_count": len(normalized_evidence),
                "relationship_backfill_requested": queue_relationship_backfill and relationship_policy == "deferred",
            },
            "client_metadata": metadata or {},
        }
        body = _build_checkpoint_body(summary=cleaned_summary, evidence_snippets=normalized_evidence)
        request_summary = {
            "title": title,
            "scope": cleaned_scope,
            "tags": deduped_tags,
            "idempotency_key": final_idempotency_key,
            "relationship_policy": relationship_policy,
            "evidence_snippet_count": len(normalized_evidence),
        }
        if dry_run:
            return {
                "status": "dry_run",
                "accepted": False,
                "would_write": request_summary,
                "relationship_backfill": {"queued": False, "reason": "dry_run"},
            }

        runtime = _runtime(ctx)
        accepted = await runtime.api.create_memory_entry(
            title=title,
            body=body,
            source=source,
            created_at=created_at,
            summary=cleaned_summary,
            tags=deduped_tags,
            scope_type=scope_type,
            scope_key=cleaned_scope_key,
            source_url=source_url,
            created_by_role=created_by_role,
            metadata=checkpoint_metadata,
            idempotency_key=final_idempotency_key,
            webhook_url=None,
            enable_ai_enrichment=enable_ai_enrichment,
            relationship_policy=relationship_policy,
        )
        job_id = accepted.get("job_id")
        job_ack: dict[str, Any] | None = None
        if read_after_write and isinstance(job_id, str) and job_id.strip():
            job_ack = await runtime.api.get_memory_job(job_id)

        relationship_backfill: dict[str, Any] = {"queued": False}
        if queue_relationship_backfill and relationship_policy == "deferred":
            relationship_backfill = await runtime.api.backfill_deferred_relationships(
                limit=backfill_limit,
                defer_seconds=backfill_defer_seconds,
            )

        return {
            "status": accepted.get("status", "accepted"),
            "accepted": True,
            "job_id": job_id,
            "accepted_as": accepted.get("accepted_as"),
            "scope": accepted.get("scope", cleaned_scope),
            "idempotency_key": final_idempotency_key,
            "memory_job": job_ack,
            "relationship_backfill": relationship_backfill,
        }

    return await _run_mcp_operation(
        ctx,
        operation="capture_checkpoint",
        params={key: value for key, value in locals().items() if key != "call"},
        call=call,
    )


@mcp.tool()
async def palace_checkpoint(
    title: str,
    summary: str,
    evidence_snippets: list[str],
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    scope_type: CheckpointScopeType = "agent",
    scope_key: str = "codex",
    checkpoint_kind: CheckpointKind = "manual",
    source: str = "mcp-checkpoint",
    created_at: str | None = None,
    tags: list[str] | None = None,
    source_url: str | None = None,
    created_by_role: str | None = "agent",
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    relationship_policy: Literal["deferred", "immediate", "skip"] = "deferred",
    queue_relationship_backfill: bool = True,
    backfill_limit: int = 25,
    backfill_defer_seconds: int = 15,
    dry_run: bool = False,
    read_after_write: bool = True,
    enable_ai_enrichment: bool = False,
) -> dict[str, Any]:
    """Codex-friendly alias for capture_checkpoint with agent/codex defaults."""
    return await capture_checkpoint(
        title=title,
        summary=summary,
        evidence_snippets=evidence_snippets,
        ctx=ctx,
        scope_type=scope_type,
        scope_key=scope_key,
        checkpoint_kind=checkpoint_kind,
        source=source,
        created_at=created_at,
        tags=tags,
        source_url=source_url,
        created_by_role=created_by_role,
        metadata=metadata,
        idempotency_key=idempotency_key,
        relationship_policy=relationship_policy,
        queue_relationship_backfill=queue_relationship_backfill,
        backfill_limit=backfill_limit,
        backfill_defer_seconds=backfill_defer_seconds,
        dry_run=dry_run,
        read_after_write=read_after_write,
        enable_ai_enrichment=enable_ai_enrichment,
    )


@mcp.tool()
async def get_memory_job(
    job_id: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
) -> dict[str, Any]:
    """Poll a previously accepted memory job until it reaches a terminal state."""
    return await _run_mcp_operation(
        ctx,
        operation="get_memory_job",
        params=locals(),
        call=lambda: _runtime(ctx).api.get_memory_job(job_id),
    )


@mcp.tool()
async def list_memory_entries(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    scope_type: ScopeType = "tenant_shared",
    scope_key: str | None = None,
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List recent memory entries for a scope without requiring a search query."""
    return await _run_mcp_operation(
        ctx,
        operation="list_memory_entries",
        params=locals(),
        call=lambda: _runtime(ctx).api.list_memory_entries(
            scope_type=scope_type,
            scope_key=scope_key,
            tags=tags,
            tags_mode=tags_mode,
            limit=limit,
            cursor=cursor,
        ),
    )


@mcp.tool()
async def list_memory_scopes(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    limit: int = 50,
    sample_limit: int = 8,
) -> dict[str, Any]:
    """Discover available tenant memory scopes without returning raw memory content."""
    return await _run_mcp_operation(
        ctx,
        operation="list_memory_scopes",
        params=locals(),
        call=lambda: _runtime(ctx).api.list_memory_scopes(
            limit=limit,
            sample_limit=sample_limit,
        ),
    )


@mcp.tool()
async def list_memory_jobs(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    status: MemoryJobStatusFilter | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict[str, Any]:
    """List recent memory jobs for read-only operational visibility; retry stays REST/UI-only."""
    return await _run_mcp_operation(
        ctx,
        operation="list_memory_jobs",
        params=locals(),
        call=lambda: _runtime(ctx).api.list_memory_jobs(
            status=status,
            page=page,
            per_page=per_page,
        ),
    )


@mcp.tool()
async def get_graph(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    item_id: str | None = None,
    include_orphans: bool = True,
    node_limit: int = 50,
    edge_limit: int = 100,
) -> dict[str, Any]:
    """Return a bounded tenant graph; pass item_id for a focused relationship neighborhood."""
    return await _run_mcp_operation(
        ctx,
        operation="get_graph",
        params=locals(),
        call=lambda: _runtime(ctx).api.get_graph(
            item_id=item_id,
            include_orphans=include_orphans,
            node_limit=node_limit,
            edge_limit=edge_limit,
        ),
    )


@mcp.tool()
async def get_item_relationships(
    item_id: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
) -> dict[str, Any]:
    """Inspect read-only graph relationships around one visible item by source item id."""
    return await _run_mcp_operation(
        ctx,
        operation="get_item_relationships",
        params=locals(),
        call=lambda: _runtime(ctx).api.get_item_relationships(item_id=item_id),
    )


@mcp.tool()
async def list_temporal_facts(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    current_only: bool = True,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """List traceable temporal facts with source item ids; no fact insertion or purge is exposed."""
    return await _run_mcp_operation(
        ctx,
        operation="list_temporal_facts",
        params=locals(),
        call=lambda: _runtime(ctx).api.list_temporal_facts(
            current_only=current_only,
            limit=limit,
        ),
    )


@mcp.tool()
async def get_palace_room(
    room_id: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
) -> dict[str, Any]:
    """Inspect a Palace room, memberships, representative items, and tunnel context by room id."""
    return await _run_mcp_operation(
        ctx,
        operation="get_palace_room",
        params=locals(),
        call=lambda: _runtime(ctx).api.get_palace_room(room_id=room_id),
    )


@mcp.tool()
async def backfill_deferred_relationships(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    limit: int = 50,
    defer_seconds: int = 15,
) -> dict[str, Any]:
    """Queue relationship extraction for deferred memory writes; job retry remains REST/UI-only."""
    return await _run_mcp_operation(
        ctx,
        operation="backfill_deferred_relationships",
        params=locals(),
        call=lambda: _runtime(ctx).api.backfill_deferred_relationships(
            limit=limit,
            defer_seconds=defer_seconds,
        ),
    )


@mcp.tool()
async def get_wakeup_brief(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    scope_type: WakeupBriefScopeType = "tenant",
    scope_key: str | None = None,
) -> dict[str, Any]:
    """Load the latest Palace wake-up brief for session-start context."""
    return await _run_mcp_operation(
        ctx,
        operation="get_wakeup_brief",
        params=locals(),
        call=lambda: _runtime(ctx).api.get_wakeup_brief(
            scope_type=scope_type,
            scope_key=scope_key,
        ),
    )


@mcp.tool()
async def retrieve_memory(
    query: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    limit: int = 5,
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    min_score: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    scope_type: ScopeType = "tenant_shared",
    scope_key: str | None = None,
    room_id: str | None = None,
    include_neighbor_chunks: bool = False,
    neighbor_chunk_window: int = 1,
    context_budget_chars: int | None = None,
) -> dict[str, Any]:
    """Retrieve scoped memory entries using the canonical Palace memory contract."""
    return await _run_mcp_operation(
        ctx,
        operation="retrieve_memory",
        params=locals(),
        call=lambda: _runtime(ctx).api.retrieve_memory(
            query=query,
            limit=limit,
            tags=tags,
            tags_mode=tags_mode,
            min_score=min_score,
            date_from=date_from,
            date_to=date_to,
            scope_type=scope_type,
            scope_key=scope_key,
            room_id=room_id,
            include_neighbor_chunks=include_neighbor_chunks,
            neighbor_chunk_window=neighbor_chunk_window,
            context_budget_chars=context_budget_chars,
        ),
    )


@mcp.tool()
async def retrieve_agent_memory(
    query: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    agent_scope_key: str | None = None,
    include_agent_scope_keys: list[str] | None = None,
    include_all_permitted_agent_scopes: bool = False,
    access_reason: str | None = None,
    workspace_scope_keys: list[str] | None = None,
    session_scope_key: str | None = None,
    include_tenant_shared: bool = True,
    tenant_shared_policy: Literal["always", "fallback_only", "never"] = "always",
    include_broad_corpus: bool = True,
    broad_corpus_policy: Literal["default", "enabled", "disabled"] = "default",
    workspace_strict: bool = False,
    limit: int = 5,
    candidate_limit: int | None = None,
    broad_candidate_limit: int | None = None,
    display_limit: int | None = None,
    context_budget_chars: int | None = None,
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    min_score: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Retrieve agent context across allowed own-agent, selected workspace, shared, and public corpus routes."""
    return await _run_mcp_operation(
        ctx,
        operation="retrieve_agent_memory",
        params=locals(),
        call=lambda: _runtime(ctx).api.retrieve_agent_memory(
            query=query,
            agent_scope_key=agent_scope_key,
            include_agent_scope_keys=include_agent_scope_keys,
            include_all_permitted_agent_scopes=include_all_permitted_agent_scopes,
            access_reason=access_reason,
            workspace_scope_keys=workspace_scope_keys,
            session_scope_key=session_scope_key,
            include_tenant_shared=include_tenant_shared,
            tenant_shared_policy=tenant_shared_policy,
            include_broad_corpus=include_broad_corpus,
            broad_corpus_policy=broad_corpus_policy,
            workspace_strict=workspace_strict,
            limit=limit,
            candidate_limit=candidate_limit,
            broad_candidate_limit=broad_candidate_limit,
            display_limit=display_limit,
            context_budget_chars=context_budget_chars,
            tags=tags,
            tags_mode=tags_mode,
            min_score=min_score,
            date_from=date_from,
            date_to=date_to,
        ),
    )


@mcp.tool()
async def retrieve_memory_trajectory(
    query: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    trajectory_subject: str | None = None,
    agent_scope_key: str | None = "codex",
    include_agent_scope_keys: list[str] | None = None,
    include_all_permitted_agent_scopes: bool = False,
    access_reason: str | None = None,
    workspace_scope_keys: list[str] | None = None,
    session_scope_key: str | None = None,
    include_tenant_shared: bool = True,
    tenant_shared_policy: Literal["always", "fallback_only", "never"] = "always",
    include_broad_corpus: bool = False,
    broad_corpus_policy: Literal["default", "enabled", "disabled"] = "disabled",
    workspace_strict: bool = False,
    limit: int = 10,
    candidate_limit: int | None = None,
    display_limit: int | None = None,
    context_budget_chars: int | None = None,
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    min_score: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Return an ordered, source-spanned timeline from scoped conversation facts."""
    return await _run_mcp_operation(
        ctx,
        operation="retrieve_memory_trajectory",
        params=locals(),
        call=lambda: _runtime(ctx).api.retrieve_memory_trajectory(
            query=query,
            trajectory_subject=trajectory_subject,
            agent_scope_key=agent_scope_key,
            include_agent_scope_keys=include_agent_scope_keys,
            include_all_permitted_agent_scopes=include_all_permitted_agent_scopes,
            access_reason=access_reason,
            workspace_scope_keys=workspace_scope_keys,
            session_scope_key=session_scope_key,
            include_tenant_shared=include_tenant_shared,
            tenant_shared_policy=tenant_shared_policy,
            include_broad_corpus=include_broad_corpus,
            broad_corpus_policy=broad_corpus_policy,
            workspace_strict=workspace_strict,
            limit=limit,
            candidate_limit=candidate_limit,
            display_limit=display_limit,
            context_budget_chars=context_budget_chars,
            tags=tags,
            tags_mode=tags_mode,
            min_score=min_score,
            date_from=date_from,
            date_to=date_to,
        ),
    )


@mcp.tool()
async def palace_search(
    query: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    agent_scope_key: str | None = "codex",
    include_agent_scope_keys: list[str] | None = None,
    include_all_permitted_agent_scopes: bool = False,
    access_reason: str | None = None,
    workspace_scope_keys: list[str] | None = None,
    session_scope_key: str | None = None,
    include_tenant_shared: bool = True,
    tenant_shared_policy: Literal["always", "fallback_only", "never"] = "always",
    include_broad_corpus: bool = False,
    broad_corpus_policy: Literal["default", "enabled", "disabled"] = "default",
    workspace_strict: bool = False,
    limit: int = 5,
    candidate_limit: int | None = None,
    broad_candidate_limit: int | None = None,
    display_limit: int | None = None,
    context_budget_chars: int | None = None,
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    min_score: float | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Codex-friendly alias for route-aware Palace memory recall."""
    return await retrieve_agent_memory(
        query=query,
        ctx=ctx,
        agent_scope_key=agent_scope_key,
        include_agent_scope_keys=include_agent_scope_keys,
        include_all_permitted_agent_scopes=include_all_permitted_agent_scopes,
        access_reason=access_reason,
        workspace_scope_keys=workspace_scope_keys,
        session_scope_key=session_scope_key,
        include_tenant_shared=include_tenant_shared,
        tenant_shared_policy=tenant_shared_policy,
        include_broad_corpus=include_broad_corpus,
        broad_corpus_policy=broad_corpus_policy,
        workspace_strict=workspace_strict,
        limit=limit,
        candidate_limit=candidate_limit,
        broad_candidate_limit=broad_candidate_limit,
        display_limit=display_limit,
        context_budget_chars=context_budget_chars,
        tags=tags,
        tags_mode=tags_mode,
        min_score=min_score,
        date_from=date_from,
        date_to=date_to,
    )


@mcp.tool()
async def palace_remember(
    title: str,
    body: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    source: str = "codex",
    created_at: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    scope_type: ScopeType = "agent",
    scope_key: str | None = "codex",
    source_url: str | None = None,
    created_by_role: str | None = "agent",
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    webhook_url: str | None = None,
    enable_ai_enrichment: bool = False,
    relationship_policy: str = "immediate",
) -> dict[str, Any]:
    """Codex-friendly alias for durable Palace memory write-back."""
    return await create_memory_entry(
        title=title,
        body=body,
        ctx=ctx,
        source=source,
        created_at=created_at,
        summary=summary,
        tags=tags,
        scope_type=scope_type,
        scope_key=scope_key,
        source_url=source_url,
        created_by_role=created_by_role,
        metadata=metadata,
        idempotency_key=idempotency_key,
        webhook_url=webhook_url,
        enable_ai_enrichment=enable_ai_enrichment,
        relationship_policy=relationship_policy,
    )


@mcp.tool()
async def palace_context(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    memory_scope_type: ScopeType = "agent",
    memory_scope_key: str | None = "codex",
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    limit: int = 10,
    cursor: str | None = None,
    wakeup_scope_type: WakeupBriefScopeType = "tenant",
    wakeup_scope_key: str | None = None,
) -> dict[str, Any]:
    """Load startup wake-up context plus recent scoped memory metadata."""
    wakeup_brief = await get_wakeup_brief(
        ctx=ctx,
        scope_type=wakeup_scope_type,
        scope_key=wakeup_scope_key,
    )
    recent_memory = await list_memory_entries(
        ctx=ctx,
        scope_type=memory_scope_type,
        scope_key=memory_scope_key,
        tags=tags,
        tags_mode=tags_mode,
        limit=limit,
        cursor=cursor,
    )
    return {
        "wakeup_brief": wakeup_brief,
        "recent_memory": recent_memory,
    }


@mcp.tool()
async def get_retrieval_doctor(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    agent_scope_key: str | None = None,
    workspace_scope_keys: list[str] | None = None,
    session_scope_key: str | None = None,
    include_tenant_shared: bool = True,
    include_broad_corpus: bool = False,
    candidate_limit: int = 10,
    broad_candidate_limit: int | None = None,
    display_limit: int = 5,
    context_budget_chars: int | None = None,
    sample_probes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a read-only redacted retrieval diagnostic report for remote Codex/Hermes debugging."""
    return await _run_mcp_operation(
        ctx,
        operation="get_retrieval_doctor",
        params=locals(),
        call=lambda: _runtime(ctx).api.get_retrieval_doctor(
            agent_scope_key=agent_scope_key,
            workspace_scope_keys=workspace_scope_keys,
            session_scope_key=session_scope_key,
            include_tenant_shared=include_tenant_shared,
            include_broad_corpus=include_broad_corpus,
            candidate_limit=candidate_limit,
            broad_candidate_limit=broad_candidate_limit,
            display_limit=display_limit,
            context_budget_chars=context_budget_chars,
            sample_probes=sample_probes,
        ),
    )


@mcp.tool()
async def search_items(
    query: str,
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    limit: int = 10,
    source_type: str | None = None,
    tags: list[str] | None = None,
    tags_mode: Literal["any", "all"] = "any",
    date_from: str | None = None,
    date_to: str | None = None,
    min_score: float | None = None,
) -> dict[str, Any]:
    """Search the shared Palace corpus across notes, docs, media, and feeds."""
    return await _run_mcp_operation(
        ctx,
        operation="search_items",
        params=locals(),
        call=lambda: _runtime(ctx).api.search_items(
            query=query,
            limit=limit,
            source_type=source_type,
            tags=tags,
            tags_mode=tags_mode,
            date_from=date_from,
            date_to=date_to,
            min_score=min_score,
        ),
    )


@mcp.tool()
async def list_tags(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    prefix: str | None = None,
) -> dict[str, Any]:
    """List known ready-item tags, optionally filtered by prefix."""
    return await _run_mcp_operation(
        ctx,
        operation="list_tags",
        params=locals(),
        call=lambda: _runtime(ctx).api.list_tags(prefix=prefix),
    )


@mcp.tool()
async def list_items(
    ctx: Context[ServerSession, SecondBrainMcpRuntime],
    page: int = 1,
    per_page: int = 20,
    source_type: str | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Browse non-failed items in the authenticated tenant's library."""
    return await _run_mcp_operation(
        ctx,
        operation="list_items",
        params=locals(),
        call=lambda: _runtime(ctx).api.list_items(
            page=page,
            per_page=per_page,
            source_type=source_type,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
        ),
    )


def _parse_args() -> argparse.Namespace:
    transport, _ = _env_value(PALACE_MCP_TRANSPORT_ENVS, "stdio")
    host, _ = _env_value(PALACE_MCP_HOST_ENVS, "127.0.0.1")
    path, _ = _env_value(PALACE_MCP_PATH_ENVS, "/mcp")
    parser = argparse.ArgumentParser(description="Run the Palace of Truth MCP adapter.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=transport,
        help="MCP transport to expose. Defaults to stdio.",
    )
    parser.add_argument(
        "--host",
        default=host,
        help="Host for streamable HTTP transport. Defaults to 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_port_from_env(PALACE_MCP_PORT_ENVS, 8765),
        help="Port for streamable HTTP transport. Defaults to 8765.",
    )
    parser.add_argument(
        "--path",
        default=path,
        help="Mount path for streamable HTTP transport. Defaults to /mcp.",
    )
    return parser.parse_args()


def _port_from_env(names: str | tuple[str, ...], default: int) -> int:
    env_names = (names,) if isinstance(names, str) else names
    raw_value, _ = _env_value(env_names)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError:
        # Kubernetes injects SERVICE_PORT-style env vars like tcp://host:port for
        # Services whose names overlap our own env names. Ignore those and keep the
        # intended listen port default unless the caller provides a numeric value.
        return default


def _csv_env(names: str | tuple[str, ...]) -> list[str]:
    env_names = (names,) if isinstance(names, str) else names
    raw_value, _ = _env_value(env_names, "")
    assert raw_value is not None
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _streamable_http_transport_security(host: str) -> TransportSecuritySettings:
    loopback_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    loopback_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

    allowed_hosts = _csv_env(PALACE_MCP_ALLOWED_HOSTS_ENVS)
    allowed_origins = _csv_env(PALACE_MCP_ALLOWED_ORIGINS_ENVS)
    if allowed_hosts or allowed_origins:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*loopback_hosts, *allowed_hosts],
            allowed_origins=[*loopback_origins, *allowed_origins],
        )

    if host in {"127.0.0.1", "localhost", "::1"}:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=loopback_hosts,
            allowed_origins=loopback_origins,
        )

    # External-facing deployments need a broader host policy unless the caller
    # explicitly configures an allowlist. The API key remains the trust boundary.
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


def main() -> None:
    args = _parse_args()
    if args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.settings.streamable_http_path = args.path
        mcp.settings.transport_security = _streamable_http_transport_security(args.host)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
