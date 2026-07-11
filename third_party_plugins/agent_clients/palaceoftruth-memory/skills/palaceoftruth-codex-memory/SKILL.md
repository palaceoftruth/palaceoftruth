---
name: palaceoftruth-codex-memory
description: Palace-first memory routing protocol for Codex agents, including startup recall, scoped lookup, safe write-back, checkpoints, and local fallback behavior.
---

# Palace-First Codex Memory

Use this skill when a Codex session needs durable memory, prior project context,
handoff recall, or post-run write-back through Palace of Truth.

## Operating Contract

Palace is the primary Codex memory path when the Palace MCP server is available.
Local Codex memory files remain the secondary audit, rollback, and
source-citation path.

When configuring a new Codex environment, run the repo-owned setup verifier
first. Its default mode is dry-run and non-mutating:

```bash
uv run python scripts/setup_codex_palace_memory.py \
  --api-base-url https://api.palaceoftruth.test
```

Use `--live-smoke` only after `PALACEOFTRUTH_API_KEY` is available in the Codex
runtime environment. The live setup path writes exactly one scoped `agent/codex`
memory, disables relationship backfill, and avoids admin, retry, delete, and
cleanup operations.

Use `scripts/codex_session_lifecycle.py` when you need copyable dry-run payloads
for startup recall, checkpoint preview, or post-run write-back. The helper never
connects to Palace and never prints raw secrets or transcript bodies.
Packaged docs may render this lifecycle contract as `codex-session-lifecycle.md`.

Before broad repo exploration or external research, run a Palace-first lookup
unless the request is trivial and self-contained.

When the MCP server exposes Codex-friendly aliases, prefer them for the common
loop: `get_wakeup_context` for compact startup context, `palace_search` for route-aware
recall, `palace_remember` for concise durable write-back, and
`palace_checkpoint` for handoff or compaction checkpoints. These aliases route
through the same canonical tools and REST contract described below.

## Startup Recall

Use this sequence at the start of non-trivial work:

1. `whoami` when tenant identity or MCP configuration is uncertain.
2. `get_wakeup_context` for startup wake-up context, selected scope summaries,
   checkpoint pointers, and readiness warnings when available; use
   `palace_context` for legacy wake-up-plus-recent-memory shape, and
   `get_wakeup_brief` directly only when you need that single primitive.
3. `retrieve_agent_memory` with:
   - `agent_scope_key="codex"`
   - `workspace_scope_keys` containing stable repo or project keys, such as
     `palaceoftruth`
   - `session_scope_key` set to the current thread, run, task, or handoff id
     when available
   - `include_tenant_shared=true` only when shared tenant context is useful
   - `include_broad_corpus=false` until the task needs general corpus search

Use `context_budget_chars` and `display_limit` so recall stays compact enough
for the current task.

Use `palace_search` as the shorter alias for the same route-aware retrieval
shape. Keep `include_broad_corpus=false` until the task actually needs broad
same-tenant corpus recall.

## Focused Lookup

Use `retrieve_memory` when the scope is already known and the task needs a
query-based lookup inside one scope:

- `scope_type="agent"`, `scope_key="codex"` for durable Codex-wide operating
  memory.
- `scope_type="workspace"`, `scope_key="<repo-or-project>"` for repo or project
  memory.
- `scope_type="session"`, `scope_key="<thread-or-run-id>"` for one handoff or
  run.
- `scope_type="tenant_shared"` only for intentionally shared tenant memory.

Use `list_memory_entries` for deterministic recent scoped memory enumeration
when inventing a search query would be misleading.

If `retrieve_memory`, `retrieve_agent_memory`, or `palace_search` fails with an
HTTP 500, timeout, or other semantic-retrieval outage, do not treat all Palace
memory as unavailable. Degrade to read-only deterministic inspection:

1. Tell the user briefly that semantic retrieval is degraded and that you are
   continuing with scoped listing where possible.
2. Call `list_memory_scopes` if the relevant workspace or session scope is not
   already known.
3. Use `list_memory_entries` for known `agent`, `workspace`, `session`, or
   `tenant_shared` memory scopes. Prefer tight scope keys, tag filters, cursors,
   and low limits.
4. Use `list_items` only when the target is an ingested library item, webpage,
   document, or article rather than scoped agent memory.
5. Use `get_item_relationships` only for a specific visible item id when the
   relationship graph can answer the question without exposing broad raw
   content.
6. Keep outputs privacy-safe: summarize counts, IDs, titles, tags, scopes,
   timestamps, and short non-sensitive snippets only when needed. Do not dump
   broad raw memory bodies or private item content into chat.
7. When deterministic listing is not enough to answer accurately, stop and say
   what semantic retrieval failure blocked.

Tags are secondary filters, not access boundaries.

## Safe Write-Back

Use `create_memory_entry` for durable learning that should affect future Codex
runs. Keep entries concise and operational:

- Store decisions, stable conventions, verified outcomes, and non-sensitive
  handoff facts.
- Use `scope_type="agent"` and `scope_key="codex"` for Codex-wide memory.
- Use `scope_type="workspace"` with a stable repo key for project memory.
- Use `scope_type="session"` for one-off run handoffs.
- Use `relationship_policy="immediate"` for normal notes.
- Use `relationship_policy="deferred"` only for bulk imports that will
  explicitly call `backfill_deferred_relationships`.

Never store raw secrets, API keys, bearer tokens, client secrets, private
transcript text, sensitive user content, or unredacted credential locations.

Use `palace_remember` for concise durable write-back only when the adapter's
configured default scope is the desired target, or pass both `scope_type` and
`scope_key` explicitly. Calls without either complete destination return the
non-retryable `scope_not_configured` contract and do not write; scope-key-only
agent inference is unsupported. `tenant_shared` requires an explicit
`scope_type="tenant_shared"` request. The wrapper uses `source="codex"` and
`created_by_role="agent"` defaults, but it must not be treated as an
`agent/codex` scope override when a host such as Iris is configured for
`agent/iris`.

## Checkpoints

Use `capture_checkpoint` before handoff, compaction, or long-running session
boundaries when a future agent should resume from Palace memory.

Checkpoint bodies should contain concise state, decisions, changed files,
validation, caveats, and next steps. Do not pass raw transcripts or large logs.
Use dry-run preview when the payload might contain sensitive content.

If `PALACEOFTRUTH_MCP_CHECKPOINT_CAPTURE_DISABLED=true`, do not work around the
kill switch. Fall back to local handoff or project-manager memory instead.

Use `palace_checkpoint` as the shorter alias when the checkpoint should default
to `agent/codex` scope.

## Local Fallback

Fall back to local Codex memory files when:

- Palace MCP is unavailable or unauthenticated.
- Semantic retrieval is unavailable and deterministic Palace listing is not
  enough to answer the question.
- Scoped retrieval drifts outside the requested scope or uses broad fallback
  when exact scoped recall is required.
- The task requires exact source-line citations from imported local files.
- The user explicitly asks for local memory inspection.

When using fallback memory in a user-facing answer, say briefly that the fact is
from local fallback memory and may be stale if it was not verified live.

## Output

Do not produce a separate memory report unless asked. Apply the recalled context
to the work, then mention only the memory facts that affected a decision,
blocker, verification path, or handoff.
