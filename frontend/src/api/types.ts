export interface Item {
  id: string;
  title: string;
  source_type: string;
  source_url: string | null;
  summary: string | null;
  tags: string[];
  categories: string[];
  status: string;
  raw_content: string | null;
  created_at: string;
  deleted_at?: string | null;
  metadata?: Record<string, unknown>;
  metadata_?: Record<string, unknown>;
}

export interface ItemListResponse {
  items: Item[];
  total: number;
  page: number;
  per_page: number;
}

export type WebSaveCaptureKind = "webpage" | "social_post" | "media" | "selection_note";

export interface WebSaveItemSummary {
  id: string;
  title: string;
  source_type: string;
  status: string;
  summary: string | null;
  tags: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface WebSave {
  id: string;
  item_id: string;
  original_url: string;
  normalized_url: string;
  source_title: string | null;
  source_domain: string | null;
  capture_kind: WebSaveCaptureKind;
  user_tags: string[];
  saved_at: string;
  archived_at: string | null;
  extension_version: string | null;
  metadata: Record<string, unknown>;
  item: WebSaveItemSummary;
}

export interface WebSaveListResponse {
  web_saves: WebSave[];
  total: number;
  page: number;
  per_page: number;
}

export interface JobStatus {
  id?: string;
  job_id: string;
  status: string;
  progress: number;
  error: string | null;
  error_message?: string | null;
  item_id: string | null;
  duplicate_of: string | null;
  recent_progress_events?: JobProgressEvent[];
}

export interface SearchResult {
  item_id: string;
  title: string;
  source_type: string;
  source_url?: string | null;
  score: number;
  summary: string | null;
  chunk_text: string;
  tags?: string[];
  system_tags?: string[];
  semantic_tags?: string[];
  artifact_citation?: ArtifactCitation | null;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
}

export type MemoryScopeType = "session" | "agent" | "workspace" | "tenant_shared";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatSource {
  item_id: string;
  title: string;
  source_type?: string;
  source_url?: string | null;
  chunk_text: string;
  artifact_citation?: ArtifactCitation | null;
}

export interface ArtifactCitation {
  kind: string;
  thumbnail_url?: string | null;
  caption?: string | null;
  extracted_text?: string[];
  source_url?: string | null;
  source_label?: string | null;
  original_artifact_url?: string | null;
  original_artifact_label?: string | null;
  filename?: string | null;
  media_type?: string | null;
  dimensions?: { width?: number | null; height?: number | null } | null;
  model?: string | null;
  provider?: string | null;
  confidence?: number | null;
  byte_hash?: string | null;
}

export interface ChatResponse {
  response: string;
  sources: ChatSource[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ConversationMessage {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  messages: ConversationMessage[];
}

export interface GraphNode {
  id: string;
  title: string;
  source_type: string;
  summary?: string | null;
  tags?: string[];
}

export interface GraphEdge {
  source: string;
  target: string;
  relationship: string;
  confidence: number;
}

export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  meta?: {
    orphaned_ready_items: number;
  };
}

export interface RelatedItem {
  item_id: string;
  title: string;
  source_type: string;
  relationship: string;
  confidence: number;
}

export interface RelatedItemsResponse {
  relationships: RelatedItem[];
}

export interface StatsResponse {
  total_items: number;
  ready_items: number;
  by_source_type: Record<string, number>;
  indexed_items: number;
  embedding_chunks: number;
  total_embeddings: number;
  orphaned_ready_items: number;
  active_jobs: number;
  feed_count?: number;
}

export interface Feed {
  id: string;
  url: string;
  name: string | null;
  auto_tags: string[];
  poll_interval: number;
  enabled: boolean;
  paused_reason: string | null;
  last_fetched_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  feed_metadata: {
    feed_title?: string;
    site_url?: string;
    description?: string;
  };
  item_count: number;
  deleted_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface FeedListResponse {
  feeds: Feed[];
  total: number;
}

export interface FeedItemsResponse {
  items: Item[];
  total: number;
}

export interface OPMLImportResponse {
  created: number;
  skipped: number;
  feeds: Feed[];
}

export type SourceSubscriptionStatus = "active" | "paused" | "deleted";
export type SourceSubscriptionEntryStatus = "discovered" | "queued" | "captured" | "skipped" | "failed";

export interface SourceSubscription {
  id: string;
  tenant_id: string;
  provider_type: "youtube_channel";
  source_url: string;
  external_id: string | null;
  external_url: string | null;
  display_name: string | null;
  status: SourceSubscriptionStatus;
  auto_tags: string[];
  poll_interval_seconds: number;
  cursor: Record<string, unknown>;
  provider_metadata: Record<string, unknown>;
  last_checked_at: string | null;
  last_discovered_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  paused_reason: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceSubscriptionPreview {
  provider_type: "youtube_channel";
  source_url: string;
  external_id: string;
  external_url: string | null;
  display_name: string | null;
  provider_metadata: Record<string, unknown>;
  no_backfill: boolean;
  backfill_enabled: boolean;
  backfill_limit: number | null;
  backfill_published_after: string | null;
}

export interface SourceSubscriptionListResponse {
  subscriptions: SourceSubscription[];
  total: number;
}

export interface SourceSubscriptionEntry {
  id: string;
  tenant_id: string;
  subscription_id: string;
  provider_entry_id: string | null;
  source_url: string | null;
  title: string | null;
  published_at: string | null;
  discovered_at: string;
  status: SourceSubscriptionEntryStatus;
  skip_reason: string | null;
  error_message: string | null;
  item_id: string | null;
  job_id: string | null;
  queued_at: string | null;
  captured_at: string | null;
  skipped_at: string | null;
  failed_at: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface SourceSubscriptionEntryListResponse {
  entries: SourceSubscriptionEntry[];
  total: number;
}

export interface PalaceStateBanner {
  kind: "redirected" | "conflict" | "fallback" | "stale" | "indexing";
  message: string;
  detail?: string | null;
}

export interface PalaceSectionFreshness {
  status: "fresh" | "stale" | "indexing" | "redirected";
  generation: number;
  target_generation: number;
  message: string;
}

export interface PalaceRoomSummary {
  id: string;
  wing_id: string;
  name: string;
  stable_key: string;
  state: "active" | "redirected";
  item_count: number;
  summary: string | null;
  membership_status: PalaceSectionFreshness;
  snapshot_status: PalaceSectionFreshness;
  tunnel_status: PalaceSectionFreshness;
  redirect_room_id: string | null;
}

export interface PalaceWingSummary {
  id: string;
  slug: string;
  name: string;
  room_count: number;
  item_count: number;
  rooms: PalaceRoomSummary[];
}

export interface PalaceRunSummary {
  id: string;
  status: "queued" | "routing" | "snapshotting" | "tunneling" | "completed" | "failed";
  triggered_by: string;
  requested_generation: number;
  applied_generation: number;
  attempt: number;
  error_message?: string | null;
  started_at: string;
  completed_at?: string | null;
}

export interface PalaceSyncSource {
  id: string;
  name: string;
  root_path: string;
  source_kind: "folder" | "repo" | "s3";
  credential_type: "none" | "github_pat" | "deployment_github_pat" | "ssh_key";
  has_stored_credential: boolean;
  status: "active" | "disabled";
  disabled_at?: string | null;
  disabled_reason?: string | null;
  scan_interval_seconds: number;
  allowed_extensions?: string[];
  bucket?: string | null;
  prefix?: string | null;
  endpoint_url?: string | null;
  region?: string | null;
  force_path_style?: boolean;
  last_synced_at?: string | null;
  last_error?: string | null;
}

export interface PalaceSyncRun {
  id: string;
  sync_source_id: string;
  sync_source_name: string;
  status: "queued" | "running" | "completed" | "failed";
  triggered_by: string;
  files_seen: number;
  files_changed: number;
  files_skipped: number;
  items_created: number;
  items_updated: number;
  items_failed: number;
  generation: number;
  error_message?: string | null;
  started_at: string;
  completed_at?: string | null;
}

export interface PalaceSyncSourceDeleteResponse {
  deleted: boolean;
  items_deactivated: number;
  sync_source_id?: string | null;
  sync_source_name?: string | null;
  status: "active" | "disabled";
}

export interface PalaceOverview {
  tenant_id: string;
  dirty_generation: number;
  indexed_generation: number;
  backlog_generation: number;
  active_palace_run?: PalaceRunSummary | null;
  latest_sync_runs: PalaceSyncRun[];
  state_banner?: PalaceStateBanner | null;
  wings: PalaceWingSummary[];
}

export interface PalaceRepresentativeItem {
  item_id: string;
  title: string;
  source_type: string;
  summary?: string | null;
  membership_source: "auto" | "pinned";
  pinned: boolean;
}

export interface PalaceTunnelSummary {
  room_id: string;
  room_name: string;
  strength: number;
  tunnel_type: string;
}

export interface PalaceMembershipDetail {
  item_id: string;
  title: string;
  source_type: string;
  summary?: string | null;
  membership_source: "auto" | "pinned";
  membership_kind: string;
  pinned: boolean;
}

export interface PalaceRoomDetail {
  room: PalaceRoomSummary;
  wing_name: string;
  banner?: PalaceStateBanner | null;
  representative_items: PalaceRepresentativeItem[];
  tunnels: PalaceTunnelSummary[];
  memberships: PalaceMembershipDetail[];
  redirect_target?: PalaceRoomSummary | null;
}

export interface PalaceTraceStep {
  title: string;
  detail: string;
}

export interface PalaceRankingTraceResult {
  rank: number;
  item_id?: string | null;
  source_type?: string | null;
  artifact_provenance_type?: string | null;
  artifact_provenance_label?: string | null;
  derived_artifact_keys?: string[];
  retrieved_scope_type?: string | null;
  retrieved_scope_key?: string | null;
  retrieved_scope_label?: string | null;
  base_score?: number | null;
  adjusted_score?: number | null;
  adjustments?: Record<string, number>;
}

export interface PalaceRankingTrace {
  route: string;
  query_intent?: string | null;
  candidate_count?: number | null;
  result_count: number;
  routing: Record<string, unknown>;
  results: PalaceRankingTraceResult[];
}

export interface PalaceRetrieveTrace {
  status_banner?: PalaceStateBanner | null;
  requested_scope_type: MemoryScopeType;
  requested_scope_key?: string | null;
  selected_wing?: string | null;
  candidate_rooms: string[];
  expanded_rooms: string[];
  fallback_used: boolean;
  completeness_warning?: string | null;
  steps: PalaceTraceStep[];
  ranking_traces: PalaceRankingTrace[];
}

export interface PalaceRetrieveResponse {
  routed_room_id?: string | null;
  redirected_from_room_id?: string | null;
  trace: PalaceRetrieveTrace;
  results: SearchResult[];
  total: number;
}

export interface PalaceMemoryJobScope {
  type: MemoryScopeType;
  key?: string | null;
}

export interface PalaceMemoryJobSummary {
  job_id: string;
  title: string;
  status: string;
  scope: PalaceMemoryJobScope;
  accepted_as?: "canonical" | "legacy_artifact" | null;
  retriable: boolean;
  source?: string | null;
  error_message?: string | null;
  created_at: string;
  completed_at?: string | null;
  recent_progress_events: JobProgressEvent[];
}

export interface JobProgressEvent {
  phase: string;
  status: string;
  progress?: number | null;
  message?: string | null;
  metadata_?: Record<string, unknown> | null;
  created_at: string;
}

export interface PalaceMemoryHealthSummary {
  queued: number;
  processing: number;
  failed: number;
  retryable: number;
  recent_jobs: PalaceMemoryJobSummary[];
}

export interface PalaceWebhookJobSummary {
  job_id: string;
  title: string;
  job_type: string;
  status: string;
  terminal: boolean;
  error_message?: string | null;
  created_at: string;
  completed_at?: string | null;
}

export interface PalaceWebhookHealthSummary {
  configured: number;
  pending: number;
  terminal: number;
  failed_jobs: number;
  retryable_jobs: number;
  recent_jobs: PalaceWebhookJobSummary[];
}

export interface PalaceTemporalFactSummary {
  id: string;
  source_item_id: string;
  source_item_title: string;
  subject: string;
  predicate: string;
  object_text: string;
  confidence: number;
  status: "active" | "superseded";
  valid_from?: string | null;
  valid_to?: string | null;
  extracted_at: string;
  superseded_at?: string | null;
}

export interface PalaceFactRegistrySummary {
  active: number;
  superseded: number;
  distinct_sources: number;
  last_extracted_at?: string | null;
  recent_facts: PalaceTemporalFactSummary[];
}

export interface PalaceDiaryRollupStatus {
  title: string;
  scope_type: "session" | "agent" | "workspace";
  scope_key?: string | null;
  day: string;
  updated_at: string;
  source_count: number;
  stale: boolean;
}

export interface PalaceDiaryRollupSummary {
  fresh: number;
  stale: number;
  expected_through_day?: string | null;
  last_refreshed_at?: string | null;
  recent_rollups: PalaceDiaryRollupStatus[];
}

export interface PalaceWakeupBriefStatus {
  title: string;
  scope_type: "tenant" | "wing";
  scope_key?: string | null;
  generation: number;
  updated_at: string;
  room_count: number;
  diary_count: number;
  fact_count: number;
  stale: boolean;
}

export interface PalaceWakeupBriefSummary {
  fresh: number;
  stale: number;
  generated_for_day?: string | null;
  last_refreshed_at?: string | null;
  recent_briefs: PalaceWakeupBriefStatus[];
}

export interface PalaceArtifactSectionHealth {
  fresh: number;
  stale: number;
}

export interface PalaceRoomArtifactBlocker {
  room_id: string;
  room_name: string;
  room_stable_key: string;
  wing_name?: string | null;
  membership_generation: number;
  closet_generation: number;
  snapshot_generation: number;
  tunnel_generation: number;
}

export interface PalaceRoomArtifactHealthSummary {
  target_generation: number;
  active_rooms: number;
  blocked_rooms: number;
  blocked_room_samples: PalaceRoomArtifactBlocker[];
  closets: PalaceArtifactSectionHealth;
  snapshots: PalaceArtifactSectionHealth;
  tunnels: PalaceArtifactSectionHealth;
}

export interface PalaceConsolidationCandidate {
  room_id: string;
  room_name: string;
  room_stable_key: string;
  candidate_room_id: string;
  candidate_room_name: string;
  candidate_stable_key: string;
  wing_id: string;
  wing_name: string;
  score: number;
  reasons: string[];
  shared_tags: string[];
  shared_drawer_item_ids: string[];
}

export interface PalaceConsolidationSummary {
  candidate_count: number;
  candidates: PalaceConsolidationCandidate[];
}

export interface PalaceWorkerQueueMetrics {
  key: string;
  label: string;
  queue_name: string;
  functions: string[];
  queued_depth: number;
  deferred_depth: number;
  oldest_queued_age_seconds?: number | null;
  worker_concurrency?: number | null;
  worker_queue_depth?: number | null;
  db_queued_depth?: number | null;
  db_processing_depth?: number | null;
  oldest_db_queued_age_seconds?: number | null;
  queued_tenant_count?: number | null;
  processing_tenant_count?: number | null;
  max_queued_per_tenant?: number | null;
  max_processing_per_tenant?: number | null;
  recent_completed: number;
  recent_failed: number;
  recent_timeout_count: number;
  recent_avg_latency_seconds?: number | null;
  unexpected_function_count: number;
  unexpected_functions: string[];
  tenant_pressure: Array<{
    rank: number;
    queued_depth: number;
    processing_depth: number;
    oldest_queued_age_seconds?: number | null;
    recent_failed: number;
    recent_timeout_count: number;
  }>;
  telemetry_error?: string | null;
}

export interface PalaceWorkerBackpressureSummary {
  generated_at: string;
  queues: PalaceWorkerQueueMetrics[];
}

export interface PalaceMcpActivityEvent {
  id: string;
  client_name: string;
  client_key: string;
  operation: string;
  required_scope?: string | null;
  status: string;
  latency_ms?: number | null;
  error_class?: string | null;
  params_summary: Record<string, unknown>;
  created_at: string;
}

export interface PalaceMcpActivitySummary {
  registered_clients: number;
  recent_success: number;
  recent_error: number;
  recent_denied: number;
  recent_events: PalaceMcpActivityEvent[];
}

export type McpOperationScope = "read" | "write" | "admin" | "local_only" | "destructive_prohibited";

export interface McpOAuthClientSummary {
  id: string;
  tenant_id: string;
  client_key: string;
  display_name: string;
  allowed_scopes: McpOperationScope[];
  metadata: Record<string, unknown>;
  token_ttl_seconds: number;
  created_at?: string | null;
  last_seen_at?: string | null;
  request_count: number;
  success_count: number;
  denied_count: number;
  error_count: number;
  last_request_at?: string | null;
  revoked_at?: string | null;
}

export interface McpClientConfigSnippets {
  codex_stdio_toml: string;
  http_oauth_toml: string;
  oauth_token_command: string;
  legacy_api_key_toml: string;
  secret_handling_note: string;
}

export interface McpOAuthClientListResponse {
  tenant_id: string;
  clients: McpOAuthClientSummary[];
  config_snippets: McpClientConfigSnippets;
}

export interface McpOAuthClientRegisterResponse {
  tenant_id: string;
  client: McpOAuthClientSummary;
  client_secret: string;
  config_snippets?: McpClientConfigSnippets | null;
}

export type CandidateArtifactStatus =
  | "draft"
  | "needs_source"
  | "reviewable"
  | "promoted"
  | "proposed"
  | "approved"
  | "rejected"
  | "stale"
  | "deprecated"
  | "superseded";

export interface CandidateCurationArtifact {
  id: string;
  tenant_id: string;
  artifact_kind: string;
  target_runtime: string;
  target_surface: string;
  status: CandidateArtifactStatus;
  source_item_ids: string[];
  source_digests: Record<string, string>;
  candidate_body: string;
  privacy_review: Record<string, unknown>;
  eval_summary: Record<string, unknown>;
  approval: Record<string, unknown>;
  metadata: Record<string, unknown>;
  promotion_state: string;
  source_support_level: string;
  advisory_generated_context: boolean;
  promoted_source_backed: boolean;
  supersedes_artifact_id: string | null;
  superseded_by_artifact_id: string | null;
  deprecated_reason: string | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
  deprecated_at: string | null;
}

export type ReviewInboxAction = "accept" | "reject" | "pin" | "defer";

export interface ReviewInboxItem {
  artifact: CandidateCurationArtifact;
  suggested_action: string;
  confidence: number | null;
  source_count: number;
  freshness: "fresh" | "stale" | "conflicting" | "needs_source";
  affected_scope: string;
  pinned: boolean;
  deferred: boolean;
  reversible_actions: string[];
}

export interface ReviewInboxSummary {
  total: number;
  needs_source: number;
  conflicting: number;
  stale: number;
  pinned: number;
  deferred: number;
}

export interface ReviewInboxResponse {
  items: ReviewInboxItem[];
  summary: ReviewInboxSummary;
}

export interface ReviewInboxActionResponse {
  action: ReviewInboxAction;
  artifacts: CandidateCurationArtifact[];
  updated: number;
}

export interface McpOAuthClientRevokeResponse {
  tenant_id: string;
  client: McpOAuthClientSummary;
  revoked: boolean;
}

export interface MemoryJobResponse {
  job_id: string;
  status: string;
  error_message?: string | null;
  duplicate_of?: string | null;
  completed_at?: string | null;
}

export interface PalaceControlTower {
  tenant_id: string;
  dirty_generation: number;
  indexed_generation: number;
  backlog_generation: number;
  active_palace_run?: PalaceRunSummary | null;
  room_artifacts: PalaceRoomArtifactHealthSummary;
  consolidation: PalaceConsolidationSummary;
  worker_backpressure?: PalaceWorkerBackpressureSummary | null;
  mcp_activity: PalaceMcpActivitySummary;
  memory_health: PalaceMemoryHealthSummary;
  webhook_health: PalaceWebhookHealthSummary;
  fact_registry: PalaceFactRegistrySummary;
  diary_rollups: PalaceDiaryRollupSummary;
  wakeup_briefs: PalaceWakeupBriefSummary;
  sync_sources: PalaceSyncSource[];
  sync_runs: PalaceSyncRun[];
  palace_runs: PalaceRunSummary[];
}
