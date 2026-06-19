# Palace of Truth Project Status

Last updated: May 28, 2026

## TL;DR

Palace of Truth is usable as a local multi-service app and Kubernetes-deployable through the Helm chart. The product is materially beyond the original MVP plan.

It is not fully consumer-ready yet. The main remaining work is public packaging, manual retrieval-quality review on realistic corpora, broader integration smoke coverage, and deciding which deferred editor/grounding bets are worth pulling back into active scope.

## Shipped

- Local development runs as a full multi-service stack via `docker compose` and devinfra with `postgres`, `redis`, `backend`, `worker`, and `frontend`.
- Kubernetes deployment uses the Helm chart. Environment-specific ArgoCD resources and private values are maintained outside this public app repo.
- The chart now defaults backend/frontend image tags from `Chart.appVersion`, so each promoted chart revision pins the matching immutable image SHA unless `image.tag` is explicitly overridden.
- The chart has an opt-in `highAvailability.enabled` profile that scales app workloads, CloudNativePG, bundled Valkey Sentinel, and PodDisruptionBudgets while preserving the current lightweight defaults.
- The backend ships multi-tenant API-key auth, ingest flows for media/webpage/doc/image/note, search, chat, feeds, export, graph, jobs, memory APIs, and the Palace control plane.
- Palace freshness work now runs on a dedicated worker queue separate from the default ingestion/enrichment queue, so Palace builds and maintenance are not blocked behind expensive relationship extraction jobs.
- Bulk memory writes can defer or skip relationship extraction, then enqueue a throttled relationship backfill later through `/api/v1/memory/relationships/backfill`.
- Integration consumers have exercised Palace's memory facade and tenant lifecycle endpoints. Those integrations are examples, not required components for self-hosted Palace deployments.
- A Hermes-compatible runtime has been verified end-to-end for Palace repo-source create/sync/edit/delete using deployment-managed GitHub credentials, and the browse cleanup regression found during that smoke has been fixed.
- Integration consumers can use the Palace memory facade directly or through the packaged MCP adapter; example consumer contracts should stay generic and avoid environment-specific operator details.
- The Hermes memory plugin now resolves its authenticated tenant via `/api/v1/memory/whoami` before mirroring writes, so non-default tenant API keys can write durable memory without hardcoded `tenant_id="default"` assumptions.
- Control-plane tenant lifecycle hardening is now shipped: `POST /api/v1/admin/tenants/register` is idempotent and Palace exposes first-class admin endpoints to list, rotate, and revoke tenant API keys without breaking Hermes runtime compatibility.
- The Helm chart now has an opt-in split admin ingress for `/api/v1/admin/*`, so self-hosted operators can attach source allowlists or internal-only ingress controls to control-plane traffic without changing runtime memory/search/MCP routes.
- Memory operators can now list tenant-scoped memory jobs through REST and MCP, retry failed/cancelled memory writes through the memory facade, and read `failed_memory_jobs` plus `active_memory_jobs` from `/api/v1/stats` for lightweight automation checks.
- Operators have a strict first-use readiness report for Palace memory that defaults to read-only checks across API health, tenant identity, `/stats`, Control Tower freshness, recent memory jobs, MCP configuration diagnostics, and retained NIST artifact evidence; optional `--live-smoke` writes exactly one scoped memory for list/retrieve proof.
- Operators now have a local database health gate for Alembic chain integrity, pgvector/halfvec search requirements, tenant/key tables, job and MCP audit/OAuth tables, relationship tables, critical indexes, and Helm Postgres vector-extension bootstrap. The default mode is offline/static; live database inspection requires an explicit database URL and is report-only.
- Palace now has a background maintenance loop that periodically re-enqueues backlogged Palace runs when `dirty_generation` gets stranded above `indexed_generation`, repairs stale room snapshots and tunnels if indexed Palace artifacts drift without new dirty items, and exposes worker backpressure telemetry in Control Tower.
- Local folder sync sources can use the optional low-latency watcher path when `PALACE_SYNC_WATCHER_ENABLED=true`; scheduled rescans remain the durable default.
- Room curation is materially beyond the first pass: room rename, batch membership curation, room finder affordances, consolidation review, and non-destructive consolidation candidates have all shipped.
- The frontend ships real routes for Home, Library, Palace, Chat, Capture, Saved Web, Sources, Feeds, Search, Graph, API Docs, Settings, item detail, and Palace Control Tower.
- Palace UI guidance is now captured in [DESIGN.md](./DESIGN.md), and the major utility surfaces have been aligned with it.
- Dogfood benchmark runs have verified 250-item and 1000-item realistic corpora, deferred relationship extraction, worker queue drain, exact search/retrieve checks, semantic retrieval checks, and Palace generation catch-up. Detailed private run records have moved out of this public app repo.
- CI validates backend smoke tests, retrieval replay, frontend build, and Helm rendering. Maintainer CI can also build images and publish the Helm chart.

## In Progress

- The retained NIST ranking follow-up is closed after the stricter top-rank gate passed. Current retained-corpus decisions remain human-reviewed.
- Frontend automated coverage is still smoke-level and not yet enforced in CI, but the local Playwright suite now covers Palace, Home, capture-to-recall, Saved Web, feeds, search, chat, graph, and API docs.
- The 250-item NIST corpus benchmark exposed a real Palace quality target. The automated gate passes in the maintainer environment, rooming is coherent, and the retained top-rank gate passed after the RMF ranking fix.
- Backend correctness hardening is still ongoing around deeper ingest, feeds, worker orchestration, and bundle-flow integration coverage.
- Admin ingress restrictions remain operator-selected at deploy time. Treat `/api/v1/admin/*` as control-plane-only, and enable `ingress.admin` when a deployment needs a distinct ingress boundary for those endpoints.
- Deployment posture is single-replica by default, with optional Helm HA guardrails available for clusters that have enough capacity.

## Next

- Expand backend tests around ingest, feeds, jobs, and worker recovery paths.
- Expand frontend smoke coverage into library/item detail, Sources, Control Tower recovery flows, and more negative/error states.
- Reconcile retained benchmark artifacts into any external status or release notes that need them, and keep cleanup plans human-reviewed before any deletion.
- Use the strict operator readiness report before adding new observability surfaces; add more only where Control Tower, memory-job listing, webhook health, Palace maintenance telemetry, or scoped retrieval proof still fail real operational review.
- Decide whether the blocked advanced room editor should stay deferred, be decomposed into smaller curation tools, or be canceled after dogfooding.
- Decide whether authority-sensitive grounding has a real target corpus before designing the validation model.
- Keep environment-specific deployment values and raw manifests in the private deployment repository.
- Keep operational docs current as Palace and portability work land so top-level status reflects the live system, not the original MVP plan.

## Source Of Truth

- Current status and support level: this file.
- Historical architecture and MVP planning context: [ENGINEERING_PLAN.md](./ENGINEERING_PLAN.md).
- Deployment and integration details: [INTEGRATIONS.md](./INTEGRATIONS.md).
- Active task status: central project-manager task pool.
- Human-readable backlog and deferred bets: [TODOS.md](./TODOS.md).
