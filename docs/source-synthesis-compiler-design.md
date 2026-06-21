# Source Synthesis Compiler Model Design

Task: SAR-833

## Goal

Make Palace's output-backward knowledge pipeline explicit. Raw captures should remain durable source material, while chunks, extracted claims, generated synthesis, and operator-promoted artifacts become first-class compiler outputs with source dependency tracking and invalidation rules.

This design is an implementation proposal, not a migration in this PR.

## Current Model

Palace already has the pieces of the pipeline, but they are split across general-purpose rows and metadata:

- `items` stores raw captures, title, summary, `raw_content`, JSON `content_chunks`, source URL, tags, status, soft deletion, and tenant scope.
- `embeddings` and `embedding_profile_vectors` duplicate chunk text by `item_id` and `chunk_index` for retrieval.
- Canonical memory entries are normalized into `items.metadata.memory_entry`, with scope, source, source URL, idempotency, and client metadata.
- `temporal_facts` stores extracted subject/predicate/object records with `source_item_id`, `source_fingerprint`, status, and validity windows.
- Palace room artifacts, retrieval hints, and dirty-item generation state track room-level derived outputs.
- `candidate_curation_artifacts` stores generated review candidates with source item ids, source digests, privacy review, approval gates, and lifecycle status.
- `sync_sources`, `sync_runs`, and `sync_source_files` track ingest origins and changed files, but they do not define a source/chunk/claim/synthesis compiler contract.

The gap is that a generated answer, claim, room digest, or promoted artifact cannot consistently answer: which source version produced me, which chunks were selected, which claims I depend on, and what should happen if a source is edited, deleted, failed, or superseded.

## Proposed Concepts

### Source Records

`source_records` are immutable-ish source-version records derived from `items`, not replacements for `items`.

Candidate fields:

- `id uuid primary key`
- `tenant_id text not null`
- `item_id uuid not null references items(id) on delete cascade`
- `source_kind text not null`: `capture`, `memory_entry`, `sync_file`, `web_save`, `feed_entry`, `media_transcript`, `repo_file`, `generated_artifact`
- `source_uri text null`: URL, file URI, object key, or synthetic `memory://...`
- `source_version text not null`: stable digest or version label for the source body and source metadata used by compilers
- `content_hash text not null`
- `status text not null`: `active`, `stale`, `failed`, `deleted`, `superseded`
- `failure_reason text null`
- `metadata jsonb not null default '{}'`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

Indexes:

- unique `(tenant_id, item_id, source_version)`
- lookup `(tenant_id, status, source_kind)`
- lookup `(tenant_id, source_uri)` where `source_uri is not null`

Mapping:

- Existing `items` rows produce the first `source_records` row from `source_type`, `source_url`, `content_hash`, `status`, `deleted_at`, and `metadata`.
- `sync_source_files` should link to the source row by `item_id` and source fingerprint when available.
- Failed items still get source records when they have enough source identity to explain failed processing; their status is `failed` and they should not feed downstream compiler runs.

### Source Chunks

`source_chunks` materialize the current JSON `items.content_chunks` contract into addressable rows. Embedding rows can keep their vector storage, but chunk identity should not depend on embedding tables.

Candidate fields:

- `id uuid primary key`
- `tenant_id text not null`
- `source_record_id uuid not null references source_records(id) on delete cascade`
- `item_id uuid not null references items(id) on delete cascade`
- `chunk_index integer not null`
- `chunk_text text not null`
- `chunk_digest text not null`
- `span jsonb not null default '{}'`: line, page, paragraph, timestamp, or byte-span data when known
- `token_count integer null`
- `created_at timestamptz not null default now()`

Indexes:

- unique `(tenant_id, source_record_id, chunk_index)`
- lookup `(tenant_id, item_id, chunk_index)`
- unique `(tenant_id, source_record_id, chunk_digest)`

Mapping:

- `items.content_chunks[*].index` and `text` backfill directly.
- `embeddings.chunk_text` remains a retrieval optimization and should validate against `source_chunks.chunk_digest` during backfill and rebuild checks.
- Future media and document extractors can populate richer `span` metadata without changing retrieval APIs.

### Claims

`claims` are normalized propositions or reusable statements extracted from source chunks. They are lower-level than promoted memory entries and higher-level than raw chunks.

Candidate fields:

- `id uuid primary key`
- `tenant_id text not null`
- `claim_key text not null`: deterministic key for idempotency within tenant and source scope
- `claim_text text not null`
- `claim_type text not null`: `fact`, `preference`, `decision`, `task_state`, `summary`, `classification`, `relationship`
- `confidence double precision not null default 1.0`
- `status text not null`: `draft`, `active`, `stale`, `conflicted`, `rejected`, `superseded`
- `superseded_by_claim_id uuid null references claims(id) on delete set null`
- `metadata jsonb not null default '{}'`
- `created_at timestamptz not null default now()`
- `updated_at timestamptz not null default now()`

`claim_sources` connect claims to source evidence:

- `id uuid primary key`
- `tenant_id text not null`
- `claim_id uuid not null references claims(id) on delete cascade`
- `source_record_id uuid not null references source_records(id) on delete cascade`
- `source_chunk_id uuid null references source_chunks(id) on delete set null`
- `support_role text not null`: `supports`, `contradicts`, `context`, `derived_from`
- `source_digest text not null`
- `source_span jsonb not null default '{}'`
- `created_at timestamptz not null default now()`

Indexes:

- unique `(tenant_id, claim_key)`
- lookup `(tenant_id, status, claim_type)`
- lookup `(tenant_id, claim_id, support_role)`
- lookup `(tenant_id, source_record_id)`

Mapping:

- `temporal_facts` map to `claims` with `claim_type='fact'`; `source_fingerprint` becomes a `claim_sources.source_digest`.
- Conversation facts and memory-entry derived facts can use `claim_type='decision'`, `task_state`, or `preference` where appropriate.
- Graph relationships may either stay in relationship tables and expose source support through `claim_sources`, or later migrate high-confidence edges into `claims` with `claim_type='relationship'`.

### Synthesis Runs

`synthesis_runs` describe compiler invocations. They are the build logs for generated artifacts.

Candidate fields:

- `id uuid primary key`
- `tenant_id text not null`
- `compiler_name text not null`: `diary_rollup`, `memory_dream`, `room_snapshot`, `retrieval_hint`, `review_candidate`, `answer_summary`, `research_artifact`
- `compiler_version text not null`
- `input_policy jsonb not null default '{}'`
- `output_kind text not null`
- `status text not null`: `queued`, `running`, `completed`, `failed`, `canceled`
- `error_message text null`
- `started_at timestamptz null`
- `completed_at timestamptz null`
- `created_at timestamptz not null default now()`

`artifact_dependencies` connect generated outputs to exact inputs:

- `id uuid primary key`
- `tenant_id text not null`
- `synthesis_run_id uuid not null references synthesis_runs(id) on delete cascade`
- `artifact_table text not null`
- `artifact_id uuid not null`
- `dependency_type text not null`: `source_record`, `source_chunk`, `claim`, `artifact`
- `dependency_id uuid not null`
- `dependency_digest text not null`
- `required_freshness text not null default 'fresh'`
- `created_at timestamptz not null default now()`

Indexes:

- lookup `(tenant_id, compiler_name, status, created_at desc)`
- unique `(tenant_id, artifact_table, artifact_id, dependency_type, dependency_id)`
- lookup `(tenant_id, dependency_type, dependency_id)`

Mapping:

- Diary rollups, memory dreams, room snapshots, retrieval hints, candidate curation artifacts, and future research artifacts become generated outputs with a `synthesis_run_id` pointer, either directly as a nullable column or indirectly through `artifact_dependencies`.
- Candidate curation `source_item_ids` and `source_digests` become a compatibility projection of `artifact_dependencies` until the UI and APIs are migrated.

## Lifecycle Semantics

### Raw Captures

Raw captures are source of record. Editing a capture creates a new `source_records.source_version` and new `source_chunks`; it does not mutate old dependency rows. Soft-deleting an item marks current source records `deleted` and invalidates dependent generated outputs. Production deletion remains a human-only operation.

### Canonical Memory

Canonical memory is operator- or agent-submitted durable memory normalized through `/api/v1/memory/entries`. It remains retrievable as an item, but its source record has `source_kind='memory_entry'` and scope metadata copied from `items.metadata.memory_entry`.

Canonical memory can depend on raw source chunks through `claim_sources` or `artifact_dependencies`, but it should not be silently rewritten when the raw source changes. Instead, downstream diagnostics should flag stale source support and ask for re-promotion.

### Generated Synthesis

Generated synthesis is compiler output. It may be useful for retrieval and review, but it is not canonical unless promoted. Generated rows must carry dependency digests and become `stale` when any required dependency digest changes, disappears, or enters a failed/deleted state.

### Operator-Promoted Artifacts

Operator-promoted artifacts are generated outputs that passed review. Promotion should copy the reviewed body and source support into durable memory or the relevant promoted artifact table while preserving dependencies. If a promoted artifact's source becomes stale or conflicting, the artifact stays visible but gets a stale-source warning rather than being deleted.

## Invalidation Policy

Use a report-first invalidation pass before automatic repair:

1. Source edit: create a new `source_record` version, rebuild chunks, mark old-version dependent artifacts `stale_source`.
2. Source soft delete: mark source record `deleted`, mark dependent generated artifacts `stale`, and mark promoted artifacts `source_deleted_warning`.
3. Source processing failure: mark source record `failed`, leave prior successful source version intact if present, and block new synthesis from the failed version.
4. Chunker/compiler version change: create new chunks or synthesis run outputs with the new version; compare dependency digests before replacing any reviewable artifact.
5. Contradictory claim: add `claim_sources.support_role='contradicts'` and set affected claims or artifacts to `conflicted` until reviewed.
6. Missing dependency: keep the artifact row for audit, exclude it from strong-evidence retrieval, and expose the missing dependency in diagnostics.

Initial invalidation should reuse existing Palace dirty generation concepts: source edits enqueue an invalidation job, then a compiler status endpoint reports stale artifacts before any rebuild or promotion.

## API Shape

Private/internal endpoints first:

- `GET /api/v1/palace/sources/{item_id}`: source records, chunks summary, current source version, and processing status.
- `GET /api/v1/palace/artifacts/{artifact_id}/dependencies`: dependency graph for a generated or promoted artifact.
- `POST /api/v1/palace/compiler-runs`: start a named compiler for a bounded input set.
- `GET /api/v1/palace/compiler-runs/{run_id}`: run status, errors, output ids, and dependency counts.
- `POST /api/v1/palace/invalidation/report`: dry-run report for changed source ids, returning affected artifacts and recommended actions.

Public/retrieval surfaces should add fields only after the internal contract is stable:

- Search and memory retrieval diagnostics can expose `dependency_state`, `source_record_id`, `source_chunk_id`, and `synthesis_run_id`.
- Review Inbox items can display exact dependency freshness rather than only source item ids and digests.
- MCP compact context can summarize generated synthesis as strong, stale, conflicting, or source-missing.

## Migration And Backfill Strategy

1. Add tables without changing ingestion behavior.
2. Backfill `source_records` from non-deleted and deleted `items`, using `content_hash` when present and a computed digest of `raw_content` plus source metadata otherwise.
3. Backfill `source_chunks` from `items.content_chunks`; validate representative rows against `embeddings` chunk text.
4. Backfill `claims` from `temporal_facts` only, because those already have explicit source fingerprints and status.
5. Backfill `artifact_dependencies` from `candidate_curation_artifacts.source_item_ids/source_digests` and `retrieval_hint_artifacts.source_item_id/source_chunk_index/source_fingerprint`.
6. Keep existing JSON fields and source arrays as compatibility projections while services start reading the normalized tables.
7. Add compiler-run rows to new synthesis jobs first; migrate older diary, dream, and room artifacts after the read APIs can tolerate missing run ids.
8. Only after backfill and diagnostics are stable, consider moving `items.content_chunks` to a cache of `source_chunks` instead of the primary chunk source.

Backfills should be tenant-bounded, resumable, dry-run capable, and safe to rerun. They should never hard-delete existing source, artifact, or memory rows.

## Test Plan

- Migration tests assert constraints, indexes, cascade behavior, and downgrade policy.
- Backfill unit tests cover items with raw content, missing content hashes, failed items, soft-deleted items, empty chunks, sync files, memory entries, temporal facts, curation artifacts, and retrieval hints.
- Service tests cover source edit, soft delete, failed processing, contradiction, stale dependency reporting, and compatibility projection updates.
- API tests cover source/dependency reads, dry-run invalidation reports, tenant isolation, and failure responses.
- Retrieval/replay tests assert stale generated synthesis is diagnostic-only and does not become stronger evidence than source-backed memory.
- Operator tests verify no production deletion path is introduced and all destructive source changes remain soft-delete or human-owned.

## Non-Goals

- Do not replace `items` as the library and retrieval root in the first slice.
- Do not hard-delete production source, chunks, claims, or artifacts.
- Do not auto-promote generated synthesis into canonical memory.
- Do not redesign room routing, graph relationship scoring, or embedding profile storage.
- Do not expose new public MCP tools before the internal dependency model and diagnostics are stable.
- Do not migrate every historical artifact class in the first implementation PR.

## First Implementation Slice

Create a follow-up implementation task for the smallest useful vertical slice:

1. Add `source_records` and `source_chunks` models plus Alembic migration.
2. Add a tenant-bounded, dry-run-capable backfill service for existing `items.content_chunks`.
3. Add a read-only internal endpoint returning source records and chunk summaries for one item.
4. Add tests for migration constraints, backfill idempotency, soft-deleted/failed item handling, and tenant isolation.
5. Leave claims, synthesis runs, and artifact dependencies as documented follow-on work until the source/chunk spine is proven.
