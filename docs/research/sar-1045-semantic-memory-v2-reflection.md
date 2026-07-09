# SAR-1045 Semantic Memory V2 Reflection

Date: 2026-07-09

## Decision

Semantic reflection is a separate operation from retention and recall.

- `retain` writes raw or extracted canonical memory through `RetentionService`
  and the existing memory-entry admission path.
- `recall` reads canonical memory through strict-scope semantic retrieval.
- `reflect` proposes generated observation candidates for operator review. It
  does not write canonical memory and is disabled unless a scope profile opts in.

## Scope Profile Contract

Reflection is controlled per memory scope:

- `reflection_enabled`: defaults to `false`.
- `reflect_mission`: a separate mission prompt for observation consolidation.

`reflect_mission` must not reuse `retain_mission`. If a scope explicitly enables
reflection but leaves the mission blank, Palace uses a conservative default that
keeps conflicts visible and requires source support before promotion.

## Candidate Contract

Reflection candidates are stored as `candidate_memory_reflection` curation
artifacts, not memory entries. Generated observations carry:

- source memory ids in `source_item_ids`,
- stable source digests in `source_digests`,
- `semantic_memory_reflection.provenance_state = generated_unpromoted`,
- the originating scope and source idempotency key,
- contradiction pointers when the input identifies conflicting memories.

Candidates without source memory ids remain `needs_source`. Candidates with
source memory ids are `reviewable`. If contradictions are present, the artifact
sets `source_conflicts=true`, which blocks promotion through the existing
curation-artifact promotion gate until an operator resolves the conflict.

## Ranking Boundary

Reflection candidates are review artifacts, so semantic recall does not return
them as canonical memory. Generated observations cannot outrank raw or
source-backed memory unless a later promotion path writes a reviewed canonical
memory entry with preserved provenance.

## Verification

The focused SAR-1045 tests cover:

- reflection disabled by default,
- opt-in reflection using `reflect_mission` instead of `retain_mission`,
- generated candidates with source memory ids and audit events,
- conflict metadata that blocks unresolved same-time/non-temporal contradictions
  from promotion.
