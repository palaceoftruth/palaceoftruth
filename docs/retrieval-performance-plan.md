# Retrieval performance and verification

## Search behavior

Small room-filtered searches use an eligibility-first candidate path. It starts
from indexed room memberships, applies the same tenant, scope, item, tag, date,
ready/deleted and currentness predicates as ordinary search, then reads each
eligible item's embeddings through the existing item/profile indexes.

Both semantic and keyword candidate lanes use the eligible item set. Correlated
embedding lookups prevent the keyword existence check from scanning and grouping
an entire embedding corpus before selective filters apply. Candidate and display
limits, ranking, source provenance, and deduplication remain in effect.

A bounded strategy probe checks at most eight requested rooms, 513 distinct
room-member items, and 4,097 matching-profile chunks. Requests above 512 items
or 4,096 chunks retain the standard query path, as do broad requests and requests
with per-scope candidate caps. These values select an execution strategy, not a
truncated result set. The main query rereads current room memberships; it does
not use a stale list of IDs from the probe. Concurrent membership growth can
increase the work of the selected strategy but cannot silently omit new members.

The existing default and supported profile-specific embedding tables are used.
No index migration or global PostgreSQL planner setting is required.

Set `RETRIEVAL_SELECTIVE_ROOM_SEARCH_ENABLED=false` to restore the standard
candidate query path. The default is `true`. Tenant row-level security remains
enabled in both paths.

## Shared recall

When selected rooms return weak results, shared recall can perform one quality
rescue within `tenant_shared`, with no scope key. It reuses the query embedding.
It does not request private agent/workspace scopes or an unrequested broad
corpus. Explicit-room and explicit-tag behavior remains unchanged. A rescue
attempt does not guarantee that relevant evidence exists.

## Timing

Retrieval traces contain additive `stage_timings_ms` and `search_attempts`
fields. Stage timings measure room loading, routing, embedding work, result
handling, and total service time. Each search attempt records its bounded reason,
outcome, and elapsed time. Search-service timing includes more than database SQL;
SQL timings and the selected candidate strategy are reported separately.

Timers use a monotonic clock. Nested stages are included in the total and must
not be summed as independent work. Failure logs contain bounded stage/outcome
information and elapsed time, not query text, SQL parameters, or memory content.
Existing response fields remain compatible.

## Repeatable benchmarks

The live benchmark accepts `--warmup`, `--repeat`, and `--concurrency`. Defaults
preserve one serial measured request per case with no warmups. Use synthetic or
approved evaluation packs. Remote endpoints require the existing explicit opt-in.

```bash
python scripts/benchmark_agent_memory_retrieval.py live-report \
  --pack path/to/synthetic-pack.json \
  --base-url http://localhost:8000 \
  --warmup 3 --repeat 30 --concurrency 1 \
  --output benchmark.json
```

Use the existing environment-based authentication options; do not put secrets
in a shared command or artifact. Warmups are reported separately. Measured
successes, errors, and timeouts remain visible in sample counts and percentile
reports. Any request error causes a nonzero exit. Compare baseline and candidate
requests at identical query, scope, candidate, display, and concurrency budgets.
Single samples and warm `EXPLAIN ANALYZE` timings are not end-to-end performance
claims. Small samples do not support a reliable p99 estimate.

## Verification contract

| Area | Required proof |
|---|---|
| Scope and tenant isolation | Restricted database role with forced RLS; no private or cross-tenant results |
| Eligibility | Ready/deleted, date/tag, current/expired/superseded, and source-status filtering |
| Candidate parity | Default/profile paths, limits, deduplication, and exact eligible references |
| Work bounds | Selective plans avoid whole embedding scans and full embedding-ID aggregation; broad/high-fanout requests retain the standard path |
| Shared rescue | One same-scope quality rescue, reused embedding, no private widening |
| Diagnostics | Nonnegative stage/attempt timings and sanitized failures |
| Benchmark | Warmup exclusion, unsorted percentile inputs, concurrency bounds, errors and defaults |
| Publication | Synthetic fixtures only; no operator captures, credentials, hostnames, or memory content in the patch |

Run the focused tests and then the backend suite using the worktree's frozen
Python dependencies. Database query-plan tests require
`PLAN_GATE_DATABASE_URL` pointing to a disposable PostgreSQL database with
pgvector. They create test tables/roles; never point them at a live service's
database.

```bash
cd backend
uv sync --frozen --python 3.12
uv run --frozen python -m pytest tests/test_search.py tests/test_palace_service.py \
  tests/test_retrieval_benchmark_sampling.py -q
# Set PLAN_GATE_DATABASE_URL to a disposable local test database first.
uv run --frozen python -m pytest tests/test_retrieval_query_plans.py -q
```

Before deployment, verify candidate-source behavior against a disposable database
and complete required CI and independent SQL/security review. At rollout, verify
the deployed revision, isolation/recall smoke cases, timeout rate, and latency at
representative load. Revert the application change or disable the selective query
path if it regresses behavior. No data deletion is required for rollback.

## Follow-up decisions

Routing caches, embedding caches, and global database tuning remain conditional
on measured residual cost. This change does not establish production latency
percentiles. Evaluate those optimizations only after the query work is bounded
and useful recall is verified.
