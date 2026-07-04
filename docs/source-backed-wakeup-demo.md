# Source-Backed Wakeup for Agent Teams

Give a fresh agent scoped memory with source support, stale warnings, and safe
follow-up probes before it acts.

This is the first Palace wedge: before an agent plans, edits code, or follows an
old handoff, Palace shows which startup context is source-backed, generated but
unpromoted, stale or missing, or policy-limited. The page starts with the trust
moment instead of taxonomy, infrastructure, or future platform language.

## Quickstart

Run the sanitized offline demo from a clean checkout:

```bash
python3 scripts/demo_source_backed_wakeup.py
```

The demo uses only sanitized fixture data in
`fixtures/source_backed_wakeup_demo.json`. It does not connect to Palace, read
secrets, use production content, or mutate a database.

For a live MCP client, use `get_wakeup_context` after installing the packaged
MCP adapter:

```json
{
  "tool": "get_wakeup_context",
  "arguments": {
    "agent_scope_key": "codex",
    "workspace_key": "palaceoftruth",
    "session_key": "demo-session"
  }
}
```

MCP setup details live in the
[packaged agent-memory client README](../third_party_plugins/agent_clients/palaceoftruth-memory/README.md).

## Expected Output

The offline smoke prints the three operator-facing blocks a fresh agent should
see before choosing a safe next action:

```text
## Context Palace selected
- Release runbook source [source_backed]: Current deploy checks come from the reviewed release runbook.
  Safe use: Use as trusted context for release-check sequencing.
- Draft release-risk synthesis [generated_unpromoted]: Generated synthesis says the worker queue is probably the riskiest area.
  Safe use: Treat as a hypothesis and verify against source-backed context before acting.

## Trust warnings Palace found
- Old staging rollback note [stale_source]: source_record_stale
  Safe use: Do not follow this note until a current source-backed rollback path replaces it.
- Security agent private finding [policy_limited]: source_summary_policy_limited
  Safe use: Ask for an authorized summary instead of expanding private details.

## Safe next action
Plan from the source-backed runbook, verify the generated synthesis before using it, ignore the stale rollback note, and request an authorized summary for policy-limited private findings.
```

The fixture scan should also pass:

```text
- States found: generated_unpromoted, policy_limited, source_backed, stale_source
- Warning states found: stale_source
- Privacy check: passed with 2 sanitized source URL(s)
```

## What The Trust States Mean

`source_backed` means Palace found an active source record with source chunks.
Use it as the strongest startup context, while still checking whether the task
needs a newer or narrower source.

`curated_memory` means a human-curated memory exists without a source record.
It can be useful context, but it is not the same as source-backed evidence.

`generated_unpromoted` means the context came from generated synthesis, a wakeup
brief, a diary rollup, a routing manifest, or another generated artifact that
has not been promoted as source-backed. Treat it as a hypothesis.

`stale_source` means the source record is stale, failed, deleted, or superseded.
Do not follow it for operational decisions until a current source-backed item
replaces it.

`source_missing` means Palace can show the memory item, but it cannot show a
usable source record and chunks. Ask a follow-up question or look for a better
source before acting.

`policy_limited` means the agent is allowed to know that relevant context
exists, but not allowed to expand the private details. Ask for an authorized
summary instead of trying to bypass the policy boundary.

`unknown` means Palace cannot classify the trust state yet. Treat it as
untrusted startup context.

## Privacy Boundary

Wakeup context is intentionally compact. It should expose titles, summaries,
trust labels, warning codes, source pointers, and safe next probes. It should not
expose raw chunks, source previews, raw production content, secrets, or private
cross-agent details in the startup payload.

The privacy scan fails if the fixture includes common secret markers, raw
production-content markers, or non-`.test` source URLs.

## What Palace Does Not Trust Yet

The MVP does not claim full source-backed answers. It does not promote generated
synthesis automatically. It does not prove proposition-level claims, model a full
dependency graph, or invalidate every downstream artifact when a source changes.

Generated summaries are visible so an agent can handle them carefully, not so the
agent can treat them as authority.

## Operator Next Steps

1. Run the offline quickstart and confirm the three blocks match the expected
   output above.
2. Install or verify the packaged MCP adapter with the setup instructions linked
   above.
3. Call `get_wakeup_context` at session startup for the intended agent and
   workspace scope.
4. Treat `source_backed` context as the starting point, verify
   `generated_unpromoted` context, ignore or replace `stale_source` context, and
   request authorized summaries for `policy_limited` context.
5. Use `palace_search` or `retrieve_agent_memory` for a specific follow-up probe
   after startup context has narrowed the work.

## Local Tenant Story

The fixture includes a local demo tenant id,
`demo-tenant-source-backed-wakeup`, so the story can be explained without
requiring a live tenant. For a live local database demo, seed equivalent
sanitized records into a disposable tenant and compare the `get_wakeup_context`
response shape against the same four public trust states.

## Roadmap After The MVP Boundary

These are not part of the wakeup MVP:

1. Research the post-wakeup claims, promotion, and invalidation model.
2. Add the first source-backed claim support after that research gate.
3. Add an operator promotion flow for generated agent-memory artifacts.
4. Track synthesis runs and artifact dependencies for stale-source
   invalidation.
5. Expose answer audit only after claims and dependencies exist.

The research gate is captured in
[Post-Wakeup Claims, Promotion, And Invalidation Design](post-wakeup-claims-promotion-invalidation-design.md).
