## Hermes Memory Plugin

This directory is the source of truth for the Hermes `palaceoftruth` memory plugin.

Current deployment reality:

- Some Hermes deployments still bake a vendored copy of this plugin into a custom runtime image.
- Example deployments can vendor it under `starter/plugins/memory/palaceoftruth` in the deployment repo.
- The vendored copy should be updated by sync, not by making unrelated logic changes only in a deployment repo.

Why this lives here:

- The plugin implements the Hermes-facing contract for Palace of Truth.
- Palace of Truth developers need to be able to evolve that integration without hand-editing the deployment repo first.
- Keeping the canonical plugin here lets this repo own the integration semantics while deployment repos own runtime image assembly and Kubernetes deployment.

## Agent Write Contract

For a normal single-memory agent write, use MCP `palace_remember` with an
explicit scope and deterministic idempotency key. Use `agent/orchestrator` for
orchestrator-owned learning, `agent/<stable-agent-key>` for a named Hermes
agent, `workspace/<stable-project-key>` for a project outcome, and
`session/<thread-or-run-id>` for a resumable run. `tenant_shared` is an
intentional publication only. Use `palace_checkpoint` for handoff or
compaction; reserve `create_memory_entry` for import, bulk/programmatic,
compatibility, protocol, or advanced-field paths.

MCP encapsulates configured transport authentication; it does not bypass OAuth.
Raw REST is for operator integration/authentication diagnostics only, never an
automatic agent fallback. If MCP is unavailable or unauthenticated, record a
local `deferred` outcome with the non-secret error. Accepted or queued writes
are not durable until terminal completion and exact-scope retrieval. A
subagent returns evidence and a proposed capture payload unless direct
write-back is explicitly delegated.
- The plugin can authenticate with either the legacy tenant API key or Palace MCP OAuth client credentials. When
  `PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET` is present, OAuth is preferred and API calls use a bearer token minted from
  `/api/v1/memory/mcp/oauth/token`; `PALACEOFTRUTH_API_KEY` remains a legacy fallback for hosts that have not cut over.
- The plugin validates its tenant identity with `/api/v1/memory/whoami` and mirrors the returned `tenant_id` into durable write payloads.
- Recall is route-aware: the plugin first asks `/api/v1/memory/scopes` for
  content-free scope metadata, then uses `/api/v1/memory/retrieve-agent` to
  search its own agent scope, discovered workspace scopes, `tenant_shared`, and
  the broad non-private corpus. If the new route is unavailable, it falls back
  to the older per-scope `/api/v1/memory/retrieve` loop.
- Route-aware recall uses separate budgets for selected-scope candidates,
  broad-corpus candidates, final display count, and rendered context characters:
  `PALACEOFTRUTH_AGENT_CANDIDATE_LIMIT`,
  `PALACEOFTRUTH_AGENT_BROAD_CANDIDATE_LIMIT`,
  `PALACEOFTRUTH_AGENT_DISPLAY_LIMIT`, and
  `PALACEOFTRUTH_CONTEXT_BUDGET_CHARS`.
- `palace_semantic_recall` exposes the strict-scope semantic memory route
  `/api/v1/memory/semantic-recall` for Hermes agents that need source-backed
  temporal recall. It defaults to the active configured Hermes scope and
  supports `valid_at`, `fact_kind_filter`, `top_k`, `candidate_limit`,
  `score_threshold`, `recall_max_tokens`, `context_budget_chars`, `date_from`,
  and `date_to`. Older Palace servers that do not yet expose the route fall
  back to the existing route-aware recall path.
- Hermes pre-turn recall remains route-aware by default. Set
  `PALACEOFTRUTH_SEMANTIC_PREFETCH_ENABLED=true` to make the pre-turn
  `prefetch()` hook call strict-scope semantic recall instead. The explicit
  `palace_semantic_recall` tool remains available regardless of this setting.
- Semantic pre-turn recall uses separate operator knobs from route-aware recall:
  `PALACEOFTRUTH_SEMANTIC_PREFETCH_TOP_K` (default `5`),
  `PALACEOFTRUTH_SEMANTIC_PREFETCH_CANDIDATE_LIMIT` (default `20`),
  `PALACEOFTRUTH_SEMANTIC_PREFETCH_RECALL_MAX_TOKENS` (default `1200`), and
  `PALACEOFTRUTH_SEMANTIC_PREFETCH_CONTEXT_BUDGET_CHARS` (default `4000`).
  Empty pre-turn recall is always audit-logged. The active scope profile's
  `quiet_recall=true` only suppresses the outward empty-recall block; it does
  not suppress audit traces.
- Semantic pre-turn recall is strict to the active Hermes scope. It does not
  expose sibling-agent semantic memory through pre-turn context, and the
  rendered block keeps provenance compact instead of dumping raw chunks.
  If `/api/v1/memory/semantic-recall` is unavailable, semantic pre-turn recall
  fails closed for that turn instead of falling back to broader route-aware
  recall. Use the explicit `palace_semantic_recall` tool when an operator wants
  an older-server compatibility fallback.
- Delegated cross-agent recall remains opt-in. Set
  `PALACEOFTRUTH_INCLUDE_AGENT_SCOPE_PATTERNS=agent/*` with
  `PALACEOFTRUTH_AGENT_SCOPE_PATTERN_LIMIT` to ask Palace to discover matching
  agent scopes, select a bounded subset, and authorize those selected scopes
  server-side before searching them.
- Palace API calls use bounded retries for transient failures only: network
  errors, HTTP 429, and retryable 5xx responses. Permanent 4xx responses,
  validation failures, tenant mismatch, and privacy/admission rejections are not
  retried.
- Retry and circuit-breaker knobs are local to this plugin:
  `PALACEOFTRUTH_RETRY_ATTEMPTS` (default `3`),
  `PALACEOFTRUTH_RETRY_BACKOFF_SECONDS` (default `1.0`),
  `PALACEOFTRUTH_CIRCUIT_FAILURE_THRESHOLD` (default `3`), and
  `PALACEOFTRUTH_CIRCUIT_COOLDOWN_SECONDS` (default `30`). `Retry-After` is
  honored when Palace sends it.
- Client-side write guardrails are enabled by default for new installs:
  `PALACEOFTRUTH_WRITE_QUOTAS_ENABLED` (default `true`),
  `PALACEOFTRUTH_MAX_WRITES_PER_TURN` (default `5`),
  `PALACEOFTRUTH_MAX_WRITES_PER_SESSION` (default `100`),
  `PALACEOFTRUTH_MAX_BULK_CALLS_PER_TURN` (default `2`), and
  `PALACEOFTRUTH_DEDUP_CACHE_TTL_SECONDS` (default `300`). Existing deployments
  that need temporary compatibility can explicitly opt out by setting
  `PALACEOFTRUTH_WRITE_QUOTAS_ENABLED=false`.
- `palace_remember` reports write contract status honestly. A successful tool
  call can still mean accepted or queued rather than durable; inspect the
  returned `durability`, `job_id`, `poll_url`, `poll_after_seconds`, and retry
  hints before claiming the memory is fully persisted.
- `palace_memory_job_status` polls one returned memory job without retrying it,
  while `palace_exact_scope_recall` queries only the active configured scope.
  Together they support write -> terminal job -> exact-scope recall canaries
  without widening recall or using raw REST.
- `palace_remember_bulk` writes up to 100 explicit memories through
  `/api/v1/memory/entries:batch` and returns ordered per-item results. Use it
  for intentional bulk saves, not as a local offline spool or replay queue.
  The local bulk-call quota prevents a single Hermes turn from looping this
  endpoint without an explicit operator override.
- `palace_remember` and `palace_remember_bulk` accept temporal retention fields
  (`valid_from`, `valid_until`, `supersedes_entry_id`, `fact_kind`) plus
  explicit enrichment controls (`enable_ai_enrichment`, `relationship_policy`).
  Bulk writes remain backward compatible with a list of strings and can also
  accept per-entry objects with those fields.
- Explicit memory tool writes over 24,000 characters are rejected with a clear
  error instead of being silently truncated. Automatic turn sync may still trim
  very long conversation bodies, but it records truncation metadata so operators
  can audit the stored body length.

Agent-visible search results:

- Every rendered `palace_search` or recall result must include the decisive
  match evidence beside the title and snippet: item id, Palace item API URL,
  scope, tags or matched tags, and score when Palace returned those fields.
- Every rendered `palace_semantic_recall` result must include citeable
  provenance: entry id, source item id, Palace item API URL, scope, source,
  validity window, temporal status, fact kind, tags, and score when Palace
  returned those fields.
- Keep tags visible even when the title is generic. A generic title such as
  `Memory` can be a correct match when `tags` or `matched_tags` include the
  user's requested handle, project id, or regression key.
- Keep snippets bounded to the existing formatter limits. The evidence line is
  for routing and auditability, not a reason to print full memory bodies or
  request headers.

No-memory answer guardrail:

- A Hermes agent must not answer that Palace has no matching memory unless it
  called `palace_search` for the user's query in that turn.
- If `palace_search` was unavailable, timed out, or returned an explicit error,
  the answer should say that Palace search was unavailable instead of presenting
  the absence as a confirmed memory result.
- The plugin's `system_prompt_block()` carries this rule for runtime consumers,
  and `backend/tests/test_hermes_memory_plugin.py` locks the prompt text so a
  future plugin edit does not silently remove it.
- This repo owns the canonical plugin contract and package. It cannot guarantee
  behavior for Hermes runtimes that do not consume this plugin version or that
  override the plugin prompt after load; deployment consumers must sync or pin
  the packaged artifact.

Sanitized regression note:

- In a prior Hermes incident, a matching Palace memory existed, but the first
  query used a close variant of the identifier and the session answered that
  memory had nothing without a recorded `palace_search` call. That is a
  no-memory-answer guardrail failure.
- A separate hidden-match failure was already covered by the result-evidence
  formatter tests: a successful `palace_search` result with a generic title such
  as `Memory` must still expose item id, item URL, scope, tags, and score so the
  agent can trust tag-only matches.

Troubleshooting a hidden-match regression:

```bash
cd backend
uv run pytest tests/test_hermes_memory_plugin.py tests/test_agent_plugin_manifests.py
```

Checking downstream runtime sync is deployment-specific. Keep those verifier
scripts and private runtime paths in the deployment repository that consumes the
published plugin artifact.

If Hermes searched Palace but answered that it found nothing, inspect the
agent-visible `palace_search` text first. The result should expose enough
evidence for a model to trust a tag-only or generic-title hit, for example the
item id or `/api/v1/items/<item_id>` URL plus the matched tag.

Troubleshooting a no-memory-answer regression:

1. Inspect the transcript or tool trace for the exact user query.
2. Confirm that the turn includes a `palace_search` call using that query or a
   faithful normalization of it.
3. If no call exists, treat the answer as invalid even when the final text says
   Palace had no memory.
4. If the call exists but failed, verify that the final answer names Palace
   search as unavailable or failed.
5. If the call exists and succeeded, inspect the rendered `palace_search` result
   for evidence lines before debugging model reasoning.

Current packaging boundary:

- This directory is published as a tiny container artifact for deployment consumers.
- Artifact repository: `ghcr.io/palaceoftruth/palaceoftruth/hermes-memory-plugin`
- Deployment consumers should pin the artifact by digest, then copy `/plugin/` into the Hermes runtime image.
- This directory remains the canonical source unless there is an explicit migration.

Release process:

- `plugin.yaml` is the release contract. Bump its `version` whenever the plugin runtime or container contract changes.
- On every merge to `main` that touches a packaged plugin file or the packaging
  script, CI packages the plugin and creates a GitHub release tagged
  `hermes-memory-plugin-v<version>`.
- Release assets include `.tar.gz`, `.zip`, metadata JSON, and a checksum file.
- The `.json` asset is the canonical update manifest. Installers should pin
  the `.json` manifest and archive assets by SHA-256 digest, compare declared
  file sizes before unpacking, and reject any archive member outside the
  declared package root.
- The manifest reserves signature, provenance, and rollback fields even before
  release signing exists. Treat missing signature entries as unsigned, not as a
  successful signature verification.
- The Hermes package version comes from this directory's `plugin.yaml`. The
  Codex/Claude client package under
  `third_party_plugins/agent_clients/palaceoftruth-memory` has its own manifest
  version and does not imply a Hermes runtime package update unless the Hermes
  manifest or packaged Hermes files change.
- The same CI run also publishes the matching container image tag:
  `ghcr.io/palaceoftruth/palaceoftruth/hermes-memory-plugin:<commit-sha>`
