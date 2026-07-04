# Post-Wakeup Claims, Promotion, And Invalidation Design

Task: SAR-935

## Goal

Define the first post-wakeup claim slice after the source-backed wakeup demo and
operator page landed. This is a research and implementation-design artifact, not
an implementation PR.

The source-backed wakeup MVP already gives a fresh agent compact trust state:
`source_backed`, `generated_unpromoted`, `stale_source`, `source_missing`,
`policy_limited`, and `unknown`. The next layer should let Palace explain a
bounded source-backed claim without turning every memory, task, artifact, or
relationship into a truth graph.

## Recommendation

Build the first claim slice around `claim_type='decision'`.

Decision claims are the smallest valuable post-wakeup claim because they answer a
high-value startup question: "What reviewed decision should this agent honor, and
what source proves it?" They are stable enough to review, easier to source than
preferences, less volatile than task state, and safer than policy or artifact
summary promotion.

Use the existing `claims` and `claim_sources` tables. Do not add a parallel
`wakeup_context_claim` type. The first implementation should extend the current
source compiler and wakeup trust path so selected source-backed decision claims
can be surfaced during wakeup with their source support and stale warnings.

## Rejected First Claim Types

- `task_state`: too volatile and already has a system of record in Linear,
  GitHub, CI, and deployment systems. Palace should point to task-state sources,
  not become the primary task-state authority.
- `preference`: useful, but promotion rules require stronger consent and
  cross-session privacy semantics before it should be source-backed at wakeup.
- `policy`: highest operational risk. Policy claims should wait for explicit
  authority hierarchy, expiry, and reviewer separation.
- `artifact_summary`: likely generated. It should stay
  `generated_unpromoted` until dependency tracking and promotion are proven.
- General `fact` expansion: already has a temporal-fact backfill path. It is a
  useful substrate, but not the first operator-facing post-wakeup claim product.

## Current Reusable Surface

Palace already has the substrate for the first slice:

- `source_records` and `source_chunks` materialize source versions and chunk
  digests.
- `claims` and `claim_sources` model source-backed propositions and support
  rows.
- `source_compiler` backfills source rows from items and claim rows from
  temporal facts.
- `source_trust_summary` and `get_wakeup_context` already surface compact trust
  state without raw chunks or source previews.
- Retrieval provenance already distinguishes raw, curated, generated, stale, and
  broad-fallback evidence classes.

The main gap is productizing a reviewed decision-claim path and wiring exact
support into wakeup context. `claim_sources.source_chunk_id` currently may be
empty, so the first implementation should tighten chunk linkage where possible
instead of adding new tables.

## Claim Model Boundary

A decision claim is a normalized proposition that records a reviewed operational
or product decision extracted from source-backed material.

Minimum boundary:

- The claim body is the decision, not the whole memory entry or document.
- The source support points to source records and, when available, source chunks
  or source spans.
- The claim belongs to a tenant and can carry workspace, task, PR, or run
  metadata for retrieval and audit.
- The claim can be used as startup context only when support is current and the
  claim is not rejected, stale, conflicted, or superseded.

Do not let a generated wakeup brief, diary rollup, routing manifest, or artifact
summary become a decision claim without an explicit promotion step.

## Promotion States

Reuse existing claim status values for claim truth state:

- `draft`: extracted candidate that has not been approved.
- `active`: promoted or accepted as usable source-backed context.
- `stale`: support no longer matches the current source version.
- `conflicted`: a contradicting source or claim exists.
- `rejected`: operator decided this should not become startup authority.
- `superseded`: a newer claim replaces this decision.

Track promotion event detail outside the status itself, either in claim metadata
for the first slice or a later event table when review history needs richer
querying. Minimum event detail is reviewer identity, review role, reviewed time,
source ids, rationale, and previous status.

## Minimum Dependency Graph

The first slice should use direct dependencies only:

- `claim -> source_record`
- `claim -> source_chunk` when exact chunk support exists
- `claim -> source_digest`
- optional metadata links to task id, PR URL, automation run id, or source URL

This is enough to tell a fresh agent why a decision is source-backed and whether
its source is current. It intentionally does not model artifact-to-artifact
dependencies, full synthesis runs, or graph-wide invalidation yet.

The conceptual model can borrow W3C PROV terminology:

- source records and claims are entities,
- extraction, promotion, and revalidation are activities,
- humans, agents, and tools are agents.

For implementation, keep Palace's existing relational rows as the source of
truth and use OpenLineage-style metadata facets only for extensible run details.
Runtime tracing can link extraction spans, but traces are not durable provenance.

## Stale Invalidation Triggers

The first implementation should be report-first and non-destructive.

Mark a decision claim `stale` when:

- its source record becomes `stale`, `failed`, `deleted`, or `superseded`,
- its stored source digest no longer matches the current source support,
- a newer source version for the same item supersedes the old version,
- the exact chunk support disappears,
- or a revalidation pass finds a contradicting current decision claim.

Mark a claim `conflicted` when a current source directly contradicts it. Mark a
claim `superseded` only when a replacement claim is known. Mark `rejected` only
through operator review. Never hard-delete production source or claim rows from
an invalidation pass.

## Operator UX Surface

Start with an operator-facing review surface that can answer these questions:

- What decision is Palace proposing as source-backed startup context?
- Which source record, chunk/span, URL, task, or PR supports it?
- Is the support current, stale, missing, conflicted, or policy-limited?
- Who or what promoted it, and when?
- What action is available: promote, reject, mark stale, supersede, or inspect
  source?

The wakeup payload should remain compact. It should show titles, summaries,
trust labels, warning codes, source pointers, and safe follow-up probes, not raw
source bodies or private cross-agent content.

## Migration And Backfill Risk

Do not bulk-promote historical decisions.

Safe sequence:

1. Keep existing source and claim tables.
2. Add a dry-run report that finds decision-like candidates from source-backed
   memory or temporal facts.
3. Backfill only a small tenant-bounded sample into `draft` decision claims.
4. Require operator promotion before any decision claim becomes wakeup
   authority.
5. Add stale/conflict diagnostics before automatic rebuild or repair.

Backfills must be tenant-bounded, resumable, dry-run capable, and safe to rerun.
They must preserve existing source rows and use soft status transitions instead
of destructive mutation.

## Test Strategy

- Unit tests for decision claim projection, deterministic claim keys, source
  digest capture, and exact chunk linkage when a chunk is available.
- Service tests for source edit, failed source, deleted source, superseded
  source, missing chunk, contradiction, and status transition behavior.
- API tests for tenant isolation and claim support reporting.
- MCP tests that assert wakeup context includes compact decision-claim trust
  state without raw chunks, body text, or source previews.
- Regression tests for the source-backed wakeup demo and docs link so the
  roadmap keeps pointing from wakeup trust to this design.

## Out Of Scope

- No production data mutation.
- No implementation in SAR-935.
- No new public MCP write tools.
- No automatic promotion of generated synthesis.
- No full artifact dependency graph.
- No synthesis-run table design in this slice.
- No authority hierarchy for policy claims.
- No replacement for Linear, GitHub, CI, deploy state, or other systems of
  record.

## Follow-Up Task Updates

- SAR-936 should start with `claim_type='decision'`, use existing source and
  claim tables, add exact chunk linkage where possible, and expose a read-only
  decision-claim support path before any promotion workflow.
- SAR-937 should promote decision claims first, not every generated artifact.
- SAR-938 should invalidate direct claim-source dependencies before modeling
  synthesis runs or artifact-to-artifact dependencies.
- SAR-939 should wait until decision claim support, promotion, and direct
  invalidation are implemented and tested.
