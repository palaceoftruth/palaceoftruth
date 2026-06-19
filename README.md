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
plugins/palaceoftruth-memory/ Codex plugin packaging for the Palace MCP adapter
third_party_plugins/hermes/   canonical Hermes memory plugin source
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

The repo-packaged Codex plugin lives in [plugins/palaceoftruth-memory](plugins/palaceoftruth-memory). It documents Codex setup, scope conventions, smoke verification, OAuth options, and transport-specific configuration.

For governed multi-agent memory positioning, use `scripts/demo_agent_organization_memory.py`. The demo shows specialist agents writing private `agent/<key>` memories while `agent/orchestrator` retrieves only server-authorized specialist scopes and writes only to its own agent scope.

## Hermes Plugin

The canonical Hermes memory plugin source lives at:

```text
third_party_plugins/hermes/memory/palaceoftruth
```

The plugin speaks the Palace memory facade:

- `GET /api/v1/memory/whoami`
- `POST /api/v1/memory/entries`
- `GET /api/v1/memory/scopes`
- `POST /api/v1/memory/retrieve-agent`
- `POST /api/v1/memory/retrieve` as the fallback/single-scope path

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
- [plugins/palaceoftruth-memory/README.md](plugins/palaceoftruth-memory/README.md): packaged MCP adapter and agent-memory setup

Private deployment runbooks, staging benchmark records, and historical planning archives live outside this public application repository.

