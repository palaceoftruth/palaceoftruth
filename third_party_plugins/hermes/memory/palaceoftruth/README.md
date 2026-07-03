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
- The plugin validates its tenant key with `/api/v1/memory/whoami` and mirrors the returned `tenant_id` into durable write payloads.
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
- `palace_remember_bulk` writes up to 100 explicit memories through
  `/api/v1/memory/entries:batch` and returns ordered per-item results. Use it
  for intentional bulk saves, not as a local offline spool or replay queue.
  The local bulk-call quota prevents a single Hermes turn from looping this
  endpoint without an explicit operator override.
- Explicit memory tool writes over 24,000 characters are rejected with a clear
  error instead of being silently truncated. Automatic turn sync may still trim
  very long conversation bodies, but it records truncation metadata so operators
  can audit the stored body length.

Agent-visible search results:

- Every rendered `palace_search` or recall result must include the decisive
  match evidence beside the title and snippet: item id, Palace item API URL,
  scope, tags or matched tags, and score when Palace returned those fields.
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
