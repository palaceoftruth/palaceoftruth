# SAR-1034 Semantic Memory v1 Spec Cleanup

Date: 2026-07-09

## Decision

Palace-native Hermes semantic memory v1 keeps the handoff's implementation
sequence, but normalizes the acceptance gates before schema work starts:

1. Retrieval contract and fixture plan.
2. Scope profile persistence for `retain_mission` and `quiet_recall`.
3. Temporal memory-entry schema and metadata backfill.
4. Strict semantic recall with `valid_at` and supersession ordering.
5. REST, MCP, and Hermes plugin exposure.
6. Mission-steered direct-write retention.
7. Hermes wake-up/pre-turn integration.
8. Eval gates and Iris end-to-end canary.
9. Deploy and canary.

The source handoff says "12 categories / 10 ship gates" but its matrix has 13
rows and 11 v1 `Yes` rows. The canonical contract is:

- 10 v1 ship gates.
- 2 v1.5 hardening gates.
- Reflection and promotion-gated consolidation are v2.

## Canonical V1 Gates

| # | Gate | Requirement |
|---|---|---|
| 1 | Strict scope isolation | `agent/iris` recall must never return sibling agent entries, even for perfect embedding matches. |
| 2 | Provenance presence and typing | Each recall hit includes well-typed `entry_id`, `scope_type`, `scope_key`, `source`, `created_at`, `valid_from`, and `score`. |
| 3 | Identity and sovereignty | Writes and reads preserve the caller's intended scope; cross-agent writes require explicit server-side grants. |
| 4 | Mission honored | `retain_mission` suppresses irrelevant greetings and preserves SAR/action facts with expected tags. |
| 5 | Empty recall contract | Empty recall returns HTTP 200 with `items: []`, `total_considered`, `scope`, and trace metadata; no fabrication or refusal. |
| 6 | Source-vs-summary preference | Source-backed/raw entries outrank generated summaries for source-sensitive questions. |
| 7 | Workspace collision | Workspace-scoped recall never leaks private agent scope entries, and agent recall does not silently broaden into unrelated workspaces. |
| 8 | Multi-source aggregation | A recall answer can cite multiple source ids from the requested scope without losing per-hit provenance. |
| 9 | Budget containment | `top_k`/`display_limit` and `recall_max_tokens`/`context_budget_chars` cap output size deterministically. |
| 10 | Stale/current temporal | Current facts outrank superseded facts by default; `valid_at` returns the fact valid at the requested time. |

## V1.5 Deferrals

- Temporal edge cases: future `valid_from`, `valid_until < valid_from`,
  dangling `supersedes`, supersession cycles, and missing reverse lineage.
- Load and cost budgets: p95 recall and retain latency under 10k entries per
  scope, queue depth/backpressure behavior, and operator metrics.

These are deferred because the v1 can be correct and useful without proving
pathological temporal inputs or sustained-load targets. They must land before a
broader v1.5 rollout.

## V2 Deferral

Reflection is out of v1. The v1 retention path is direct-write with a careful
mission prompt, but it must be shaped so a later `RetentionService` can return
candidate observations, confidence, and promotion metadata without changing the
public recall contract.

## API Vocabulary

Use `valid_until` for semantic memory entry APIs, fixtures, and MCP tool
schemas. Existing Palace temporal fact code uses `valid_to`; implementation
tasks may map `valid_until` to existing internal vocabulary when necessary, but
the semantic memory contract should expose `valid_until`.

Use `valid_at` for recall-time historical filtering.

Use `fact_kind` with these initial values:

- `world`: a fact about external state.
- `experience`: an agent action or operational experience.
- `observation`: a third-party claim preserved for review.

Use `retain_mission` and `quiet_recall` as per-scope profile fields, not ad hoc
entry metadata. Missions must be updateable without rewriting stored memories.

## Fixture Plan

The machine-readable fixture pack at
`backend/tests/fixtures/semantic_memory_v1_eval_plan.json` is the canonical
execution contract for follow-on implementation tasks. It names:

- the ten v1 gates and their expected payload shapes,
- the two v1.5 deferrals,
- the v2 reflection deferral,
- three scope profile examples,
- temporal field decisions,
- representative recall and retain payloads,
- the Iris end-to-end canary query.

The initial tests validate that this fixture is internally consistent. They
also include strict `xfail` placeholders for surfaces that must fail until
SAR-1035 and later implementation tasks add the real schema, services, and API.

## Non-Goals

- No Hindsight Cloud dependency.
- No production data reads beyond provided handoff and memory summaries.
- No schema migration in SAR-1034.
- No REST, MCP, Hermes plugin, or deployment behavior changes in SAR-1034.
- No reflection or LLM-as-judge contradiction resolver before v2.
