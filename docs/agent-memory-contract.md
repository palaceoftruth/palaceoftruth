# Agent memory contract

The memory facade is the stable write and read contract for agents. The MCP
adapter and the Hermes memory plugin both speak it.

## Endpoints

- `GET /api/v1/memory/whoami`
- `POST /api/v1/memory/entries`
- `POST /api/v1/memory/entries:batch`
- `GET /api/v1/memory/scopes`
- `POST /api/v1/memory/retrieve-agent`
- `POST /api/v1/memory/retrieve` as the single-scope path

## Scope headers

A raw API key client must send explicit scope headers on these routes. Read
routes send `X-MCP-Scope: read`. Write routes send `X-MCP-Scope: write` plus the
narrow grant, such as `write:agent`, `write:workspace`, or `write:session`, in
`X-MCP-Scopes`.

The headers narrow access. They do not grant it. Every API key carries a scope
grant stored on its `api_keys` row, and each request is authorized against the
intersection of that grant and the headers. A header can drop a scope for one
call. It can never add one the key does not hold, including `admin`. A key with
an empty stored grant is refused at authentication.

Set a key's grant with `scopes` on tenant registration or key rotation
(`POST /api/v1/admin/tenants` and the key-rotation route). Keys that predate the
grant field keep their previous privilege. Narrow one by rotating to a key with
an explicit `scopes` list.

## Durability contract

A `202` response means Palace accepted a durable item and job before it
enqueued background work. A client must persist `job_id`, poll `poll_url` after
`poll_after_seconds`, and read these fields before a retry:

- `contract_status`
- `retryable`
- `retry_after_seconds`
- `queue.state`

`queue.state=backpressure` or `saturated` is a retry or defer hint. It is not a
hard server-side rate limit. These response headers carry the same guidance for
HTTP clients:

- `Retry-After`
- `X-Palace-Memory-Queue-State`
- `X-Palace-Memory-Poll-After`
- `X-Palace-Rate-Limit-State`

When an enqueue dependency is unavailable, Palace returns `503` with
`contract_status=dependency_unavailable`, the accepted `job_id`, `poll_url`, and
`Retry-After`. The client can poll or retry the accepted job without rewriting
the memory body.

## Batch writes

A client can send up to 100 canonical memory entries to
`POST /api/v1/memory/entries:batch`. The batch endpoint applies the single-entry
tenant, admission, idempotency, queue, and retry contract to each entry. It
returns ordered per-item results with `index`, `status`, `contract_status`,
`job_id`, `poll_url`, `retryable`, and sanitized error details when an entry is
rejected.

For a deterministic landing check, write the batch, poll the accepted jobs, then
verify the imported records with exact `source_url` filtering on
`GET /api/v1/items?source_url=<encoded-url>`. Item listings support `page` and
`per_page`. A created-at sorted audit scan can follow the returned
`next_cursor` with `GET /api/v1/items?cursor=<cursor>&sort=created_at`.

## Session startup

Use `get_wakeup_context` when an agent needs one compact package at session
start. It returns wake-up status, selected agent, workspace, and session memory
summaries, checkpoint pointers, promoted source-backed decision claims,
readiness warnings, and safe follow-up probes.

Only active, operator-promoted claims with current exact source support appear
as startup guidance. Stale, conflicted, missing, policy-limited, draft, and
rejected claims stay warning-only. The payload carries claim text and source
pointers. It never carries source bodies, chunks, previews, or spans.

Use `palace_search` or `retrieve_agent_memory` for a specific question after
wake-up. Use `capture_checkpoint` only when you write a reviewed handoff or a
compaction checkpoint.

## Health and readiness

- `GET /api/v1/health`: simple Kubernetes-compatible probe, returns
  `{"status":"ok"}`
- `GET /api/v1/ready`: dependency-aware readiness
- `GET /api/v1/version`: deployed application version

## Worker health

Worker health has three separate signals. Kubernetes process supervision is the
liveness signal. `wait_for_worker_dependencies.py` is the startup gate for the
database and the Valkey or Sentinel dependency. After startup, each worker
deployment uses the ARQ read-only `--check` command as its readiness probe,
backed by a 15-second pod-specific queue heartbeat, so a sibling replica cannot
satisfy the probe.

Prometheus aggregates the heartbeats by bounded queue group and exports
`palace_arq_worker_available`, `palace_arq_worker_instances`, and
`palace_arq_worker_heartbeat_age_seconds`. A missing heartbeat means worker
availability is unknown or unavailable. A zero queue depth can still be a
healthy idle worker.
