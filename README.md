# Palace of Truth

Palace of Truth is a cloud-native knowledge base, retrieval service, and agent-memory control plane for humans and AI agents.

It provides:

- a canonical `/api/v1/memory/*` REST contract for durable agent memory
- MCP adapters for Codex, Claude, OpenAI Agents, and other MCP-capable clients
- ingestion for notes, web pages, browser captures, media transcripts, documents, images, RSS feeds, source subscriptions, repository sync, and S3-compatible object stores
- hybrid retrieval with vector search, full-text search, deterministic lexical rescue, relationship expansion hooks, and replay gates
- tenant-scoped API keys, conversation history, memory jobs, retry paths, and operator telemetry
- Palace UI surfaces for capture, library browsing, search, chat, graph exploration, room curation, sources, feeds, and Control Tower operations
- Kubernetes-ready deployment assets with a Helm chart, optional MCP workload, CloudNativePG, Valkey, External Secrets Operator support, media workers, and an opt-in high-availability profile

For the live support-level snapshot, start with [PROJECT_STATUS.md](PROJECT_STATUS.md).

## License

Palace of Truth is open source under the GNU Affero General Public License, version 3 only (`AGPL-3.0-only`). See [LICENSE](LICENSE) for the full license text.

The Palace of Truth name, logo, and branding are reserved trademarks. See [TRADEMARKS.md](TRADEMARKS.md) for permitted descriptive uses.

Third-party reference and benchmark notices live in [NOTICE](NOTICE).

## Repository Layout

```text
backend/                      FastAPI service, workers, models, migrations, MCP server
frontend/                     Vite, React, TypeScript web app
extension/                    Chrome-compatible browser capture extension
chart/                        Helm chart for Kubernetes deployments
argocd/                       Notes for external ArgoCD Application ownership
k8s/                          Notes for external raw manifest ownership
third_party_plugins/           third-party plugin packages
  agent_clients/palaceoftruth-memory/
                                Codex/Claude plugin packaging for the Palace MCP adapter
  hermes/                       canonical Hermes memory plugin source
scripts/                      local smoke, benchmark, packaging, migration, and operator helpers
```

## Local Development

Standard local development uses Docker Compose. If your environment provides
local HTTPS routing for `*.test` hostnames, bring that up after Compose:

```bash
cp .env.example .env
# Fill in DB_PASSWORD, API_KEY, PALACEOFTRUTH_ADMIN_SECRET, OPENAI_API_KEY, and OPENROUTER_API_KEY as needed.
docker compose up --build -d
di up palaceoftruth
open https://palaceoftruth.test
```

For a public Docker-only fallback, use the Compose service ports directly and set
`VITE_API_PROXY_TARGET=http://localhost:8000` when running the host-side frontend:

```bash
docker network create traefik 2>/dev/null || true
docker compose -f docker-compose.yml -f docker-compose.localhost.yml up --build -d
open http://localhost:8080
```

The `.test` hostnames remain the standard review target when devinfra is available.

Local URLs:

- frontend: `https://palaceoftruth.test`
- API: `https://api.palaceoftruth.test`
- local streamable HTTP MCP host: `https://mcp.palaceoftruth.test/mcp`

Host-side frontend iteration:

```bash
cd frontend
npm install
npm run dev
npm run build
```

Host-run Vite proxies `/api`, `/docs`, and `/redoc` to the backend and injects
the server-side `API_KEY` from the root `.env`. `VITE_API_KEY` is an optional
local-only browser override and should normally stay unset.

Host-side backend iteration:

```bash
cd backend
pip install -e .
uvicorn app.main:app --reload
pytest
```

Start workers when testing ingestion, feed polling, Palace maintenance, media processing, or background memory jobs outside Compose:

```bash
cd backend
arq app.workers.worker.WorkerSettings
arq app.workers.worker.MediaWorkerSettings
arq app.workers.worker.PalaceWorkerSettings
```

Optional Firecrawl webpage scraping:

- `WEBPAGE_SCRAPER_PROVIDER=local` keeps the built-in trafilatura/Playwright scraper.
- `WEBPAGE_SCRAPER_PROVIDER=firecrawl-cloud` uses Firecrawl Cloud at `https://api.firecrawl.dev/v2` and requires `FIRECRAWL_API_KEY`.
- `WEBPAGE_SCRAPER_PROVIDER=firecrawl-self-hosted` uses `FIRECRAWL_BASE_URL`; `FIRECRAWL_API_KEY` is optional for private-network self-hosted deployments.

Self-hosted deployments can set:

```bash
WEBPAGE_SCRAPER_PROVIDER=firecrawl-self-hosted
FIRECRAWL_BASE_URL=https://firecrawl.example.internal/v2
```

For Helm-managed deployments, use:

```yaml
config:
  webpageScraperProvider: firecrawl-self-hosted
  firecrawlBaseUrl: https://firecrawl.example.internal/v2
```

After the worker is running inside that deployment environment, verify from an
application pod or worker pod that the Firecrawl base URL reaches the self-hosted
service, then run a focused webpage ingest and confirm the job metadata records
`content_source=firecrawl`.

## API And Product Surfaces

The backend registers these API domains under `/api/v1`:

- system health/stats
- ingest and browser capture
- web saves
- search and chat
- memory facade, memory jobs, relationship backfill, and retrieval diagnostics
- MCP OAuth and metadata
- items, tags, export, graph, conversations, feeds, source subscriptions, jobs, admin, curation artifacts, and Palace control-plane routes

The frontend routes are:

- Home, Library, Palace, Chat
- Capture, Saved Web, Sources, Feeds
- Search, Graph, API, Settings
- Item detail and Palace Control Tower

The browser extension submits captures through `/api/v1/capture/browser`; see [extension/README.md](extension/README.md).

## MCP And Agent Memory

The standalone MCP adapter lives at [backend/app/mcp_server.py](backend/app/mcp_server.py). It is a thin MCP wrapper over the existing Palace REST API and supports `stdio` and streamable HTTP transports.

Retrieval diagnostics include report-only provenance labels for operators:
`trust_class`, `source_support_state`, `freshness`, and
`derived_raw_classification`. Treat `raw_source` and source-backed
`curated_memory` as the strongest evidence, review `generated_synthesis` before
copying it into durable memory, and avoid letting `low_support_generated`,
`stale_context`, or `broad_fallback` results drive an answer without checking
the underlying source. Aggregate trace counts compare the mix across direct
retrieval, room routing, broad fallback, and generated artifacts; they are
diagnostic labels, not ranking approvals.

For session startup, use `get_wakeup_context` when an agent needs one compact
package with wake-up status, selected agent/workspace/session memory summaries,
checkpoint pointers, readiness warnings, and safe follow-up probes. Use
`palace_search` or `retrieve_agent_memory` after that for a specific question,
and use `capture_checkpoint` only when writing a reviewed handoff or compaction
checkpoint.

Before improvement planning or DOTODO task selection, generate a report-only
startup evidence refresh:

```bash
uv run python scripts/smoke_agent_memory_compatibility.py startup-context-report \
  --run-id "$(date -u +%Y%m%d-%H%M%S)"
```

The default report stays offline and non-mutating. It summarizes the Codex
bridge dry run, `get_wakeup_context` readiness, the dry-run scorecard, offline
compatibility fixtures, and opt-in command previews for task-pool and live
deploy checks. Add `--include-task-pool` or `--include-live-deploy` only when
read-only network checks are explicitly desired.

The repo-packaged Codex/Claude plugin lives in [third_party_plugins/agent_clients/palaceoftruth-memory](third_party_plugins/agent_clients/palaceoftruth-memory). It documents Codex setup, scope conventions, smoke verification, OAuth options, and transport-specific configuration.

For governed multi-agent memory positioning, use `scripts/demo_agent_organization_memory.py`. The demo shows specialist agents writing private `agent/<key>` memories while `agent/orchestrator` retrieves only server-authorized specialist scopes and writes only to its own agent scope.

## Hermes Plugin

The canonical Hermes memory plugin source lives at:

```text
third_party_plugins/hermes/memory/palaceoftruth
```

The plugin speaks the Palace memory facade:

- `GET /api/v1/memory/whoami`
- `POST /api/v1/memory/entries`
- `POST /api/v1/memory/entries:batch`
- `GET /api/v1/memory/scopes`
- `POST /api/v1/memory/retrieve-agent`
- `POST /api/v1/memory/retrieve` as the fallback/single-scope path

Memory writes return a durability contract for clients. A `202` response means
Palace accepted a durable item/job before enqueueing background work. Clients
should persist `job_id`, poll `poll_url` after `poll_after_seconds`, and inspect
`contract_status`, `retryable`, `retry_after_seconds`, and `queue.state` before
retrying. `queue.state=backpressure` or `saturated` is a retry/defer hint, not a
hard server-side rate limit; the matching `Retry-After`,
`X-Palace-Memory-Queue-State`, `X-Palace-Memory-Poll-After`, and
`X-Palace-Rate-Limit-State` headers carry the same guidance for HTTP clients.
If enqueue dependencies are unavailable, Palace returns `503` with
`contract_status=dependency_unavailable`, the accepted `job_id`, `poll_url`, and
`Retry-After` so clients can poll or retry the accepted job without rewriting
the memory body.

Bulk clients can send up to 100 canonical memory entries to
`POST /api/v1/memory/entries:batch`. The batch endpoint reuses the single-entry
tenant, admission, idempotency, queue, and retry contract for each entry and
returns ordered per-item results with `index`, `status`, `contract_status`,
`job_id`, `poll_url`, `retryable`, and sanitized error details when an entry is
rejected. For deterministic landing checks, write the batch, poll accepted jobs,
then verify imported records with exact `source_url` filtering on
`GET /api/v1/items?source_url=<encoded-url>`. Item listings continue to support
`page`/`per_page`; created-at sorted audit scans may also follow the returned
`next_cursor` with `GET /api/v1/items?cursor=<cursor>&sort=created_at`.

For an explicit authenticated production smoke, set an operator-provided API key
in the runtime environment and run:

```bash
PALACEOFTRUTH_API_KEY=... uv run python scripts/smoke_agent_memory_compatibility.py \
  --api-base-url https://api.palace.example.com \
  batch-production-smoke \
  --run-id "$(date -u +%Y%m%d-%H%M%S)"
```

The smoke writes two bounded `memory://production-smoke/batch/<run-id>/...`
entries, polls their accepted jobs, verifies each item through exact
`source_url` filtering, and follows a returned item-listing cursor for the
unique run tag. It does not read cluster secrets, use admin endpoints, or delete
production data. Use `--dry-run` to inspect the exact payload and verification
plan without writing.

Operators and clients can use `GET /api/v1/version` for the deployed app version
and `GET /api/v1/ready` for dependency-aware readiness. `GET /api/v1/health`
remains the simple Kubernetes-compatible probe and intentionally returns only
`{"status":"ok"}`.

Every plugin behavior change should bump `third_party_plugins/hermes/memory/palaceoftruth/plugin.yaml` so CI can publish a new release artifact.

Release assets are tagged as:

```text
hermes-memory-plugin-v<version>
```

Maintainers publish release containers to GHCR. External operators can use
those public images or build and publish their own runtime images under their
own registry coordinates.

## Deployment

The primary deployment artifact is the Helm chart in [chart/](chart/). External
operators should build and publish backend/frontend images to their own
registry, then override `image.registry`, `image.backendRepository`,
`image.frontendRepository`, and `image.tag` as needed.

For install, ArgoCD, External Secrets, S3/repo sync credentials, local embedding service, admin ingress, media worker sizing, and high-availability options, use [INTEGRATIONS.md](INTEGRATIONS.md).

Environment-specific ArgoCD Applications, raw manifests, DNS targets, secret-manager item IDs, and Helm values overlays belong in a private deployment repository.

## Verification

Useful local checks:

```bash
cd backend
pytest
uv run python ../scripts/check_database_health.py

cd ../frontend
npx playwright install --with-deps chromium
npm run build
# Standard stack mode uses https://palaceoftruth.test.
PLAYWRIGHT_BASE_URL=https://palaceoftruth.test PALACE_FRONTEND_BASE_URL=https://palaceoftruth.test npm run test:e2e

cd ../extension
npm test
```

CI currently runs a backend smoke subset, the static database health gate, retrieval replay gate, frontend build, and Helm rendering checks on self-hosted Linux x64 runners. Frontend Playwright and extension tests are available local checks but are not enforced in CI yet.

## Documentation Map

- [PROJECT_STATUS.md](PROJECT_STATUS.md): current shipped, in-progress, and next status
- [INTEGRATIONS.md](INTEGRATIONS.md): deployment and integration runbook
- [CONTRIBUTING.md](CONTRIBUTING.md): contributor workflow and repo conventions
- [SECURITY.md](SECURITY.md): security posture and vulnerability reporting
- [DESIGN.md](DESIGN.md): frontend and product design guidance
- [docs/source-synthesis-compiler-design.md](docs/source-synthesis-compiler-design.md): typed source, chunk, claim, and synthesis compiler model proposal
- [third_party_plugins/agent_clients/palaceoftruth-memory/README.md](third_party_plugins/agent_clients/palaceoftruth-memory/README.md): packaged MCP adapter and agent-memory setup

Private deployment runbooks, staging benchmark records, and historical planning archives live outside this public application repository.
