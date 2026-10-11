# SAR-1406 checkpoint candidate — review only

This is not a release approval or a production canary receipt. Default behavior
is unchanged: `precompression_checkpoint_enabled` is absent/false and the
provider advertises API v1. No fleet configuration was changed.

## Supported upstream boundary

Official documentation:
https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin#pre-compress-checkpoints-fail-closed

Verified against upstream commit `c57331677d7fc46298a7159cf3a650853f0966c5`:

- `MemoryManager.on_pre_compress(messages, *, evidence_messages=None,
  require_checkpoint=False, checkpoint_api_version=2)` selects normalized evidence
  only for API v2 providers and propagates their required-mode failures.
- Provider signature is `on_pre_compress(messages, *, require_checkpoint=False)`.
- The opt-in file configuration `precompression_checkpoint_enabled: true`
  selects API v2 in this candidate. Enable it only in isolated acceptance tests
  until independent review and the scoped backend canary approve release.
- No upstream code, installed host, other worktree, or profile was changed.

## Actual durability boundary

`backend/app/services/memory.py`, `accept_memory_entry`, commits Item.raw_content,
MemoryEntry, and Job together before enqueue. A queued job is not itself proof.
The existing MCP checkpoint helper reads job status only; this candidate does not
reinterpret that status as a durable checkpoint.

The added GET `/api/v1/memory/jobs/{job_id}/checkpoint-receipt` independently reads
the committed canonical entry and source Item and checks tenant, pointers, scope,
idempotency key and deletion state. Its response binds the exact raw-body SHA-256
to the job/item/scope. Queue/index/enrichment completion is not required for raw
archive durability. Missing records or mismatches fail, regardless of job status.
Contained clients can read only their bound agent; delegated scope grants are
also enforced. No raw archive text is returned through the receipt endpoint.

## Evidence and handoff

The provider preserves exact direct user/assistant text with content evidence
IDs. Tools, tool_calls, system rows and marked compaction summaries are excluded.
Each row is content-keyed with tenant, session and scope, so identical retries and
shared rows in overlapping attempts reuse backend idempotency keys. Parallel
calls on one provider serialize; every attempt still reads the durable receipt.
No local accepted-response cache can bypass readback.

Assistant prose is a claim, never verified task state. A direct text row can
contain a JSON object with `checkpoint_handoff: 2` and string-list categories
`constraints`, `accepted_artifacts`, `failed_approaches`, and `remaining_work`.
These declarations carry source evidence IDs and `assistant_claimed` or
`user_asserted` status. `verified_state` remains empty without independent task
verification evidence; a supplied `verified_state` assertion cannot manufacture
proof. Ordinary prose remains unclassified direct evidence. No LLM invents a
handoff. Only fixed receipt labels and validated UUID pointers reach the summary
prompt, never raw archive text or tool content.

## Failure and resource limits

- Default optional hook does no writes.
- Opted-in API v2 never returns success after a failed archive or readback;
  it raises even when called in optional mode. The supported host decides whether
  to suppress that exception or block compression.
- Writable initialized session, tenant, scope and ready OAuth token are required.
  No token refresh or indefinite token-lock wait occurs inside the boundary.
- Subagent write suppression and the existing cron automatic-capture denylist
  remain effective. Session change during archiving blocks success.
- At most 16 direct rows and 24,000 body characters per row. Excess evidence
  blocks rather than truncates. This conservative limit is a usability constraint,
  not an archive-completeness exception.
- Each network operation is a disposable stdlib subprocess, synchronously waited
  and killed/reaped on its five-second wall-clock timeout. Credentials travel on
  stdin, not process arguments; stderr is discarded. No retry or Retry-After sleep.
  A 30-second hook budget is checked between rows and before success; at most the
  current POST/readback pair can extend a failed attempt beyond that budget.
  Lock admission has a ten-second limit included in the hook budget.
- A timed-out POST can already have committed at the backend. Compression still
  fails; retry uses the same key and requires fresh exact readback.

## Evidence and remaining gates

TDD: eleven initial product failures (missing hook) before implementation.
An earlier import/collection mistake was fixed before that run and is not product
failure evidence.

The focused fixtures cover raw-tool filtering, exact receipt mismatches, required
and optional failures, content retries/overlaps/restarted callers, parallel calls,
session switching, size rejection, unauthorized receipts, and no retry sleep.
A real stdlib child is exercised against a loopback mock server. A subprocess
loads the unmodified upstream ABC and MemoryManager with HOME/HERMES_HOME isolated;
no production provider initialization or live Palace call occurs.

Remaining release gates: independent review, real backend concurrency/commit and
correct-scope canary, SAR-1403 lifecycle integration, SAR-1404 approved host matrix,
and release/rollback work owned outside this task. Those gates are not claimed
complete by mocked backend fixtures. No merge, deploy, release bump, or Done.
