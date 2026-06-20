---
name: palaceoftruth-memory
description: Use Palace of Truth as a scoped MCP memory adapter for Codex or Claude-style agent clients, including setup, safe tool boundaries, scope conventions, and non-destructive smoke verification.
---

# Palace Of Truth Memory

Use this skill when configuring, validating, or operating the Palace of Truth
MCP memory package for an agent client.

## Current Product Shape

Palace of Truth is a tenant-scoped knowledge base, retrieval service, and
operator control plane for humans and agents. It ingests durable agent memory,
notes, webpages, documents, PDFs, images, media links, feeds, and batch imports,
then stores searchable items with chunks, embeddings, summaries, tags,
relationships, metadata, and job state.

The MCP package is not a separate memory implementation. It packages the
existing Palace MCP adapter so an agent client can call the Palace REST
contract for scoped memory, retrieval, search, graph inspection, temporal facts,
Palace rooms, wake-up context, items, tags, and operational job visibility.

For Codex's always-on memory behavior, load the companion
`palaceoftruth-codex-memory` skill. This setup skill covers installation,
adapter safety, transcript preview, and smoke verification; the companion skill
covers Palace-first recall, scoped write-back, checkpoint capture, and local
fallback routing during normal Codex work.

Palace itself also has a React operator UI for ingest, search, chat, browse,
graph, feeds, room navigation, pinned curation, sync-source management, MCP
client registration/revocation, memory-job retry, worker backpressure, wake-up
briefs, diary rollups, and fact summaries.

## Setup

Prefer local `stdio` for Codex and other local agents. The packaged MCP config
launches the existing repo adapter:

```bash
uv --directory ../../backend run python scripts/palaceoftruth_mcp.py
```

That command runs the repository's primary adapter source at
`backend/scripts/palaceoftruth_mcp.py`.

Required runtime environment:

```bash
PALACEOFTRUTH_API_BASE_URL=https://api.palaceoftruth.test
PALACEOFTRUTH_API_KEY=<tenant-api-key>
```

The package does not put a placeholder API key into `.mcp.json`; provide
`PALACEOFTRUTH_API_KEY` through the agent runtime environment or the client
secret store.

Use `PALACEOFTRUTH_*` environment variables for new installs. Legacy aliases
remain supported in code for older deployments.

Run the repo-owned setup verifier as the first Codex setup step. Its default
mode is a non-mutating dry run that validates adapter and skillpack paths,
prints a redacted Codex config snippet, and previews the explicit live smoke:

```bash
uv run python scripts/setup_codex_palace_memory.py \
  --api-base-url https://api.palaceoftruth.test
```

Use `--live-smoke` only after the tenant runtime API key is available in the
agent environment. The live setup smoke writes exactly one scoped `agent/codex`
memory through stdio MCP, disables relationship backfill, and does not call
admin, retry, delete, cleanup, or restore operations.

For ongoing Codex session lifecycle setup, use the dry-run helper
`scripts/codex_session_lifecycle.py`. It provides copyable startup recall,
checkpoint preview, and post-run write-back payloads without connecting to
Palace or printing raw secrets/transcripts.

## Scope Convention

Use scopes as the first lookup boundary:

- `agent` with `scope_key="codex"` for durable Codex-wide memory.
- `workspace` with a stable repo or project key for project memory.
- `session` with a thread or run id for one handoff.
- `tenant_shared` only for memory every tenant runtime should see.

Use tags only as secondary filters. Use `relationship_policy="immediate"` for
normal notes and `relationship_policy="deferred"` only for bulk imports that
will explicitly call `backfill_deferred_relationships`.

## Safe Tool Boundary

Allowed tools are the adapter's current memory, wake-up, job-list, search,
tag-list, item-list, bounded graph, item-relationship, temporal-fact, and
Palace-room tools. The `palace_search`, `palace_remember`,
`palace_checkpoint`, `palace_context`, and `get_wakeup_context` tools are
Codex-friendly aliases over those same canonical tools, not a separate memory
API.

The only MCP write tools are `create_memory_entry`, `capture_checkpoint`, and
`backfill_deferred_relationships`, plus the shorthand aliases
`palace_remember` and `palace_checkpoint`. Graph, item-relationship,
temporal-fact, Palace-room, wake-up, retrieval, search, tag, item, job-list,
`palace_search`, `palace_context`, and `get_wakeup_context` tools are read-only
MCP surfaces.

Do not add or route through admin registration, key rotation, failed-job retry,
cleanup, delete/restore, fact writes, graph writes, room mutation, sync-source
mutation, or retrieval-ranking changes from this package. The adapter can use
API-key, bearer-token, or OAuth client-secret configuration and records
best-effort redacted MCP audit events internally, but OAuth mint/revoke and
request logging are not exposed as agent-callable MCP tools.

## Transcript Preview

Use the offline normalizer before designing any transcript hook or sweeper:

```bash
cd backend
uv run python ../scripts/normalize_agent_transcripts.py dry-run \
  --adapter codex \
  --tenant-id "$PALACEOFTRUTH_TENANT_ID" \
  --scope-type agent \
  --scope-key codex \
  /path/to/transcript.jsonl
```

The preview emits canonical memory-entry envelopes with stable source ids,
`source_file` metadata, role tags, scope data, idempotency keys, and privacy
classification. It redacts bodies by default, never connects to Palace, and does
not write memory. The command does not write memory even when it finds valid
records.

For local capture, use `sweep` first without `--write`:

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

Add `--write` only after `PALACEOFTRUTH_API_BASE_URL` and
`PALACEOFTRUTH_API_KEY` are configured. The hook command is silent by default,
never writes to stdout, and should use a PID lock file when installed in an
agent lifecycle hook. Neither command deletes or purges transcript files.

Transcript hooks are separate from lifecycle checkpoints. Use transcript hooks
only for reviewed local capture; use `capture_checkpoint` or
`palace_checkpoint` for resumable session summaries, decisions, validation, and
next steps.

## Verification

Dry-run before live use:

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

Live smoke is optional. It writes exactly one deterministic scoped memory,
polls it, optionally queues one bounded deferred relationship backfill,
retrieves it, checks wake-up context when available, and lists recent jobs.
