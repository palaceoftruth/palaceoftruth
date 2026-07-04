# SAR-946: Duplicate Palace Memory Write 409 Contract

Date: 2026-07-04

Task: [SAR-946](https://linear.app/sarvent/issue/SAR-946/research-duplicate-palace-memory-write-409-handling)

## TLDR

Palace should treat an idempotent duplicate memory write as a replay, not as an ordinary failure, when the same tenant, idempotency key, and equivalent memory payload point at an existing write. The API/MCP contract should return or expose the existing job/item pointer for that replay. A duplicate with a reused key but different payload or scope should stay a real conflict and must not be normalized into success.

Codex/DOTODO automation should continue its current safe behavior of not retrying with rewritten memory content after `409 Memory entry already exists`. The missing product contract is richer server/client metadata: future runs need a stable way to record `already_present` with the existing `job_id`, `source_item_id`, scope, and idempotency key when Palace can safely reveal them.

## Trigger Evidence

SAR-932 landing exposed the problem:

- PR: https://github.com/palaceoftruth/palaceoftruth/pull/83
- Landing artifact: `/Users/asarver/.codex/automation-artifacts/dotodo-palace-of-truth/approvals/20260704T0224Z-SAR-932-landing.md`
- Existing implementation memory source item: `57c3b195-4dee-4eb7-88d2-dd87bff39a62`
- Post-landing write result: Palace API returned `409` for `POST /api/v1/memory/entries` with `Memory entry already exists`
- Landing behavior: recorded `duplicate_rejected`; did not retry with rephrased memory

The same pattern recurred in later DOTODO landings:

- SAR-966 implementation memory job `d4fad6e1-c5ba-4bfa-82bc-17d9f7492956`, idempotency key `dotodo-sar966-pr99-c588abf`; post-landing Palace write returned duplicate `409` and was recorded as `duplicate_rejected`.
- SAR-934 implementation memory job `a80d607e-b962-45fb-98fe-22c13d942e78`, idempotency key `dotodo-sar934-pr101-fa77666`; post-landing Palace write returned duplicate `409` and was recorded as already captured.

## Current Palace Behavior

The canonical memory write route accepts `POST /api/v1/memory/entries` and returns a `202` durability contract when the write is accepted. The route validates tenant and admission policy, calls `accept_canonical_memory_entry`, and returns `job_id`, `poll_url`, `contract_status`, retry metadata, scope, and queue hints.

Current duplicate handling is job-first:

- `accept_memory_entry` searches for an existing same-tenant memory job by `Job.payload["idempotency_key"]`.
- If found, Palace returns the existing job and only requeues if stale or retryable.
- If an insert races the unique item idempotency constraint, Palace rolls back and checks for the job again.
- If the item conflict exists but no matching job can be found, Palace raises `409` with free-form detail `Memory entry already exists`.

Important gaps:

- The successful replay path does not explicitly mark the response as a replay or duplicate.
- The last-resort `409` does not include structured fields such as `code`, `existing_job_id`, `existing_source_item_id`, `scope`, `idempotency_key`, or `retryable`.
- `palace_remember` forwards to `create_memory_entry` but does not add a default idempotency key.
- Auto-derived canonical keys include `created_at`, so otherwise equivalent client writes with different timestamps are not deduplicated unless callers supply a stable idempotency key.
- Current client scripts often treat terminal job status `duplicate` as success, but write-time duplicate classification is not typed.

## External Contract Evidence

The IETF Idempotency-Key draft says clients must not reuse an idempotency key with a different payload, servers should document their idempotency policy, completed duplicate retries should return the previous operation result, concurrent duplicate retries can be `409`, and different-payload key reuse can be an error such as `422`.

MDN summarizes the same pattern: clients should reuse the same key for a resend of the same request; servers should respond as though the request had already been processed, while fingerprint mismatch can be an error.

Stripe's public guidance reinforces client behavior: after an indeterminate network failure, retry with the same idempotency key and same parameters; when modifying the request, use a fresh key.

Increase's API docs are the closest fit for Palace's desired client ergonomics: same arguments plus same idempotency key return the created object with an idempotent replay marker; a different object with the same key returns `409` and includes the associated resource id.

## Recommended Palace Contract

Use these terms:

- `idempotent_replay`: same tenant, same idempotency key, same memory identity or payload fingerprint, and existing job/item found.
- `idempotency_conflict`: same tenant and idempotency key, but the submitted payload or scope differs from the stored fingerprint.
- `ordinary_duplicate_conflict`: a duplicate item/source conflict not proven to be an idempotent replay.
- `concurrent_replay`: same key while the first matching job is still queued or processing.

For `POST /api/v1/memory/entries`:

- First accepted write: continue returning `202` with the current durability contract.
- Safe replay with existing job/item: return the same durable acceptance shape, add `replay_status: "idempotent_replay"` or `duplicate: true`, and include the existing `job_id`, `source_item_id` when available, `scope`, `idempotency_key`, `poll_url`, and current `contract_status`.
- Concurrent replay: return `202` with the existing queued/processing job when Palace can safely identify it, or structured `409` with `code: "memory_write_in_progress"` and retry/poll metadata when returning the existing job would be unsafe.
- Same key with different payload or scope: return structured error metadata such as `code: "idempotency_conflict"`, `idempotency_key`, `scope`, `existing_job_id`, `existing_source_item_id`, `conflict_fields`, and `retryable: false`.
- Ordinary duplicate without an idempotency match: return structured `409` with `code: "memory_entry_already_exists"` and a safe existing-resource pointer when authorized.

The response should never require clients to parse `Memory entry already exists` text.

## Recommended MCP Behavior

`create_memory_entry` and `palace_remember` should expose duplicate semantics directly:

- Safe replay should return a typed success result, not an exception.
- The result should preserve `job_id`, `source_item_id` if known, `idempotency_key`, `scope`, `replay_status`, and `contract_status`.
- True conflicts should remain typed errors with the structured server metadata intact.
- `capture_checkpoint` should keep its stable checkpoint idempotency key behavior and surface read-after-write/read-after-replay job metadata when available.
- A `read_after_duplicate` or `read_after_replay` option is useful only after the API returns enough safe metadata to locate the existing item or job.

## Recommended DOTODO Automation Behavior

Implementation memory and landing memory should use different deterministic idempotency-key namespaces:

- Implementation memory: include task id, PR number, and implementation commit.
- Landing memory: include task id, PR number, landing action, merge commit, and deployed chart/app version when applicable.

Automation should classify results as:

- `written`: a new memory write was accepted.
- `already_present`: Palace returned a typed idempotent replay with an existing pointer.
- `duplicate_rejected`: Palace returned an untyped duplicate `409`; record the raw safe detail and do not retry with rewritten body.
- `blocked`: Palace returned a structured conflict showing key reuse with materially different payload/scope, or Palace is unavailable and the run requires memory capture before approval.
- `deferred`: Palace MCP/API is unavailable or unauthenticated and the automation contract allows memory to be recorded locally for later capture.

DOTODO run memory should store:

- task id(s)
- PR URL and branch
- implementation/landing phase
- idempotency key
- Palace result classification
- existing `job_id` and `source_item_id` when available
- scope type/key
- error code and retryable flag for non-success results
- local artifact path if capture failed or was deferred

Automation must not bypass a duplicate by changing the memory body, title, or randomizing the key. That turns a replay into a second memory, which is exactly what idempotency is meant to prevent.

## Follow-Up Implementation Work

This research recommends implementation follow-ups rather than changing runtime behavior in this research PR:

1. Specify and test a typed duplicate/replay response contract for `POST /api/v1/memory/entries`.
2. Add request fingerprint storage/comparison for canonical memory writes so same-key/different-payload conflicts are distinguishable from safe replays.
3. Update MCP `create_memory_entry` / `palace_remember` to expose typed replay success and structured conflict errors.
4. Update Codex/DOTODO memory capture instructions/helpers to log `already_present`, `duplicate_rejected`, `blocked`, or `deferred` with the fields above.

## Caveats

- Returning an existing pointer is only safe inside the caller's authorized tenant/scope. Do not leak existence of memory entries across scope boundaries.
- A `303 See Other` can be appropriate for generic HTTP APIs, but Palace's current client contract is already built around `202` durable jobs and polling. Extending the existing acceptance response is less disruptive than redirecting agent clients.
- This research did not mutate production data and did not change API behavior.
