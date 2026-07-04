from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.job import JobProgressEventResponse
from app.schemas.retrieval_provenance import (
    RetrievalDerivedRawClass,
    RetrievalFreshnessClass,
    RetrievalSourceSupportState,
    RetrievalTrustClass,
)
from app.schemas.search import SearchResult
from app.services.retrieval_lenses import validate_retrieval_lens_name


PalaceSectionStatus = Literal["fresh", "stale", "indexing", "redirected"]
PalaceMembershipSource = Literal["auto", "pinned"]
PalaceRoomState = Literal["active", "redirected"]
PalaceRunStatus = Literal["queued", "routing", "snapshotting", "tunneling", "completed", "failed"]
SyncRunStatus = Literal["queued", "running", "completed", "failed"]
SyncSourceStatus = Literal["active", "disabled"]
SyncSourceKind = Literal["folder", "repo", "s3"]
RepoCredentialType = Literal["none", "github_pat", "deployment_github_pat", "ssh_key"]


def _not_blank(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def _normalize_extensions(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        stripped = value.strip().lower()
        if not stripped:
            continue
        if not stripped.startswith("."):
            stripped = f".{stripped}"
        normalized.append(stripped)
    return list(dict.fromkeys(normalized))


def _normalize_optional_string(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def _normalize_prefix(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().strip("/")
    return stripped or None


class PalaceStateBanner(BaseModel):
    kind: Literal["redirected", "conflict", "fallback", "stale", "indexing"]
    message: str
    detail: str | None = None


class PalaceSectionFreshness(BaseModel):
    status: PalaceSectionStatus
    generation: int
    target_generation: int
    message: str


class PalaceRoomSummary(BaseModel):
    id: uuid.UUID
    wing_id: uuid.UUID
    name: str
    stable_key: str
    state: PalaceRoomState
    item_count: int = 0
    summary: str | None = None
    membership_status: PalaceSectionFreshness
    snapshot_status: PalaceSectionFreshness
    tunnel_status: PalaceSectionFreshness
    redirect_room_id: uuid.UUID | None = None


class PalaceWingSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    room_count: int
    item_count: int
    rooms: list[PalaceRoomSummary] = Field(default_factory=list)


class PalaceRepresentativeItem(BaseModel):
    item_id: uuid.UUID
    title: str
    source_type: str
    summary: str | None = None
    membership_source: PalaceMembershipSource
    pinned: bool = False


class PalaceTunnelSummary(BaseModel):
    room_id: uuid.UUID
    room_name: str
    strength: float
    tunnel_type: str
    activation_count: int = 0
    stability: float = 1.0
    last_activated_at: datetime | None = None


class PalaceMembershipDetail(BaseModel):
    item_id: uuid.UUID
    title: str
    source_type: str
    summary: str | None = None
    membership_source: PalaceMembershipSource
    membership_kind: str
    pinned: bool = False


class PalaceRoomDetail(BaseModel):
    room: PalaceRoomSummary
    wing_name: str
    banner: PalaceStateBanner | None = None
    representative_items: list[PalaceRepresentativeItem] = Field(default_factory=list)
    tunnels: list[PalaceTunnelSummary] = Field(default_factory=list)
    memberships: list[PalaceMembershipDetail] = Field(default_factory=list)
    redirect_target: PalaceRoomSummary | None = None


class PalaceSourceChunkSummary(BaseModel):
    id: uuid.UUID
    chunk_index: int
    chunk_digest: str
    token_count: int | None = None
    preview: str


class PalaceSourceRecordSummary(BaseModel):
    id: uuid.UUID
    item_id: uuid.UUID
    source_kind: str
    source_uri: str | None = None
    source_version: str
    content_hash: str
    status: Literal["active", "stale", "failed", "deleted", "superseded"]
    failure_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunk_count: int
    chunks: list[PalaceSourceChunkSummary] = Field(default_factory=list)


class PalaceItemSourceSummary(BaseModel):
    tenant_id: str
    item_id: uuid.UUID
    source_records: list[PalaceSourceRecordSummary] = Field(default_factory=list)


class PalaceClaimSourceSupportSummary(BaseModel):
    id: uuid.UUID
    source_record_id: uuid.UUID
    source_chunk_id: uuid.UUID | None = None
    source_item_id: uuid.UUID
    source_record_status: Literal["active", "stale", "failed", "deleted", "superseded"]
    support_role: Literal["supports", "contradicts", "context", "derived_from"]
    status: Literal["current", "stale"]
    source_digest: str
    source_span: dict[str, Any] = Field(default_factory=dict)


class PalaceClaimSupportSummary(BaseModel):
    id: uuid.UUID
    claim_key: str
    claim_text: str
    claim_type: Literal["fact", "preference", "decision", "task_state", "summary", "classification", "relationship"]
    confidence: float
    status: Literal["draft", "active", "stale", "conflicted", "rejected", "superseded"]
    support_state: Literal[
        "source_backed",
        "weak_source_support",
        "stale_source",
        "source_missing",
        "conflicted",
        "not_authoritative",
        "generated_unpromoted",
    ]
    warning: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    sources: list[PalaceClaimSourceSupportSummary] = Field(default_factory=list)


class PalaceClaimSupportReport(BaseModel):
    tenant_id: str
    claims: list[PalaceClaimSupportSummary] = Field(default_factory=list)


class PalaceRoomUpdate(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _not_blank(value, "name")


class PalaceOverview(BaseModel):
    tenant_id: str
    dirty_generation: int
    indexed_generation: int
    backlog_generation: int
    active_palace_run: "PalaceRunSummary | None" = None
    latest_sync_runs: list["SyncRunSummary"] = Field(default_factory=list)
    state_banner: PalaceStateBanner | None = None
    wings: list[PalaceWingSummary] = Field(default_factory=list)


class PalaceRetrieveRequest(BaseModel):
    query: str
    room_id: uuid.UUID | None = None
    limit: int = Field(5, ge=1, le=50)
    candidate_limit: int | None = Field(None, ge=1, le=200)
    include_neighbor_chunks: bool = False
    neighbor_chunk_window: int = Field(1, ge=1, le=5)
    context_budget_chars: int | None = Field(None, ge=200, le=20000)
    include_derived_artifacts: bool = False
    retrieval_lens: str | None = None
    scope_type: Literal["session", "agent", "workspace", "tenant_shared"] = "tenant_shared"
    scope_key: str | None = None
    tags: list[str] | None = None
    tags_mode: Literal["any", "all"] = "any"
    min_score: float | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return _not_blank(value, "query")

    @field_validator("retrieval_lens")
    @classmethod
    def validate_retrieval_lens(cls, value: str | None) -> str | None:
        return validate_retrieval_lens_name(value)

    @field_validator("scope_key")
    @classmethod
    def validate_scope_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _not_blank(value, "scope_key")

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for tag in value:
            normalized.append(_not_blank(tag, "tags"))
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> "PalaceRetrieveRequest":
        if self.scope_type == "tenant_shared":
            if self.scope_key is not None:
                raise ValueError("tenant_shared scope must not include a key")
            return self
        if self.scope_key is None:
            raise ValueError(f"{self.scope_type} scope requires a key")
        return self


class PalaceTraceStep(BaseModel):
    title: str
    detail: str


class PalaceRankingTraceResult(BaseModel):
    rank: int
    item_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    source_type: str | None = Field(default=None, exclude_if=lambda value: value is None)
    artifact_provenance_type: str | None = Field(default=None, exclude_if=lambda value: value is None)
    artifact_provenance_label: str | None = Field(default=None, exclude_if=lambda value: value is None)
    derived_artifact_keys: list[str] = Field(default_factory=list)
    retrieved_scope_type: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_scope_key: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_scope_label: str | None = Field(default=None, exclude_if=lambda value: value is None)
    trust_class: RetrievalTrustClass | None = Field(default=None, exclude_if=lambda value: value is None)
    source_support_state: RetrievalSourceSupportState | None = Field(default=None, exclude_if=lambda value: value is None)
    freshness: RetrievalFreshnessClass | None = Field(default=None, exclude_if=lambda value: value is None)
    derived_raw_classification: RetrievalDerivedRawClass | None = Field(default=None, exclude_if=lambda value: value is None)
    source_publication_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    source_role: str | None = Field(default=None, exclude_if=lambda value: value is None)
    query_source_role: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reranker_score: float | None = Field(default=None, exclude_if=lambda value: value is None)
    reranker_bonus: float | None = Field(default=None, exclude_if=lambda value: value is None)
    reranker_provider: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reranker_reason: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieval_hint_score: float | None = Field(default=None, exclude_if=lambda value: value is None)
    relationship_graph_score: float | None = Field(default=None, exclude_if=lambda value: value is None)
    base_score: float | None = Field(default=None, exclude_if=lambda value: value is None)
    adjusted_score: float | None = Field(default=None, exclude_if=lambda value: value is None)
    adjustments: dict[str, float] = Field(default_factory=dict)


class PalaceRankingTrace(BaseModel):
    route: str
    retrieval_lens: str | None = None
    retrieval_lens_profile: dict[str, Any] | None = None
    ranking_features_version: int | None = None
    query_intent: str | None = None
    source_ranking_enabled: bool | None = None
    second_stage_reranker: dict[str, Any] = Field(default_factory=dict)
    ranking_feature_flags: dict[str, bool] = Field(default_factory=dict)
    display_limit: int | None = None
    candidate_limit: int | None = None
    candidate_count: int | None = None
    trust_class_counts: dict[str, int] = Field(default_factory=dict)
    source_support_counts: dict[str, int] = Field(default_factory=dict)
    freshness_counts: dict[str, int] = Field(default_factory=dict)
    derived_raw_counts: dict[str, int] = Field(default_factory=dict)
    reuse_metrics: dict[str, Any] = Field(default_factory=dict)
    result_count: int = 0
    routing: dict[str, Any] = Field(default_factory=dict)
    results: list[PalaceRankingTraceResult] = Field(default_factory=list)


class PalaceTunnelActivationTrace(BaseModel):
    source_room_id: uuid.UUID
    target_room_id: uuid.UUID
    tunnel_type: str
    strength: float
    activation_count: int
    stability: float
    last_activated_at: datetime | None = None


class PalaceRetrieveTrace(BaseModel):
    status_banner: PalaceStateBanner | None = None
    requested_scope_type: Literal["session", "agent", "workspace", "tenant_shared"] = "tenant_shared"
    requested_scope_key: str | None = None
    selected_wing: str | None = None
    candidate_rooms: list[str] = Field(default_factory=list)
    expanded_rooms: list[str] = Field(default_factory=list)
    route_score: float | None = None
    route_confidence: Literal["none", "low", "high"] = "none"
    route_abstain_reason: str | None = None
    route_candidate_count: int = 0
    route_room_candidate_count: int | None = None
    route_global_candidate_count: int | None = None
    global_merge_rescued_results: bool = False
    fallback_used: bool = False
    completeness_warning: str | None = None
    hint_report: dict | None = None
    retrieval_lens: str | None = None
    retrieval_lens_profile: dict[str, Any] | None = None
    search_ranking_trace: dict | None = None
    activated_tunnels: list[PalaceTunnelActivationTrace] = Field(default_factory=list)
    context_budget_chars: int | None = None
    context_budget_truncated: bool = False
    steps: list[PalaceTraceStep] = Field(default_factory=list)
    ranking_traces: list[PalaceRankingTrace] = Field(default_factory=list)


class PalaceRetrieveResponse(BaseModel):
    routed_room_id: uuid.UUID | None = None
    redirected_from_room_id: uuid.UUID | None = None
    trace: PalaceRetrieveTrace
    results: list[SearchResult]
    total: int


class PalaceMemoryJobScope(BaseModel):
    type: Literal["session", "agent", "workspace", "tenant_shared"] = "tenant_shared"
    key: str | None = None


class PalaceMemoryJobSummary(BaseModel):
    job_id: uuid.UUID
    title: str
    status: str
    scope: PalaceMemoryJobScope = Field(default_factory=PalaceMemoryJobScope)
    accepted_as: Literal["canonical", "legacy_artifact"] | None = None
    retriable: bool = False
    source: str | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    recent_progress_events: list["JobProgressEventResponse"] = Field(default_factory=list)


class PalaceMemoryHealthSummary(BaseModel):
    queued: int = 0
    processing: int = 0
    failed: int = 0
    retryable: int = 0
    recent_jobs: list[PalaceMemoryJobSummary] = Field(default_factory=list)


class PalaceWebhookJobSummary(BaseModel):
    job_id: uuid.UUID
    title: str
    job_type: str
    status: str
    terminal: bool = False
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class PalaceWebhookHealthSummary(BaseModel):
    configured: int = 0
    pending: int = 0
    terminal: int = 0
    failed_jobs: int = 0
    retryable_jobs: int = 0
    recent_jobs: list[PalaceWebhookJobSummary] = Field(default_factory=list)


class PalaceTemporalFactSummary(BaseModel):
    id: uuid.UUID
    source_item_id: uuid.UUID
    source_item_title: str
    subject: str
    predicate: str
    object_text: str
    confidence: float
    status: Literal["active", "superseded"]
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    extracted_at: datetime
    superseded_at: datetime | None = None


class PalaceFactRegistrySummary(BaseModel):
    active: int = 0
    superseded: int = 0
    distinct_sources: int = 0
    last_extracted_at: datetime | None = None
    recent_facts: list[PalaceTemporalFactSummary] = Field(default_factory=list)


class PalaceDiaryRollupStatus(BaseModel):
    title: str
    scope_type: Literal["session", "agent", "workspace"]
    scope_key: str | None = None
    day: date
    updated_at: datetime
    source_count: int = 0
    stale: bool = False


class PalaceDiaryRollupSummary(BaseModel):
    fresh: int = 0
    stale: int = 0
    expected_through_day: date | None = None
    last_refreshed_at: datetime | None = None
    recent_rollups: list[PalaceDiaryRollupStatus] = Field(default_factory=list)


class PalaceWakeupBriefStatus(BaseModel):
    title: str
    scope_type: Literal["tenant", "wing"]
    scope_key: str | None = None
    generation: int = 0
    updated_at: datetime
    room_count: int = 0
    diary_count: int = 0
    fact_count: int = 0
    stale: bool = False


class PalaceWakeupBriefSummary(BaseModel):
    fresh: int = 0
    stale: int = 0
    generated_for_day: date | None = None
    last_refreshed_at: datetime | None = None
    recent_briefs: list[PalaceWakeupBriefStatus] = Field(default_factory=list)


class PalaceArtifactSectionHealth(BaseModel):
    fresh: int = 0
    stale: int = 0


class PalaceRoomArtifactBlocker(BaseModel):
    room_id: uuid.UUID
    room_name: str
    room_stable_key: str
    wing_name: str | None = None
    membership_generation: int = 0
    closet_generation: int = 0
    snapshot_generation: int = 0
    tunnel_generation: int = 0


class PalaceRoomArtifactHealthSummary(BaseModel):
    target_generation: int = 0
    active_rooms: int = 0
    blocked_rooms: int = 0
    blocked_room_samples: list[PalaceRoomArtifactBlocker] = Field(default_factory=list)
    closets: PalaceArtifactSectionHealth = Field(default_factory=PalaceArtifactSectionHealth)
    snapshots: PalaceArtifactSectionHealth = Field(default_factory=PalaceArtifactSectionHealth)
    tunnels: PalaceArtifactSectionHealth = Field(default_factory=PalaceArtifactSectionHealth)


class PalaceConsolidationCandidate(BaseModel):
    room_id: uuid.UUID
    room_name: str
    room_stable_key: str
    candidate_room_id: uuid.UUID
    candidate_room_name: str
    candidate_stable_key: str
    wing_id: uuid.UUID
    wing_name: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    shared_tags: list[str] = Field(default_factory=list)
    shared_drawer_item_ids: list[uuid.UUID] = Field(default_factory=list)


class PalaceConsolidationSummary(BaseModel):
    candidate_count: int = 0
    candidates: list[PalaceConsolidationCandidate] = Field(default_factory=list)


class PalaceWorkerQueueMetrics(BaseModel):
    key: str
    label: str
    queue_name: str
    functions: list[str] = Field(default_factory=list)
    queued_depth: int = 0
    deferred_depth: int = 0
    oldest_queued_age_seconds: int | None = None
    worker_concurrency: int | None = None
    worker_queue_depth: int | None = None
    db_queued_depth: int | None = None
    db_processing_depth: int | None = None
    oldest_db_queued_age_seconds: int | None = None
    queued_tenant_count: int | None = None
    processing_tenant_count: int | None = None
    max_queued_per_tenant: int | None = None
    max_processing_per_tenant: int | None = None
    recent_completed: int = 0
    recent_failed: int = 0
    recent_timeout_count: int = 0
    recent_avg_latency_seconds: float | None = None
    unexpected_function_count: int = 0
    unexpected_functions: list[str] = Field(default_factory=list)
    tenant_pressure: list[dict] = Field(default_factory=list)
    telemetry_error: str | None = None


class PalaceWorkerBackpressureSummary(BaseModel):
    generated_at: datetime
    queues: list[PalaceWorkerQueueMetrics] = Field(default_factory=list)


class PalaceMcpActivityEvent(BaseModel):
    id: uuid.UUID
    client_name: str
    client_key: str
    operation: str
    required_scope: str | None = None
    status: str
    latency_ms: int | None = None
    error_class: str | None = None
    params_summary: dict = Field(default_factory=dict)
    created_at: datetime


class PalaceMcpActivitySummary(BaseModel):
    registered_clients: int = 0
    recent_success: int = 0
    recent_error: int = 0
    recent_denied: int = 0
    recent_events: list[PalaceMcpActivityEvent] = Field(default_factory=list)


class PalaceControlTower(BaseModel):
    tenant_id: str
    dirty_generation: int
    indexed_generation: int
    backlog_generation: int
    active_palace_run: "PalaceRunSummary | None" = None
    room_artifacts: PalaceRoomArtifactHealthSummary = Field(default_factory=PalaceRoomArtifactHealthSummary)
    consolidation: PalaceConsolidationSummary = Field(default_factory=PalaceConsolidationSummary)
    worker_backpressure: PalaceWorkerBackpressureSummary | None = None
    mcp_activity: PalaceMcpActivitySummary = Field(default_factory=PalaceMcpActivitySummary)
    memory_health: PalaceMemoryHealthSummary = Field(default_factory=PalaceMemoryHealthSummary)
    webhook_health: PalaceWebhookHealthSummary = Field(default_factory=PalaceWebhookHealthSummary)
    fact_registry: PalaceFactRegistrySummary = Field(default_factory=PalaceFactRegistrySummary)
    diary_rollups: PalaceDiaryRollupSummary = Field(default_factory=PalaceDiaryRollupSummary)
    wakeup_briefs: PalaceWakeupBriefSummary = Field(default_factory=PalaceWakeupBriefSummary)
    sync_sources: list["SyncSourceSummary"] = Field(default_factory=list)
    sync_runs: list["SyncRunSummary"] = Field(default_factory=list)
    palace_runs: list["PalaceRunSummary"] = Field(default_factory=list)


class SyncSourceCreate(BaseModel):
    name: str
    root_path: str | None = None
    source_kind: SyncSourceKind = "folder"
    credential_type: RepoCredentialType = "none"
    github_pat: str | None = None
    ssh_private_key: str | None = None
    scan_interval_seconds: int = Field(900, ge=300, le=86400)
    allowed_extensions: list[str] = Field(default_factory=list)
    bucket: str | None = None
    prefix: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    force_path_style: bool = False

    @field_validator("name")
    @classmethod
    def validate_required_strings(cls, value: str, info) -> str:
        return _not_blank(value, info.field_name)

    @field_validator("root_path", "bucket", "endpoint_url", "region")
    @classmethod
    def validate_optional_strings(cls, value: str | None, info) -> str | None:
        return _normalize_optional_string(value, info.field_name)

    @field_validator("github_pat", "ssh_private_key")
    @classmethod
    def validate_optional_secrets(cls, value: str | None, info) -> str | None:
        return _normalize_optional_string(value, info.field_name)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str | None) -> str | None:
        return _normalize_prefix(value)

    @field_validator("allowed_extensions")
    @classmethod
    def validate_allowed_extensions(cls, value: list[str]) -> list[str]:
        return _normalize_extensions(value)

    @model_validator(mode="after")
    def validate_source_shape(self) -> "SyncSourceCreate":
        if self.source_kind == "s3":
            if self.bucket is None:
                raise ValueError("bucket must be provided for s3 sync sources")
            if self.root_path is not None:
                raise ValueError("root_path is only valid for folder or repo sync sources")
            if self.credential_type != "none" or self.github_pat is not None or self.ssh_private_key is not None:
                raise ValueError("repo credentials are only valid for repo sync sources")
            return self

        if self.root_path is None:
            raise ValueError("root_path must be provided for folder or repo sync sources")
        if self.bucket is not None or self.prefix is not None or self.endpoint_url is not None or self.region is not None:
            raise ValueError("bucket, prefix, endpoint_url, and region are only valid for s3 sync sources")
        if self.source_kind == "folder":
            if self.credential_type != "none" or self.github_pat is not None or self.ssh_private_key is not None:
                raise ValueError("repo credentials are only valid for repo sync sources")
        else:
            if self.credential_type == "github_pat" and self.github_pat is None:
                raise ValueError("github_pat must be provided when credential_type is github_pat")
            if self.credential_type == "ssh_key" and self.ssh_private_key is None:
                raise ValueError("ssh_private_key must be provided when credential_type is ssh_key")
            if self.credential_type != "github_pat" and self.github_pat is not None:
                raise ValueError("github_pat is only valid when credential_type is github_pat")
            if self.credential_type != "ssh_key" and self.ssh_private_key is not None:
                raise ValueError("ssh_private_key is only valid when credential_type is ssh_key")
        if self.force_path_style:
            raise ValueError("force_path_style is only valid for s3 sync sources")
        return self


class SyncSourceUpdate(BaseModel):
    name: str | None = None
    root_path: str | None = None
    source_kind: SyncSourceKind | None = None
    credential_type: RepoCredentialType | None = None
    github_pat: str | None = None
    ssh_private_key: str | None = None
    clear_stored_credential: bool = False
    scan_interval_seconds: int | None = Field(None, ge=300, le=86400)
    allowed_extensions: list[str] | None = None
    bucket: str | None = None
    prefix: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    force_path_style: bool | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _not_blank(value, "name")

    @field_validator("root_path", "bucket", "endpoint_url", "region")
    @classmethod
    def validate_optional_strings(cls, value: str | None, info) -> str | None:
        return _normalize_optional_string(value, info.field_name)

    @field_validator("github_pat", "ssh_private_key")
    @classmethod
    def validate_optional_secrets(cls, value: str | None, info) -> str | None:
        return _normalize_optional_string(value, info.field_name)

    @field_validator("prefix")
    @classmethod
    def validate_prefix(cls, value: str | None) -> str | None:
        return _normalize_prefix(value)

    @field_validator("allowed_extensions")
    @classmethod
    def validate_allowed_extensions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _normalize_extensions(value)


class SyncSourceSummary(BaseModel):
    id: uuid.UUID
    name: str
    root_path: str
    source_kind: SyncSourceKind
    credential_type: RepoCredentialType = "none"
    has_stored_credential: bool = False
    status: SyncSourceStatus
    disabled_at: datetime | None = None
    disabled_reason: str | None = None
    scan_interval_seconds: int
    allowed_extensions: list[str] = Field(default_factory=list)
    bucket: str | None = None
    prefix: str | None = None
    endpoint_url: str | None = None
    region: str | None = None
    force_path_style: bool = False
    last_synced_at: datetime | None = None
    last_error: str | None = None


class SyncSourceDeleteResponse(BaseModel):
    deleted: bool
    items_deactivated: int = 0
    sync_source_id: uuid.UUID | None = None
    sync_source_name: str | None = None
    status: SyncSourceStatus = "disabled"


class SyncRunSummary(BaseModel):
    id: uuid.UUID
    sync_source_id: uuid.UUID
    sync_source_name: str
    status: SyncRunStatus
    triggered_by: str
    files_seen: int
    files_changed: int
    files_skipped: int
    items_created: int
    items_updated: int
    items_failed: int
    generation: int
    error_message: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class PalaceRunSummary(BaseModel):
    id: uuid.UUID
    status: PalaceRunStatus
    triggered_by: str
    requested_generation: int
    applied_generation: int
    attempt: int
    error_message: str | None = None
    started_at: datetime
    completed_at: datetime | None = None


class PalacePinRequest(BaseModel):
    item_id: uuid.UUID


PalaceOverview.model_rebuild()
PalaceControlTower.model_rebuild()
