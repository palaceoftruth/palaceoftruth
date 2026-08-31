# Watched HTTP Source Drift Review

Palace turns each material watched HTTP source change into one tenant-scoped
Review Inbox proposal. The worker compares the last successful source record
with the new active record after indexing succeeds. It does not use an LLM and
does not apply a suggested content edit.

## Proposal contract

A `candidate_source_drift` artifact is created only when both successful source
records have different content hashes. It contains:

- `source_resource_id`, `previous_source_record_id`, and
  `current_source_record_id` as raw provenance pointers;
- `affected_item_ids` and any claim IDs whose support references either source
  record;
- a deterministic, bounded unified diff in `evidence_diff`;
- a tenant-scoped `dedupe_key` for the exact resource and version pair; and
- an immutable `source_drift_created` curation event.

Known secret assignments and private-key or private-transcript markers are
redacted before the diff is stored. Source text is never written to logs. A
missing tenant, watched resource, source record, source URI, item ownership row,
or chunk set aborts the refresh transaction instead of creating weak evidence.

The first successful version has no prior version and creates no proposal. An
HTTP `304`, a successful fetch with the same digest, or equal source-record
content hashes also creates no proposal.

## Review Inbox and API

`GET /api/v1/curation-artifacts/review-inbox` returns open proposals. Use
`include_deferred=true` to include deferred work and `include_resolved=true` to
include accepted or rejected source-drift proposals.

`POST /api/v1/curation-artifacts/review-inbox/actions` supports `accept`,
`reject`, `defer`, and `reopen`. Accept and reject require
`curation:approve`. Accept records approval of the proposal only. It does not
change an item, claim, source record, or remote HTTP source. `reopen` reverses
an accepted or rejected source-drift decision while preserving its provenance
and audit history.

All list, lookup, creation, and action queries use the authenticated tenant.
The database unique constraint on `(tenant_id, dedupe_key)` is the final retry
and concurrency guard.

## Operations

Privacy-safe structured log event names are:

- `source_drift_created` for a new proposal;
- `source_drift_no_change` when equal evidence creates no proposal;
- `source_drift_retry_deduplicated` for an idempotent retry;
- `source_drift_denied` for missing tenant or ACL provenance; and
- `source_drift_failed` when proposal creation rolls back the refresh.

Log fields contain tenant and database identifiers, counts, and the truncation
flag. They do not contain source text or diff content.

Migration `071_source_drift_artifacts` adds nullable provenance columns for
existing rows, JSON columns with empty safe defaults, the new artifact kind,
foreign keys, and the tenant-scoped unique dedupe constraint. Downgrade removes
only this slice. If drift proposals already exist, downgrade first preserves
their explicit provenance under `metadata.legacy_source_drift` and remaps them
to the existing `candidate_memory_reflection` kind so the older constraint can
be restored without deleting review evidence.

## Deferred phases

Slack routing, LLM-authored fixes, automatic edits, and automatic promotion are
not part of this slice. Folder, repository, S3, feed, YouTube, and browser
capture drift proposals are also deferred.
