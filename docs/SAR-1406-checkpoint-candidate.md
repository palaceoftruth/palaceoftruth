# SAR-1406 checkpoint repair candidate — review only

This candidate is not a release approval, deployment, or production canary.
`precompression_checkpoint_enabled` defaults absent/false: API v1, no automatic
checkpoint writes. Literal `true` selects the tested API v2 contract. No fleet
configuration, installed host, core source, or sibling source tree was changed.

## Supported upstream boundary

Official documentation:
https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin#pre-compress-checkpoints-fail-closed

Unmodified upstream source tested: `c57331677d7fc46298a7159cf3a650853f0966c5`.

- `MemoryManager.on_pre_compress(messages, *, evidence_messages=None,
  require_checkpoint=False, checkpoint_api_version=2)` routes normalized evidence
  to v2 providers and propagates required-mode failures.
- Provider signature: `on_pre_compress(messages, *, require_checkpoint=False)`.
- `_direct_messages_for_pre_compress_memory` preserves source `message_uid` and
  `timestamp` while excluding tool/system/summary material. The official-host
  subprocess test exercises this real normalizer, MemoryManager, source metadata
  helpers and restart replay, without provider initialization or live API calls.
- `stamp_message_timestamp` records source append-time Unix seconds;
  `stamp_message_uid` creates the durable logical-event ID at first insertion.
  This checkpoint never calls either helper, invents source time, or mints IDs.
  Missing metadata fails safely. Restored metadata must come from durable source
  records, not from a retry-time adapter that stamps old text as a new event.

## Backend durable boundary

`accept_memory_entry` commits Item.raw_content, MemoryEntry and Job together
before enqueue. Queue acceptance/completion is not a checkpoint receipt.

GET `/api/v1/memory/jobs/{job_id}/checkpoint-receipt` independently reads committed
canonical records and checks tenant, pointers, scope, idempotency and deletion
state. It binds exact raw-body SHA-256 to the job/item/scope. Missing, foreign or
mismatched records fail, regardless of queue status. Contained-agent and delegated
read restrictions apply; the receipt does not return raw evidence text.

The repair acceptance test submits the actual provider payload through the real
FastAPI POST model, provenance/admission and canonical acceptance/replay algorithm.
A fresh mock read handle exposes committed records only. Two POSTs replay one
canonical archive and one job. No PostgreSQL/Redis/production initialization is
performed; this is not a real database concurrency canary.

## R1 and R5: stable source time and ordered occurrences

One fully validated manifest is one atomic canonical archive. It preserves all
included direct normalized source rows, their exact text, order, role, stable
source-event ID, source timestamp and source-linked evidence ID. Repeated text
with distinct source IDs remains distinct. Event identity does not depend on
window position. Overlapping windows retain common IDs; reordered windows have
different manifest keys. Exact retries/restarts reuse the complete manifest key
and still require a fresh durable receipt.

`created_at` means the latest included source-event time, not archive-operation
or retry time. Each source row retains its own time. Unix-second and timezone-aware
ISO source timestamps normalize to UTC; missing/nonfinite/invalid/naive values
fail before POST. Host absorbed-message identity witnesses remain recorded without
inventing separate constituent text. Duplicate logical IDs are rejected.

Every included row and declared category is validated before any write. At most
16 direct source rows and 24,000 characters for the whole manifest are allowed.
Excess rows, oversized bodies or a malformed later row cause no archive write;
the whole transcript must be preserved. No partial archive is called complete.

Assistant statements remain claims. Explicit JSON `checkpoint_handoff: 2` string
lists can categorize constraints, accepted_artifacts, failed_approaches and
remaining_work, but remain source-linked user assertions/assistant claims.
`verified_state` stays empty. Tool calls/output, system text and compressed
summaries never become instructions or verification. Only a fixed label and
validated literal archive UUID enter the summary handoff.

## R2 and R3: immutable ready state and bounded transport

- Snapshot cached session, tenant, canonical scope, base URL and ready auth values
  under bounded locks. No legacy scope/identity/header helper, whoami, refresh,
  retries or Retry-After sleep runs inside the hook.
- OAuth must already have a valid cached binding and token covering the remaining
  total budget. Cold/conflicting state fails safely. API-key scope declarations
  are still authorized by the backend; headers do not manufacture a grant.
- One 30-second deadline covers serialization locks, cached-state locks,
  prevalidation, native queue wait, quota lock, POST, GET and final snapshot check.
  Each child receives at most five seconds AND at most the remaining total budget.
  Deadline exhaustion blocks success. A timed-out POST might already have committed;
  only a same-key retry with exact fresh readback can claim durability.
- Disposable children are killed/reaped on timeout; credentials use stdin, not
  argv, and stderr is discarded. All redirects are rejected before following,
  including cross-origin and HTTPS downgrade. Real HTTP/TLS loopback tests prove
  the origin received authenticated requests and redirect targets received none.
- TLS verification remains enabled. `checkpoint_ca_file`, when operator-supplied,
  supplies a trusted CA file; the regression uses a temporary fixture CA. HTTP is
  accepted only for localhost/loopback fixtures, never for remote production hosts.

## R4: minimal native lifecycle integration contract

Tests load immutable source fixtures exported from these exact PR550 revisions:

- Review revision: `a368363dedf547da667c7b8eab32f3ecdd1f73ea`.
- Latest session-quota repair: `40b5bac85026ba754d410af973e0b09c6e4e9b64`.

Composition adds `CheckpointMixin` before the provider base and calls
`_init_checkpoint()` once after the native constructor. It does not replace native
policy, quota, queue, session-switch, flush or shutdown methods. This is an
explicit tested integration contract, not a merge into the sibling source tree.

Checkpoint admission checks automatic lifecycle suppression, cron suppression and
closed admission under the native lifecycle lock. Explicit cron palace_remember
remains allowed. Work enters pure local `_queue_write(worker, quota)` as one
native accepted/active/completed/failed task. For the latest revision, quota is
selected by `_quota_for_session(snapshot.session_id)`, never a stale current
bucket, and owner identity is checked. Turn/session limits use the admitted task's
captured quota under the remaining-time-bounded quota lock. Turn rotation cannot
charge a new bucket; an unrelated bucket cannot bypass an exhausted source cap.

Expired pending work never writes later and is counted as failed. Active work
remains visible to flush/shutdown. A drained barrier is NOT remote durability or
proof that writes succeeded; `shutdown_drain_state` separately reports failures
and timeouts. Successful checkpoint return requires its own exact durable receipt.
The legacy base has checkpoint close/drain accounting without a background writer.

## Verification and remaining gates

Original independent-review defect reproductions are preserved unchanged as test
input. Repaired acceptance fixtures cover R1-R5, real route/backend replay,
child timeouts/reaping, redirects, source-order identity, entire-manifest validation,
exact old/latest sibling composition, session-quota ownership and official host.
See `SAR-1406-repair-evidence.json` for red/green results and fixture hashes.

Release remains blocked on independent review, actual scoped backend persistence
and concurrency canary, SAR-1403/1404 integration/host matrix, and SAR-1407 rollout.
In particular, the parent host-drain requirement remains UNRESOLVED: native provider
accounting does not prove every real host exit invokes and respects that barrier.
No host/core changes are made here, and no merge/deploy/Done is claimed.
