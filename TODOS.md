# TODOs

This file is a human-readable backlog snapshot. The central project-manager task pool is the operational source of truth; for the current shipped / in progress / next snapshot, use [PROJECT_STATUS.md](PROJECT_STATUS.md).

## Recently completed

- Hardened the conversation history API so titles and message content are trimmed and validated, message reads stay tenant-scoped, and direct CRUD coverage now protects `GET/POST/PATCH/DELETE /api/v1/conversations/*`.
- Shipped idempotent tenant registration plus tenant API key list, rotate, revoke, audit, and usage metadata surfaces under `/api/v1/admin/*`.
- Split Palace builds and maintenance onto a dedicated worker queue so Palace freshness does not wait behind expensive default-queue enrichment jobs.
- Added `relationship_policy` for memory writes and a throttled deferred relationship backfill endpoint for bulk/import workflows.
- Coalesced Palace dirty marking for bulk writes so large imports can schedule one Palace rebuild instead of per-item freshness churn.
- Surfaced worker backpressure telemetry in Control Tower and benchmark output.
- Expanded the Palace maintenance loop beyond backlog replay to refresh dirty rooms, repair stale artifacts, recompute tunnel strengths, and recover stale memory jobs.
- Added optional low-latency local folder sync watching for changed folder sync sources.
- Shipped first-class room curation increments: rename, batch membership changes, room finder affordances, consolidation review, and non-destructive consolidation candidates.
- Wrote `DESIGN.md`, extended it for Palace and Control Tower, and applied the documented utility-shell rules across Feeds, Item detail, Graph, Search, Settings, and API Docs.
- Preserved and restored original uploaded binaries in portable bundle archives.
- Exposed read-only memory job health through MCP for agent/operator checks.

## ExampleOS / Hermes

- No active ExampleOS/Hermes implementation backlog is tracked here right now. Keep `/api/v1/admin/*` control-plane-only and prefer targeted follow-up tasks in the central pool when a real integration issue appears.

## Palace

### Add an advanced room editor

**What:** Add merge, split, rename, redirect review, and conflict-resolution tools on top of the Palace lineage system.

**Status:** Deferred / blocked in the central task pool.

**Why:** Users can already rename rooms, batch-curate memberships, review consolidation candidates, and use room finder affordances. A larger merge/split editor should wait until real dogfooding shows which room-shape failures remain painful enough to justify the complexity.

**Context:** The CEO review locked in immutable room IDs, lineage/redirect infrastructure, and lightweight curation, but intentionally stopped short of full room editing for v1. Keep this as a future product bet rather than active next work.

**Effort:** L
**Priority:** P2
**Depends on:** Observing actual room-shape failure modes in use

### Add authority-sensitive grounding mode for future corpora

**What:** Add an optional retrieval and answer-validation mode for authority-sensitive document sets such as legal, regulatory, policy, compliance, or standards material.

**Status:** Deferred / blocked in the central task pool.

**Why:** The current Palace + chat stack is strong on scoped retrieval, raw-source retention, and operator traceability, but it does not yet verify proposition-level support, authority hierarchy, or source applicability. That is acceptable for the current product direction, but would be the wrong trust model once Palace of Truth starts answering against documents where outdated, inapplicable, or weakly supported citations are materially risky.

**Context:** Palace already returns first-class routing and fallback trace data and keeps raw excerpts visible, which is a strong foundation. The missing future layer is authority-aware grounding: exact supporting excerpts per claim, stronger abstention when support is weak, and reranking/validation that can distinguish "retrieved a related source" from "retrieved the governing source." This should stay out of the near-term roadmap because the product is not ingesting legal-style corpora right now.

**Possible scope later:**
- proposition-level citation support instead of title-level source references only
- authority-aware reranking and filters for jurisdiction, source type, recency, and controlling-vs-secondary material
- explicit unsupported / weak-support answer states rather than confident synthesis
- benchmark queries and evals for misgrounding, stale authority, and wrong-context retrieval

**Effort:** L
**Priority:** P3
**Depends on:** Real demand for authority-sensitive corpora and a clear target domain before designing the validation model


## Preserve original uploaded files in portable bundles

Status: shipped. Original upload artifacts are now persisted and restored through portable bundle archives.
