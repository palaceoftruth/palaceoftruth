# Palace of Truth

Palace of Truth is an open source knowledge base, retrieval service, and memory
control plane for people and AI agents. It stores sources, notes, documents,
media transcripts, and curated facts in one tenant-scoped system, and it serves
them back to both humans and agents through a stable API and an MCP adapter.

Most agent memory is a set of per-tool stores with no shared source of truth.
Palace gives every agent the same memory surface: durable writes with a
delivery contract, hybrid retrieval, explicit scopes, and provenance labels
that show whether an answer rests on a raw source or on a generated summary.

## What it provides

- A canonical memory API under `/api/v1/memory/*` for durable agent memory
- MCP adapters for Codex, Claude, [OI] Agents, and other MCP-capable clients,
  over `stdio` and streamable HTTP
- Ingestion for notes, web pages, browser captures, media transcripts,
  documents, images, RSS feeds, source subscriptions, repository sync, and
  S3-compatible object stores
- Hybrid retrieval that combines vector search, full-text search, deterministic
  lexical rescue, and relationship expansion
- Tenant-scoped API keys, conversation history, memory jobs, retry paths, and
  operator telemetry
- A web interface for capture, library browsing, search, chat, graph
  exploration, room curation, sources, feeds, and operations
- Kubernetes deployment assets: a Helm chart, an optional MCP workload,
  CloudNativePG, Valkey, External Secrets Operator support, media workers, and
  an opt-in high-availability profile

## Architecture

```text
                 +-------------------------------+
  humans  ------> |  frontend (Vite + React)      |
                 +---------------+---------------+
                                 |
                 +---------------v---------------+
  agents  ------> |  backend (FastAPI)            |
  (MCP, REST)    |  memory API, retrieval,       |
                 |  ingestion, auth, jobs        |
                 +------+----------------+-------+
                        |                |
              +---------v------+  +------v-----------+
              | PostgreSQL     |  | Valkey / Redis   |
              | + pgvector     |  | queues + cache   |
              +----------------+  +------------------+
                        ^
              +---------+---------+
              |  ARQ workers      |
              |  ingest, media,   |
              |  memory, feeds    |
              +-------------------+
```

The backend owns authorization, tenancy, and the memory contract. Workers run
ingestion, media, feed, and memory maintenance jobs outside the request path.
The MCP adapter is a thin wrapper over the REST API, so an agent and a browser
see the same data and the same rules.

## Quickstart

You need Docker with the Compose plugin.

```bash
git clone https://github.com/palaceoftruth/palaceoftruth.git
cd palaceoftruth
cp .env.example .env
```

Set these values in `.env` before the first start:

- `DB_PASSWORD` and `REDIS_PASSWORD`: unique local secrets
- `API_KEY` and `CREDENTIAL_PEPPER`: unique secrets, for example
  `openssl rand -hex 32`
- `OPENAI_API_KEY` and `OPENROUTER_API_KEY`: provider keys
- `PALACEOFTRUTH_ADMIN_SECRET`: a unique admin secret

The backend refuses to start while any of these still holds its example value.
Then start the stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.localhost.yml up --build -d
```

Open <http://localhost:8080>. The same stack exposes:

- API and OpenAPI documentation: <http://localhost:8000/docs>
- MCP streamable HTTP endpoint: <http://localhost:8765/mcp>

All published ports bind to `127.0.0.1`. Do not publish them on a shared
network.

To stop the stack:

```bash
docker compose down
```

## Configuration

Configuration is environment driven. `.env.example` lists every supported key
with comments, and the Helm chart exposes the same settings under `config`.

The settings most deployments change first:

- `DATABASE_URL` and `DB_PASSWORD`: PostgreSQL connection
- `REDIS_URL` and `REDIS_PASSWORD`: Valkey or Redis connection
- `OPENROUTER_API_KEY`: model provider for generation, vision, and
  relationship classification
- `WEBPAGE_SCRAPER_PROVIDER`: `local`, `firecrawl-cloud`, or
  `firecrawl-self-hosted`
- `TRANSCRIPTION_PROVIDER`: [OI], AssemblyAI, or a local WhisperX runtime

Optional integrations are documented in [INTEGRATIONS.md](INTEGRATIONS.md),
including ArgoCD ownership, External Secrets, S3 and repository sync
credentials, a local embedding service, admin ingress, media worker sizing, and
the high-availability profile.

## Agent memory

Agents reach Palace in two ways.

**MCP.** Point an MCP client at the streamable HTTP endpoint, or run the adapter
over `stdio`. For shared and remote runtimes use OAuth client credentials
rather than a static API key. The packaged adapter in
[third_party_plugins/agent_clients/palaceoftruth-memory](third_party_plugins/agent_clients/palaceoftruth-memory)
documents client setup, scope conventions, transport configuration, and smoke
checks.

**REST.** The memory facade is the stable contract for non-MCP clients:

- `GET /api/v1/memory/whoami`
- `POST /api/v1/memory/entries`
- `POST /api/v1/memory/entries:batch`
- `GET /api/v1/memory/scopes`
- `POST /api/v1/memory/retrieve-agent`
- `POST /api/v1/memory/retrieve`

Writes return a durability contract. A `202` response means Palace accepted a
durable item and job before it enqueued background work. Persist `job_id`, poll
`poll_url` after `poll_after_seconds`, and read `contract_status`, `retryable`,
and `queue.state` before you retry. A `503` with
`contract_status=dependency_unavailable` still carries the accepted `job_id`
and a `Retry-After` value, so the client can poll without rewriting the body.

Scopes narrow access. They do not grant it. Every API key carries a stored
scope grant, and each request is authorized against the intersection of that
grant and its scope headers. A header can drop a scope for one call. It can
never add one the key does not hold.

See [docs/agent-memory-contract.md](docs/agent-memory-contract.md) for the full
write contract, batch behaviour, and scope header rules. The Hermes memory
plugin lives in
[third_party_plugins/hermes/memory/palaceoftruth](third_party_plugins/hermes/memory/palaceoftruth).

## Retrieval quality

Every search hit can carry provenance and currentness labels, so a reader can
judge the evidence instead of trusting a rank:

- `trust_class`: `raw_source`, `curated_memory`, `generated_synthesis`, or
  `low_support_generated`
- `source_support_state`: whether an exact source still supports the claim
- `freshness`: how old the supporting source is
- `governance_currentness_state`: `unassigned`, `current`, `expired`, or
  `superseded`

Expired items stay visible in current mode with a ranking penalty, so the
warning travels with the result. Superseded items appear only in historical
mode. These labels are diagnostic. They are not a ranking approval.

Item governance, the PATCH payload, the currentness rules, and the audit trail
are documented in [docs/item-governance.md](docs/item-governance.md).

## Deployment

The primary deployment artifact is the Helm chart in [chart/](chart/). Build
and publish the backend and frontend images to your own registry, then set
`image.registry`, `image.backendRepository`, `image.frontendRepository`, and
`image.tag`.

```bash
helm install palace ./chart \
  --namespace palace \
  --create-namespace \
  --values my-values.yaml
```

The chart supports CloudNativePG or an external PostgreSQL, Valkey with
optional Sentinel, External Secrets Operator, an optional MCP deployment, media
workers, and an opt-in high-availability profile. Environment-specific
manifests, DNS targets, secret identifiers, and values overlays belong in your
own private deployment repository, not here.

## Development

Backend, Python 3.12 or newer:

```bash
cd backend
pip install -e .
uvicorn app.main:app --reload
pytest
```

Frontend, Node 22 or newer:

```bash
cd frontend
npm install
npm run dev
npm run build
```

Browser extension:

```bash
cd extension
npm install
npm test
```

Workers, for ingestion, media processing, feed polling, Palace maintenance, and
background memory jobs:

```bash
cd backend
arq app.workers.worker.WorkerSettings
arq app.workers.worker.MediaWorkerSettings
arq app.workers.worker.PalaceWorkerSettings
```

Run the migration round-trip harness only against a disposable loopback
database whose name starts with `palace_migration_`:

```bash
MIGRATION_TEST_ALLOW_DESTRUCTIVE=1 \
MIGRATION_TEST_DATABASE_URL=postgresql://palace:***@localhost:5432/palace_migration_local \
  uv run --directory backend python ../scripts/verify_migration_roundtrip.py
```

The harness downgrades to the first intentionally irreversible data-shape
migration, then upgrades back to head. It rejects any other database name, any
non-loopback host, and a missing acknowledgement, so it cannot run its
destructive downgrade against a deployed database.

## Repository layout

```text
backend/                      FastAPI service, workers, models, migrations, MCP server
frontend/                     Vite, React, TypeScript web app
extension/                    Chrome-compatible browser capture extension
chart/                        Helm chart for Kubernetes deployments
argocd/                       Notes for external ArgoCD Application ownership
k8s/                          Notes for external raw manifest ownership
third_party_plugins/          Packaged agent clients and the Hermes memory plugin
scripts/                      Local smoke, benchmark, packaging, and operator helpers
docs/                         Product, API, and design documentation
```

## Documentation

- [PROJECT_STATUS.md](PROJECT_STATUS.md): shipped, in progress, and next
- [INTEGRATIONS.md](INTEGRATIONS.md): deployment and integration runbook
- [DESIGN.md](DESIGN.md): product and interface design guidance
- [CONTRIBUTING.md](CONTRIBUTING.md): contributor workflow and conventions
- [SECURITY.md](SECURITY.md): security posture and vulnerability reporting
- [docs/agent-memory-contract.md](docs/agent-memory-contract.md): memory write
  contract, batch behaviour, and scope headers
- [docs/item-governance.md](docs/item-governance.md): item ownership,
  verification, currentness, and audit trail
- [docs/palace-oauth-scope-catalog.md](docs/palace-oauth-scope-catalog.md):
  supported MCP OAuth scopes and token examples
- [docs/palace-oauth-mcp-runtime-rollout.md](docs/palace-oauth-mcp-runtime-rollout.md):
  OAuth-first MCP deployment and credential lifecycle
- [docs/source-backed-wakeup-demo.md](docs/source-backed-wakeup-demo.md): a
  source-backed wakeup walkthrough for agent teams
- [docs/agent-organization-memory-demo.md](docs/agent-organization-memory-demo.md):
  governed multi-agent memory with private and shared scopes

## Contributing

Issues and pull requests are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) before you start, and follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

For a security issue, do not open a public issue. Follow
[SECURITY.md](SECURITY.md).

## License

Palace of Truth is open source under the GNU Affero General Public License,
version 3 only (`AGPL-3.0-only`). See [LICENSE](LICENSE) for the full text.

The Palace of Truth name, logo, and branding are reserved trademarks. See
[TRADEMARKS.md](TRADEMARKS.md) for permitted descriptive uses.

Third-party notices live in [NOTICE](NOTICE).
