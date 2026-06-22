from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.palace import PalaceRetrieveTrace
from app.schemas.search import SearchResult
from app.services.retrieval_lenses import validate_retrieval_lens_name


MemoryKind = Literal["task_retrospective", "content_approval", "founder_note"]
MemoryScopeType = Literal["session", "agent", "workspace", "tenant_shared"]
MemoryWakeupBriefScopeType = Literal["tenant", "wing"]
RelationshipExtractionPolicy = Literal["immediate", "deferred", "skip"]
TagsMode = Literal["any", "all"]
AgentMemoryTenantSharedPolicy = Literal["always", "fallback_only", "never"]
AgentMemoryBroadCorpusPolicy = Literal["default", "enabled", "disabled"]
DelegatedAgentMemoryDecision = Literal["not_requested", "allowed", "partial", "denied"]
MemoryWriteContractStatus = Literal[
    "accepted",
    "queued",
    "processing",
    "completed",
    "retryable_degraded",
    "rejected",
    "quarantined",
    "permanent_tenant_mismatch",
    "dependency_unavailable",
]
MemoryQueueState = Literal["healthy", "backpressure", "saturated", "unknown"]
McpOperationScope = Literal[
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "admin",
    "local_only",
    "destructive_prohibited",
    "capture:write",
    "capture:job:read",
]
McpAuditStatus = Literal["success", "error", "denied"]

MEMORY_JOB_TYPE = "memory_artifact"
MEMORY_IDEMPOTENCY_KEY_MAX_LENGTH = 64
MEMORY_JOB_PUBLIC_STATUS_MAP = {
    "queued": "queued",
    "processing": "processing",
    "completed": "complete",
    "duplicate": "duplicate",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _validate_not_blank(value: str, field_name: str) -> str:
    stripped = value.strip()
    if stripped == "":
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def _clean_tags(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    cleaned: list[str] = []
    for tag in value:
        stripped = tag.strip()
        if not stripped:
            raise ValueError("tags must not contain blank values")
        cleaned.append(stripped)
    return cleaned


class MemoryWhoAmIResponse(BaseModel):
    status: Literal["ok"] = "ok"
    tenant_id: str


class McpClientInfo(BaseModel):
    client_key: str
    display_name: str
    allowed_scopes: list[McpOperationScope] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("client_key", "display_name")
    @classmethod
    def required_strings_not_blank(cls, value: str, info) -> str:
        return _validate_not_blank(value, info.field_name)


class McpRequestAuditRequest(BaseModel):
    client: McpClientInfo
    operation: str
    required_scope: McpOperationScope | None = None
    params_summary: dict[str, Any] = Field(default_factory=dict)
    status: McpAuditStatus
    latency_ms: int | None = Field(None, ge=0)
    error_class: str | None = None
    app_version: str | None = None

    @field_validator("operation")
    @classmethod
    def operation_not_blank(cls, value: str) -> str:
        return _validate_not_blank(value, "operation")

    @field_validator("error_class", "app_version")
    @classmethod
    def optional_strings_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)


class McpRequestAuditResponse(BaseModel):
    audit_event_id: uuid.UUID
    client_id: uuid.UUID
    tenant_id: str
    status: Literal["recorded"] = "recorded"


class McpOAuthClientRegisterRequest(BaseModel):
    client_key: str
    display_name: str
    allowed_scopes: list[McpOperationScope] = Field(default_factory=lambda: ["read"])
    metadata: dict[str, Any] = Field(default_factory=dict)
    token_ttl_seconds: int = Field(3600, ge=60, le=86400)

    @field_validator("client_key", "display_name")
    @classmethod
    def required_strings_not_blank(cls, value: str, info) -> str:
        return _validate_not_blank(value, info.field_name)


class McpOAuthClientSummary(BaseModel):
    id: uuid.UUID
    tenant_id: str
    client_key: str
    display_name: str
    allowed_scopes: list[McpOperationScope]
    metadata: dict[str, Any] = Field(default_factory=dict)
    token_ttl_seconds: int
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    request_count: int = 0
    success_count: int = 0
    denied_count: int = 0
    error_count: int = 0
    last_request_at: datetime | None = None
    revoked_at: datetime | None = None


class McpClientConfigSnippets(BaseModel):
    codex_stdio_toml: str
    http_oauth_toml: str
    oauth_token_command: str
    legacy_api_key_toml: str
    secret_handling_note: str


class McpOAuthClientRegisterResponse(BaseModel):
    tenant_id: str
    client: McpOAuthClientSummary
    client_secret: str
    config_snippets: McpClientConfigSnippets | None = None


class McpOAuthClientRevokeResponse(BaseModel):
    tenant_id: str
    client: McpOAuthClientSummary
    revoked: bool = True


class McpOAuthClientListResponse(BaseModel):
    tenant_id: str
    clients: list[McpOAuthClientSummary]
    config_snippets: McpClientConfigSnippets


class McpOAuthTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    scope: str


class McpOAuthRevokeResponse(BaseModel):
    revoked: bool = True


class BrowserExtensionTokenIssueRequest(BaseModel):
    display_name: str = "Palace Capture Extension"
    extension_version: str | None = None
    token_ttl_seconds: int = Field(2592000, ge=3600, le=7776000)

    @field_validator("display_name", "extension_version")
    @classmethod
    def optional_strings_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)


class BrowserExtensionTokenIssueResponse(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    scope: str
    tenant_id: str
    client_key: str
    expires_at: datetime


class McpOAuthProtectedResourceMetadata(BaseModel):
    resource: str
    authorization_servers: list[str]
    bearer_methods_supported: list[str] = Field(default_factory=lambda: ["header"])
    scopes_supported: list[McpOperationScope]


class MemoryScope(BaseModel):
    type: MemoryScopeType = "tenant_shared"
    key: str | None = None

    @field_validator("key")
    @classmethod
    def key_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, "scope.key")

    @model_validator(mode="after")
    def validate_scope_shape(self) -> "MemoryScope":
        if self.type == "tenant_shared":
            if self.key is not None:
                raise ValueError("tenant_shared scope must not include a key")
            return self
        if self.key is None:
            raise ValueError(f"{self.type} scope requires a key")
        return self


class MemoryEntryRequest(BaseModel):
    tenant_id: str
    title: str
    body: str
    summary: str | None = None
    source: str
    created_at: datetime
    tags: list[str] = Field(default_factory=list)
    scope: MemoryScope = Field(default_factory=MemoryScope)
    source_url: str | None = None
    created_by_role: str | None = None
    metadata: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=MEMORY_IDEMPOTENCY_KEY_MAX_LENGTH)
    webhook_url: str | None = None
    enable_ai_enrichment: bool = False
    relationship_policy: RelationshipExtractionPolicy = "immediate"

    @field_validator("tenant_id", "title", "body", "source")
    @classmethod
    def required_strings_not_blank(cls, value: str, info) -> str:
        return _validate_not_blank(value, info.field_name)

    @field_validator("summary", "source_url", "created_by_role", "idempotency_key", "webhook_url")
    @classmethod
    def optional_strings_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)

    @field_validator("tags")
    @classmethod
    def tags_must_not_be_blank(cls, value: list[str]) -> list[str]:
        return _clean_tags(value) or []


class LegacyMemoryArtifactRequest(BaseModel):
    tenant_id: str
    company_id: str
    memory_kind: MemoryKind
    title: str
    summary: str
    body: str
    tags: list[str] = Field(default_factory=list)
    created_by_role: str
    source: str
    created_at: datetime
    project_id: str | None = None
    ticket_id: str | None = None
    task_id: str | None = None
    outcome: str | None = None
    review_status: str | None = None
    repo_ref: str | None = None
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    webhook_url: str | None = None
    enable_ai_enrichment: bool = False
    relationship_policy: RelationshipExtractionPolicy = "immediate"

    @field_validator(
        "tenant_id",
        "company_id",
        "title",
        "summary",
        "body",
        "created_by_role",
        "source",
    )
    @classmethod
    def required_strings_not_blank(cls, value: str, info) -> str:
        return _validate_not_blank(value, info.field_name)

    @field_validator(
        "project_id",
        "ticket_id",
        "task_id",
        "outcome",
        "review_status",
        "repo_ref",
        "webhook_url",
    )
    @classmethod
    def optional_strings_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)

    @field_validator("tags")
    @classmethod
    def tags_must_not_be_blank(cls, value: list[str]) -> list[str]:
        return _clean_tags(value) or []

    @model_validator(mode="after")
    def validate_kind_specific_requirements(self) -> "LegacyMemoryArtifactRequest":
        if self.memory_kind == "task_retrospective" and not self.task_id:
            raise ValueError("task_retrospective requires task_id")
        if self.memory_kind == "content_approval" and not self.ticket_id:
            raise ValueError("content_approval requires ticket_id")
        if self.memory_kind == "task_retrospective" and self.task_id and self.task_id not in self.body:
            raise ValueError("body must include task_id")
        if self.memory_kind == "content_approval" and self.ticket_id and self.ticket_id not in self.body:
            raise ValueError("body must include ticket_id")
        return self


class MemoryQueueHint(BaseModel):
    state: MemoryQueueState
    queue_name: str | None = None
    queued_depth: int | None = None
    deferred_depth: int | None = None
    worker_queue_depth: int | None = None
    oldest_queued_age_seconds: int | None = None
    retry_after_seconds: int | None = None
    poll_after_seconds: int = 5
    rate_limit_state: Literal["not_enforced"] = "not_enforced"
    telemetry_error: str | None = None


class MemoryArtifactAcceptedResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    contract_status: MemoryWriteContractStatus = "accepted"
    scope: MemoryScope | None = None
    accepted_as: Literal["canonical", "legacy_artifact"] | None = None
    poll_url: str | None = None
    poll_after_seconds: int = 5
    retryable: bool = False
    retry_after_seconds: int | None = None
    rate_limit_state: Literal["not_enforced"] = "not_enforced"
    queue: MemoryQueueHint | None = None


class MemoryEntryBatchRequest(BaseModel):
    entries: list[MemoryEntryRequest] = Field(..., min_length=1, max_length=100)


class MemoryEntryBatchResult(BaseModel):
    index: int
    status: str
    contract_status: MemoryWriteContractStatus
    retryable: bool = False
    job_id: uuid.UUID | None = None
    poll_url: str | None = None
    poll_after_seconds: int | None = None
    retry_after_seconds: int | None = None
    accepted_as: Literal["canonical", "legacy_artifact"] | None = None
    scope: MemoryScope | None = None
    error: dict[str, Any] | None = None


class MemoryEntryBatchResponse(BaseModel):
    status: Literal["accepted", "partial", "failed"]
    accepted: int
    failed: int
    max_entries: int = 100
    poll_after_seconds: int = 5
    retryable: bool = False
    retry_after_seconds: int | None = None
    rate_limit_state: Literal["not_enforced"] = "not_enforced"
    queue: MemoryQueueHint | None = None
    results: list[MemoryEntryBatchResult]


class RelationshipBackfillRequest(BaseModel):
    limit: int = Field(50, ge=1, le=500)
    defer_seconds: int = Field(15, ge=0, le=3600)


class RelationshipBackfillAcceptedResponse(BaseModel):
    status: Literal["queued", "active"] = "queued"
    tenant_id: str
    limit: int
    defer_seconds: int
    lease_key: str | None = None
    lease_holder: str | None = None


class MemoryJobResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    contract_status: MemoryWriteContractStatus
    error_message: str | None = None
    duplicate_of: uuid.UUID | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    poll_after_seconds: int = 5
    retryable: bool = False
    retry_after_seconds: int | None = None


class MemoryJobListResponse(BaseModel):
    jobs: list[MemoryJobResponse]
    total: int


class MemoryEntryListItem(BaseModel):
    source_item_id: uuid.UUID
    title: str
    summary: str | None = None
    source: str | None = None
    source_url: str | None = None
    scope: MemoryScope
    tags: list[str] = Field(default_factory=list)
    system_tags: list[str] = Field(default_factory=list)
    semantic_tags: list[str] = Field(default_factory=list)
    source_project: str | None = Field(default=None, exclude_if=lambda value: value is None)
    created_at: datetime
    updated_at: datetime
    readiness_state: str
    job_id: uuid.UUID | None = None
    job_status: str | None = None


class MemoryEntryListResponse(BaseModel):
    entries: list[MemoryEntryListItem]
    total: int
    limit: int
    next_cursor: datetime | None = None


class MemoryScopeSummary(BaseModel):
    scope: MemoryScope
    entry_count: int
    latest_created_at: datetime | None = None
    latest_updated_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class MemoryScopeListResponse(BaseModel):
    scopes: list[MemoryScopeSummary]
    total: int
    limit: int


class MemoryRetrieveRequest(BaseModel):
    query: str
    limit: int = Field(5, ge=1, le=50)
    candidate_limit: int | None = Field(None, ge=1, le=200)
    include_neighbor_chunks: bool = False
    neighbor_chunk_window: int = Field(1, ge=1, le=5)
    context_budget_chars: int | None = Field(None, ge=200, le=20000)
    include_derived_artifacts: bool = False
    retrieval_lens: str | None = None
    tags: list[str] | None = None
    tags_mode: TagsMode = "any"
    min_score: float | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    scope: MemoryScope = Field(default_factory=MemoryScope)
    room_id: uuid.UUID | None = None

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        return _validate_not_blank(value, "query")

    @field_validator("retrieval_lens")
    @classmethod
    def validate_retrieval_lens(cls, value: str | None) -> str | None:
        return validate_retrieval_lens_name(value)

    @field_validator("tags")
    @classmethod
    def retrieve_tags_not_blank(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)


class MemoryRetrieveResponse(BaseModel):
    scope: MemoryScope
    routed_room_id: uuid.UUID | None = None
    redirected_from_room_id: uuid.UUID | None = None
    trace: PalaceRetrieveTrace
    results: list[SearchResult]
    total: int


class AgentMemoryRetrieveRequest(BaseModel):
    query: str
    agent_scope_key: str | None = None
    include_agent_scope_keys: list[str] = Field(default_factory=list)
    include_all_permitted_agent_scopes: bool = False
    access_reason: str | None = None
    workspace_scope_keys: list[str] = Field(default_factory=list)
    session_scope_key: str | None = None
    include_tenant_shared: bool = True
    tenant_shared_policy: AgentMemoryTenantSharedPolicy = "always"
    include_broad_corpus: bool = True
    broad_corpus_policy: AgentMemoryBroadCorpusPolicy = "default"
    workspace_strict: bool = False
    limit: int = Field(5, ge=1, le=50)
    candidate_limit: int | None = Field(None, ge=1, le=50)
    broad_candidate_limit: int | None = Field(None, ge=1, le=50)
    display_limit: int | None = Field(None, ge=1, le=50)
    context_budget_chars: int | None = Field(None, ge=200, le=20000)
    include_derived_artifacts: bool = False
    retrieval_lens: str | None = None
    tags: list[str] | None = None
    tags_mode: TagsMode = "any"
    min_score: float | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    @field_validator("query")
    @classmethod
    def agent_query_not_blank(cls, value: str) -> str:
        return _validate_not_blank(value, "query")

    @field_validator("agent_scope_key", "session_scope_key")
    @classmethod
    def optional_scope_key_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)

    @field_validator("include_agent_scope_keys", "workspace_scope_keys")
    @classmethod
    def scope_key_lists_not_blank(cls, value: list[str], info) -> list[str]:
        cleaned: list[str] = []
        for key in value:
            stripped = _validate_not_blank(key, info.field_name)
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned

    @field_validator("access_reason")
    @classmethod
    def access_reason_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, "access_reason")

    @field_validator("tags")
    @classmethod
    def agent_retrieve_tags_not_blank(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)

    @field_validator("retrieval_lens")
    @classmethod
    def agent_retrieval_lens_valid(cls, value: str | None) -> str | None:
        return validate_retrieval_lens_name(value)

    @model_validator(mode="after")
    def validate_workspace_strict_scope(self) -> "AgentMemoryRetrieveRequest":
        if self.workspace_strict and not self.workspace_scope_keys:
            raise ValueError("workspace_strict requires at least one workspace_scope_key")
        return self


class AgentMemoryRetrieveTrace(BaseModel):
    searched_scopes: list[MemoryScope] = Field(default_factory=list)
    caller_agent_scope_key: str | None = None
    requested_agent_scope_keys: list[str] = Field(default_factory=list)
    authorized_agent_scope_keys: list[str] = Field(default_factory=list)
    denied_agent_scope_keys: list[str] = Field(default_factory=list)
    delegated_agent_policy_id: str | None = None
    delegated_agent_policy_source: str | None = None
    delegated_agent_decision: DelegatedAgentMemoryDecision = "not_requested"
    delegated_agent_deny_reasons: list[str] = Field(default_factory=list)
    access_reason_required: bool = False
    access_reason_present: bool = False
    result_counts_by_scope: dict[str, int] = Field(default_factory=dict)
    workspace_strict: bool = False
    workspace_scope_exhausted: bool = False
    tenant_shared_policy: AgentMemoryTenantSharedPolicy = "always"
    tenant_shared_fallback_used: bool = False
    broad_corpus_policy: AgentMemoryBroadCorpusPolicy = "default"
    broad_corpus_searched: bool = False
    broad_corpus_skipped_reason: str | None = None
    excluded_scope_types: list[MemoryScopeType] = Field(default_factory=list)
    selected_scope_candidate_limit: int = 5
    broad_candidate_limit: int = 5
    display_limit: int = 5
    context_budget_chars: int | None = None
    query_embedding_reused: bool = False
    selected_scope_query_count: int = 0
    selected_scope_result_count: int = 0
    selected_scope_fallback_used: bool = False
    selected_scope_completeness_warnings: list[str] = Field(default_factory=list)
    broad_result_count: int = 0
    deduped_result_count: int = 0
    selected_scope_duration_ms: int | None = None
    broad_corpus_duration_ms: int | None = None
    merge_duration_ms: int | None = None
    total_duration_ms: int | None = None
    budget_truncated: bool = False
    context_budget_truncated: bool = False
    fallback_used: bool = False
    completeness_warnings: list[str] = Field(default_factory=list)


class AgentMemoryRetrieveResponse(BaseModel):
    scopes: list[MemoryScope] = Field(default_factory=list)
    trace: AgentMemoryRetrieveTrace
    results: list[SearchResult]
    total: int


TrajectoryEntryStatus = Literal["current", "stale"]


class MemoryTrajectoryRequest(BaseModel):
    query: str
    trajectory_subject: str | None = None
    agent_scope_key: str | None = None
    include_agent_scope_keys: list[str] = Field(default_factory=list)
    include_all_permitted_agent_scopes: bool = False
    access_reason: str | None = None
    workspace_scope_keys: list[str] = Field(default_factory=list)
    session_scope_key: str | None = None
    include_tenant_shared: bool = True
    tenant_shared_policy: AgentMemoryTenantSharedPolicy = "always"
    include_broad_corpus: bool = False
    broad_corpus_policy: AgentMemoryBroadCorpusPolicy = "disabled"
    workspace_strict: bool = False
    limit: int = Field(10, ge=1, le=50)
    candidate_limit: int | None = Field(None, ge=1, le=50)
    display_limit: int | None = Field(None, ge=1, le=50)
    context_budget_chars: int | None = Field(None, ge=200, le=20000)
    tags: list[str] | None = None
    tags_mode: TagsMode = "any"
    min_score: float | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    @field_validator("query")
    @classmethod
    def trajectory_query_not_blank(cls, value: str) -> str:
        return _validate_not_blank(value, "query")

    @field_validator("trajectory_subject", "agent_scope_key", "session_scope_key")
    @classmethod
    def optional_trajectory_strings_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)

    @field_validator("include_agent_scope_keys", "workspace_scope_keys")
    @classmethod
    def trajectory_scope_key_lists_not_blank(cls, value: list[str], info) -> list[str]:
        cleaned: list[str] = []
        for key in value:
            stripped = _validate_not_blank(key, info.field_name)
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned

    @field_validator("access_reason")
    @classmethod
    def trajectory_access_reason_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, "access_reason")

    @field_validator("tags")
    @classmethod
    def trajectory_tags_not_blank(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)

    @model_validator(mode="after")
    def validate_trajectory_workspace_strict_scope(self) -> "MemoryTrajectoryRequest":
        if self.workspace_strict and not self.workspace_scope_keys:
            raise ValueError("workspace_strict requires at least one workspace_scope_key")
        return self


class MemoryTrajectoryEntry(BaseModel):
    item_id: uuid.UUID
    title: str
    subject: str | None = None
    predicate: str | None = None
    object_text: str
    trajectory_key: str
    status: TrajectoryEntryStatus
    event_time: datetime
    source_item_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    source_span: dict | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_scope_label: str | None = Field(default=None, exclude_if=lambda value: value is None)
    score: float


class MemoryTrajectoryResponse(BaseModel):
    query: str
    trajectory_subject: str | None = None
    scopes: list[MemoryScope] = Field(default_factory=list)
    trace: AgentMemoryRetrieveTrace
    entries: list[MemoryTrajectoryEntry]
    current_entries: list[MemoryTrajectoryEntry] = Field(default_factory=list)
    total: int


class MemoryWakeupBriefResponse(BaseModel):
    source_item_id: uuid.UUID
    title: str
    summary: str | None = None
    body: str
    source_url: str | None = None
    day: str
    scope_type: MemoryWakeupBriefScopeType
    scope_key: str | None = None
    generation: int
    indexed_generation: int
    freshness: Literal["fresh", "stale"]
    stale: bool
    room_count: int
    diary_count: int
    fact_count: int
    updated_at: datetime


DoctorStatus = Literal["ok", "degraded", "unhealthy"]


class MemoryRetrievalDoctorProbe(BaseModel):
    query: str
    scope: MemoryScope = Field(default_factory=MemoryScope)
    tags: list[str] | None = None
    tags_mode: TagsMode = "any"
    limit: int = Field(5, ge=1, le=20)
    expected_item_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("query")
    @classmethod
    def doctor_query_not_blank(cls, value: str) -> str:
        return _validate_not_blank(value, "query")

    @field_validator("tags")
    @classmethod
    def doctor_tags_not_blank(cls, value: list[str] | None) -> list[str] | None:
        return _clean_tags(value)


class MemoryRetrievalDoctorRequest(BaseModel):
    agent_scope_key: str | None = None
    workspace_scope_keys: list[str] = Field(default_factory=list)
    session_scope_key: str | None = None
    include_tenant_shared: bool = True
    include_broad_corpus: bool = False
    candidate_limit: int = Field(10, ge=1, le=50)
    broad_candidate_limit: int | None = Field(None, ge=1, le=50)
    display_limit: int = Field(5, ge=1, le=20)
    context_budget_chars: int | None = Field(None, ge=200, le=20000)
    sample_probes: list[MemoryRetrievalDoctorProbe] = Field(default_factory=list, max_length=5)

    @field_validator("agent_scope_key", "session_scope_key")
    @classmethod
    def doctor_optional_scope_key_not_blank(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_not_blank(value, info.field_name)

    @field_validator("workspace_scope_keys")
    @classmethod
    def doctor_workspace_scope_keys_not_blank(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for key in value:
            stripped = _validate_not_blank(key, "workspace_scope_keys")
            if stripped not in cleaned:
                cleaned.append(stripped)
        return cleaned


class MemoryRetrievalDoctorAuthShape(BaseModel):
    auth_mode: str | None = None
    mcp_client_key: str | None = None
    allowed_scopes: list[str] = Field(default_factory=list)


class MemoryRetrievalDoctorCheck(BaseModel):
    name: str
    status: DoctorStatus
    reasons: list[str] = Field(default_factory=list)


class MemoryRetrievalDoctorGeneration(BaseModel):
    dirty_generation: int = 0
    indexed_generation: int = 0
    backlog_generation: int = 0


class MemoryRetrievalDoctorRelationshipState(BaseModel):
    relationship_edges: int = 0
    ready_items_without_relationships: int = 0
    deferred_memory_candidates: int = 0


class MemoryRetrievalDoctorWakeupState(BaseModel):
    fresh: int = 0
    stale: int = 0
    generated_for_day: str | None = None
    last_refreshed_at: datetime | None = None


class MemoryRetrievalDoctorProbeTopResult(BaseModel):
    rank: int
    item_id: uuid.UUID
    source_type: str
    score: float
    tags: list[str] = Field(default_factory=list)
    expected_match: bool = False


class MemoryRetrievalDoctorRankingRoute(BaseModel):
    route: str
    candidate_limit: int | None = None
    candidate_count: int | None = None
    result_count: int = 0
    source_ranking_enabled: bool | None = None
    fallback_used: bool | None = None
    global_merge_rescued_results: bool | None = None


class MemoryRetrievalDoctorProbeReport(BaseModel):
    probe_index: int
    query_fingerprint: str
    scope: MemoryScope
    tags: list[str] | None = None
    status: DoctorStatus
    reasons: list[str] = Field(default_factory=list)
    route_confidence: str = "none"
    route_score: float | None = None
    route_candidate_count: int = 0
    route_room_candidate_count: int | None = None
    route_global_candidate_count: int | None = None
    fallback_used: bool = False
    global_merge_rescued_results: bool = False
    selected_scope_result_count: int = 0
    broad_result_count: int = 0
    deduped_result_count: int = 0
    budget_truncated: bool = False
    ranking_routes: list[MemoryRetrievalDoctorRankingRoute] = Field(default_factory=list)
    top_results: list[MemoryRetrievalDoctorProbeTopResult] = Field(default_factory=list)
    expected_top_rank: int | None = None


class MemoryRetrievalDoctorResponse(BaseModel):
    status: DoctorStatus
    tenant_id: str
    auth: MemoryRetrievalDoctorAuthShape = Field(default_factory=MemoryRetrievalDoctorAuthShape)
    selected_scopes: list[MemoryScope] = Field(default_factory=list)
    generation: MemoryRetrievalDoctorGeneration = Field(default_factory=MemoryRetrievalDoctorGeneration)
    queue_health: Any | None = None
    wakeup_briefs: MemoryRetrievalDoctorWakeupState = Field(default_factory=MemoryRetrievalDoctorWakeupState)
    relationships: MemoryRetrievalDoctorRelationshipState = Field(default_factory=MemoryRetrievalDoctorRelationshipState)
    probes: list[MemoryRetrievalDoctorProbeReport] = Field(default_factory=list)
    checks: list[MemoryRetrievalDoctorCheck] = Field(default_factory=list)
