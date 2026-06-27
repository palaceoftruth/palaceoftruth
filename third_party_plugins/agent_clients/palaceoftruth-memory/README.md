# Palace of Truth Memory Plugin

This package installs the existing Palace MCP adapter as a repo-local agent
memory surface. It is intended for Codex first and keeps Claude-style metadata
beside the Codex manifest for clients that understand that simple plugin
shape.

## What Palace Of Truth Is Today

Palace of Truth is a tenant-scoped knowledge base, retrieval service, and
operator control plane for humans and agents. It accepts durable agent memory,
notes, webpages, documents, PDFs, images, media links, feeds, and batch imports,
then stores them as searchable items with chunks, embeddings, summaries, tags,
relationships, metadata, and job state.

The backend is a FastAPI service backed by PostgreSQL with pgvector and ARQ
workers. The React frontend exposes dashboard, ingest, search, chat, browse,
item detail, graph, feed, API docs, Palace, and Palace Control Tower views.
Deployment assets support local Docker Compose/devinfra, Helm, k8s manifests,
ArgoCD, and a dedicated MCP workload.

The current Palace-specific layer adds:

- a canonical `/api/v1/memory/*` facade for scoped agent memory
- memory job listing, polling, retry through REST/UI, and operator visibility
- hybrid retrieval with vector search, lexical rescue, tags, date filters,
  source filters, and captured retrieval traces
- RAG chat over the tenant corpus with citations and durable conversations
- feed management, OPML import, exports, item editing, soft delete/restore, and
  batch item actions
- graph relationships, related-item lookup, and traceable temporal facts
- Palace wings, rooms, room memberships, snapshots, tunnels, pinned curation,
  and room-scoped retrieval
- sync sources and runs for folder, repo, and S3-backed Palace ingestion
- wake-up briefs, diary rollups, worker backpressure, MCP client audit, and
  OAuth client registration/revocation in Control Tower

This plugin does not reimplement those behaviors. It packages the existing MCP
adapter so agent clients can call the Palace REST contract through MCP.

## Codex Skillpack

The package includes two installable skills:

- `palaceoftruth-memory` for MCP setup, safe tool boundaries, transcript
  preview, and smoke verification.
- `palaceoftruth-codex-memory` for the Codex operating protocol: Palace-first
  startup recall, scoped lookup, safe write-back, checkpoint capture, and local
  fallback behavior.

Use `palaceoftruth-codex-memory` as the always-on behavior layer for non-trivial
Codex work. It intentionally mirrors GBrain's brain-first discipline while
preserving Palace's scoped MCP contract and non-destructive safety boundary.
The dry-run helper `scripts/codex_session_lifecycle.py` provides copyable
startup, checkpoint, and post-run write-back payloads used by that behavior
layer.

When installing skills directly into a local Codex profile, keep one Palace
skill active by copying only `palaceoftruth-codex-memory` into
`~/.codex/skills`. The `palaceoftruth-memory` skill is setup/operator guidance
for packaged agent installs and smoke verification; copying both into
`~/.codex/skills` can make skill routing noisier without improving normal
Palace-first memory behavior.

## Default Transport

Use local `stdio` by default. The packaged MCP server launches:

```bash
uv --directory ../../../backend run python scripts/palaceoftruth_mcp.py
```

That command runs the repository's primary adapter source at
`backend/scripts/palaceoftruth_mcp.py`.

When installed from this repository, `../..` resolves from
`third_party_plugins/agent_clients/palaceoftruth-memory` back to the repo root,
while `../../../backend`
selects the Python project that owns the adapter dependencies. For copied
installs, update the MCP command directory to the absolute Palace `backend`
path.

## Required Configuration

Set a tenant runtime API key before starting the MCP server:

```bash
export PALACEOFTRUTH_API_BASE_URL="https://api.palaceoftruth.test"
export PALACEOFTRUTH_API_KEY="replace-with-tenant-api-key"
```

The package does not put a placeholder API key into `.mcp.json`; provide
`PALACEOFTRUTH_API_KEY` through the agent runtime environment or the client
secret store.

Use the `PALACEOFTRUTH_*` environment variables for new installs. Legacy
environment aliases remain supported in code for older deployments.

## One-Command Codex Setup

Run the repo setup verifier before pasting any MCP config into Codex. The
default mode is a non-mutating dry run that validates the adapter path, verifies
the Codex skillpack from SAR-355 is present, prints a redacted Codex config
snippet, and previews the exact live-smoke command:

```bash
uv run python scripts/setup_codex_palace_memory.py \
  --api-base-url https://api.palaceoftruth.test
```

This command is a verifier, not an installer. It does not edit
`~/.codex/config.toml`, copy skill files into `~/.codex/skills`, remove older
local skills, or clean up operator-created backups. Make those local profile
changes deliberately, then rerun the verifier to confirm the repo adapter and
skillpack are present.

After `PALACEOFTRUTH_API_KEY` is available in the Codex runtime environment,
add `--live-smoke` to launch the stdio adapter and write exactly one scoped
`agent/codex` memory. The live setup smoke disables relationship backfill and
does not call admin, retry, delete, cleanup, or restore operations:

```bash
PALACEOFTRUTH_API_KEY="$PALACEOFTRUTH_API_KEY" \
uv run python scripts/setup_codex_palace_memory.py \
  --api-base-url https://api.palaceoftruth.test \
  --live-smoke
```

## Safe Tool Boundary

The package exposes the current Palace MCP adapter tools only:

- `palace_search`
- `palace_remember`
- `palace_checkpoint`
- `palace_context`
- `get_wakeup_context`
- `whoami`
- `create_memory_entry`
- `get_memory_job`
- `get_retrieval_doctor`
- `capture_checkpoint`
- `list_memory_entries`
- `list_memory_scopes`
- `list_memory_jobs`
- `get_graph`
- `get_item_relationships`
- `list_temporal_facts`
- `get_palace_room`
- `backfill_deferred_relationships`
- `get_wakeup_brief`
- `retrieve_memory`
- `retrieve_agent_memory`
- `search_items`
- `list_tags`
- `list_items`

The write tools are `create_memory_entry`, `capture_checkpoint`, and
`backfill_deferred_relationships`, plus their `palace_remember` and
`palace_checkpoint` aliases. The graph, item-relationship,
temporal-fact, Palace-room, wake-up, retrieval, search, tag, item, job-list, and
session-context tools are read-only MCP surfaces, including the `palace_search`,
`palace_context`, and `get_wakeup_context` aliases.

Use `get_wakeup_context` for startup recall when an agent needs a bounded
hot-cache package: wake-up readiness, selected agent/workspace/session memory
summaries, recent checkpoint pointers, provenance IDs, and safe next probes.
Use `palace_search` or `retrieve_agent_memory` for a specific follow-up query,
and use `capture_checkpoint` only when writing a reviewed checkpoint.

If semantic retrieval tools such as `retrieve_memory`, `retrieve_agent_memory`,
or `palace_search` are temporarily failing, agents should not treat the whole
MCP surface as unavailable. They should tell the user that semantic retrieval is
degraded, then continue with read-only deterministic listing where possible:
`list_memory_scopes` for discovery, `list_memory_entries` for scoped agent,
workspace, session, or tenant-shared memory, `list_items` for ingested library
content, and `get_item_relationships` only after a specific visible item id is
known. User-facing output should avoid broad raw-content exposure and prefer
counts, IDs, titles, scopes, tags, timestamps, and short non-sensitive snippets
only when needed.

It does not expose admin registration, key rotation, failed-job retry, cleanup,
delete/restore, fact writes, graph writes, room mutation, sync-source mutation,
or new retrieval semantics as callable MCP tools. The adapter can use API-key,
bearer-token, or OAuth client-secret configuration, and it records best-effort
redacted MCP audit events internally; OAuth mint/revoke and request logging are
not exposed as agent-callable MCP tools.

The repository does include an offline transcript preview contract for Codex,
Claude Code, and Gemini CLI logs:

```bash
cd backend
uv run python ../scripts/normalize_agent_transcripts.py dry-run \
  --adapter codex \
  --tenant-id "$PALACEOFTRUTH_TENANT_ID" \
  --scope-type agent \
  --scope-key codex \
  /path/to/transcript.jsonl
```

That command only prints redacted `MemoryEntryRequest`-shaped JSON and warnings.
It does not connect to Palace and does not write memory.

Optional local capture is available through explicit sweep and silent hook
commands. They only write when `--write` is set and a tenant API key is supplied,
submit through `POST /api/v1/memory/entries`, use stable transcript idempotency
keys, and never delete source logs:

```bash
cd backend
uv run python ../scripts/normalize_agent_transcripts.py sweep \
  --adapter codex \
  --tenant-id "$PALACEOFTRUTH_TENANT_ID" \
  --scope-type agent \
  --scope-key codex \
  --lock-file "$HOME/.cache/palaceoftruth/transcript-sweep.lock" \
  "$HOME/.codex/sessions"
```

The `hook` command is silent by default and never writes to stdout, preserving
agent JSON-RPC streams. Use `--verbose` only when stderr diagnostics are safe.

## Non-Destructive Smoke

Dry-run the exact stdio command and tool payloads without connecting:

```bash
cd backend
uv run python ../scripts/smoke_agent_memory_compatibility.py \
  --api-base-url https://api.palaceoftruth.test \
  mcp-stdio \
  --scope-type agent \
  --scope-key codex \
  --relationship-policy deferred \
  --dry-run
```

A live smoke writes exactly one deterministic scoped memory, polls its job,
optionally queues one bounded deferred relationship backfill, retrieves that
memory, checks wake-up context when available, and lists recent memory jobs.

For the full Codex bridge gate, run the CI-safe dry run:

```bash
cd backend
uv run python ../scripts/smoke_agent_memory_compatibility.py \
  --api-base-url https://api.palaceoftruth.test \
  codex-bridge
```

That gate validates skillpack presence, the setup verifier, lifecycle recall
payloads, MCP tool reachability, dry-run checkpoint defaults, and secret
redaction without connecting to Palace or writing memory. Add `--live-smoke`
only after `PALACEOFTRUTH_API_KEY` is available in the runtime environment.

For improvement-planning and DOTODO startup checks, generate the compact
evidence refresh from the repository root:

```bash
uv run python scripts/smoke_agent_memory_compatibility.py startup-context-report \
  --run-id "$(date -u +%Y%m%d-%H%M%S)"
```

By default this report is offline and report-only. It labels direct local
evidence separately from Palace-generated synthesis, summarizes the
`get_wakeup_context` route, Codex bridge dry run, scorecard dry run, and
compatibility fixture health, and only previews task-pool or live deploy
commands. Use `--include-task-pool` or `--include-live-deploy` when those
read-only checks are intentionally part of the run.

## Codex Session Lifecycle

For normal Codex work, use the Palace-first lifecycle rather than relying on
manual memory habits:

1. Start with `whoami`, `get_wakeup_context`, and route-aware
   `retrieve_agent_memory` using `agent_scope_key="codex"` and the stable
   workspace key.
2. Before handoff or compaction, dry-run `capture_checkpoint` with sanitized
   summary/evidence snippets, then write only after review.
3. After substantial work, write concise durable learning with
   `create_memory_entry` or `palace_remember`.

Generate copyable dry-run payloads without connecting to Palace:

```bash
python3 scripts/codex_session_lifecycle.py \
  --workspace-key palaceoftruth \
  --session-key "$CODEX_RUN_THREAD_ID" \
  --format markdown
```

Do not store raw secrets, bearer tokens, private transcript text, or log dumps.
Use local Codex memory files only when Palace MCP is unavailable, scoped
retrieval drifts, or exact source-line citations are required.
