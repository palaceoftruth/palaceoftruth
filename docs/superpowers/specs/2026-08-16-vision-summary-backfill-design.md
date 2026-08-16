# Vision Recovery and Summary Backfill Design

Date: 2026-08-16

## Purpose

Restore reliable image analysis for browser-captured images and provide a safe,
resumable way to fill missing summaries for normal media and webpage items.
Agent-memory notes are outside the first backfill scope because their contract
makes summaries optional and disables AI enrichment by default.

## Current State

The deployed Palace tenant has ten image candidates from three X captures made
on 2026-08-15 Eastern time. The item rows and artifact files still exist, but
all ten vision jobs failed after OpenRouter returned HTTP 404 for both the
primary and fallback models. Failed items are hidden from normal Library browse
results.

The active tenant also has 4,041 ready items without summaries:

- 3,969 agent-memory notes;
- 70 media items;
- 1 webpage item.

The current memory contract defines `summary` as optional and
`enable_ai_enrichment` as `false` by default. Live data confirms that contract:
8,986 memory jobs used `enable_ai_enrichment=false`; 4,865 of those still have a
caller-supplied summary. All 147 memory jobs with enrichment enabled have a
summary. A missing summary on a raw agent-memory capture is therefore not, by
itself, a processing failure.

Palace main already contains the provider-compatibility repair from PR #454.
This work builds on that current main revision instead of the older public-copy
history that originally created the worktree.

## Model Selection

The vision chain will be:

1. `minimax/minimax-m3`
2. `openai/gpt-4o-mini`
3. `openai/gpt-4.1-mini`

OpenRouter currently reports MiniMax M3 as accepting text, image, and video
input and supporting structured output. MiniMax M2.7 is text-only and must not
be used for vision. GPT-4o mini is the lowest-cost verified fallback; GPT-4.1
mini is the final compatibility fallback.

Sources:

- https://openrouter.ai/minimax/minimax-m3/pricing
- https://openrouter.ai/minimax/minimax-m2.7/pricing
- https://openrouter.ai/openai/gpt-4o-mini
- https://openrouter.ai/openai/gpt-4.1-mini/benchmarks
- https://openrouter.ai/api/v1/models

The chain will continue to use Palace's strict local `VisionAnalysis`
validation. Provider-only schema constraints will remain relaxed as implemented
by PR #454. Model-specific parameters, such as Gemini reasoning controls, must
not be sent to models that do not advertise those parameters.

Immediate fallback applies to provider rejection or compatibility responses
such as HTTP 400, 403, 404, and 422. HTTP 429 and server failures receive only
the existing bounded retries before fallback. Logs and persisted failure
metadata must identify the attempted model without retaining provider response
bodies or image content.

## Summary Backfill Scope

The first backfill includes an item only when all conditions are true:

- tenant matches the explicit `--tenant-id` value;
- `source_type` is `media` or `webpage`;
- status is `ready`;
- `deleted_at` is null;
- summary is null or blank;
- preserved raw content is non-blank.

The first backfill excludes agent-memory notes, failed items, image candidates,
deleted items, and items that already have a summary.

## Summary Backfill Architecture

Add a dedicated summary-only service and a backend operator script. Do not
reuse `embed_item`, because that path also rewrites embeddings and may generate
taxonomy.

The operator script will:

1. Select a stable ordered page of eligible item identifiers and content
   previews.
2. Close the selection transaction before making provider calls.
3. Generate one concise summary at a time through the existing OpenRouter text
   path, using MiniMax M2.7 as the requested text model.
4. Open a short transaction for each completed result.
5. Lock and re-read the item.
6. Update only when the item is still eligible and its summary is still blank.
7. Store the summary and a compact `summary_backfill` metadata receipt.
8. Commit that item before continuing.

The metadata receipt will contain only:

- schema version;
- backfill implementation name;
- completion timestamp;
- requested model;
- source content hash;
- source type.

The receipt will not contain source text, provider response bodies, credentials,
or full prompts.

## Controls and Recovery

The script is dry-run by default. A live write requires `--write`. Required and
bounded controls are:

- explicit tenant identifier;
- source types restricted to `media` and `webpage`;
- positive item limit;
- batch size capped at 25;
- provider concurrency fixed at one for the first release;
- deterministic order by creation time and item identifier;
- optional `--item-id` restriction for a pilot;
- process exit failure when any selected item fails, after preserving receipts
  for items already completed.

Re-running the same command is safe because existing summaries are never
overwritten and each item is rechecked under a row lock. Provider failures leave
the item unchanged and appear in a secret-safe report. The script will report
selected, completed, skipped, failed, and remaining counts.

The production sequence is:

1. Run a dry-run inventory.
2. Process a five-item pilot.
3. Verify summary quality, unchanged embeddings and taxonomy, and worker health.
4. Continue in batches of at most 25.
5. Stop on a rising error rate, HTTP 429 exhaustion, database instability, or
   unexpected item changes.

Production execution is separate from implementation and deployment. No live
backfill runs as part of unit tests, CI, chart installation, or application
startup.

## Image Recovery

The existing `backend/scripts/backfill_image_summaries.py` remains the recovery
path for preserved image candidates. This change must make its model chain use
MiniMax M3 first and preserve the fallback behavior above.

After deployment, image recovery will use this sequence:

1. Dry-run the ten known image candidate identifiers.
2. Process one retained image as a canary.
3. Verify artifact hash, vision receipt, searchable summary, item status, and
   parent linkage.
4. Process the remaining nine retained images.
5. Retry the two failed parent webpage captures only after the database
   connection-lifetime issue is separately verified or repaired.

This work does not delete, replace, or redownload a preserved artifact when its
recorded file and hash remain valid.

## Agent-Memory Policy

Agent-memory summaries remain optional:

- curated memories and checkpoints should normally supply a concise summary;
- callers may explicitly enable AI enrichment when they want Palace to generate
  one;
- raw transcript turns, imported session events, and other high-volume captures
  may remain unsummarized because retrieval uses preserved chunks;
- a future agent-memory audit should classify missing summaries by source and
  memory purpose before any source-specific backfill.

The current Library message that says an AI summary has not been generated can
be misleading for agent memories where no summary was requested. Correcting
that UI wording is a separate change.

## Testing

Automated coverage will verify:

- the default vision chain order is M3, GPT-4o mini, then GPT-4.1 mini;
- MiniMax M2.7 is never used for image input;
- model-specific parameters are not leaked to incompatible fallback models;
- structured vision responses still receive strict local validation;
- summary dry-run performs no writes or provider calls;
- selection excludes memory notes, image candidates, failed, deleted, blank
  content, and already summarized items;
- write mode fills only a blank summary and records the compact receipt;
- an item changed after selection is skipped under lock;
- provider failure leaves the item unchanged and produces a safe failure count;
- rerunning after partial completion does not overwrite summaries;
- limits, batch size, tenant scope, and item restrictions fail closed.

Focused backend tests, the full backend test suite required by repository CI,
`git diff --check`, and chart render validation must pass before the work is
presented for review.

## Success Criteria

Implementation is complete when:

- configuration defaults and examples use the approved vision chain;
- focused model-chain and summary-backfill tests pass;
- a dry-run report deterministically identifies the 71 current media/webpage
  candidates without selecting agent memories;
- no existing summary, raw content, embedding, tag, category, or deletion state
  is changed by the backfill implementation tests;
- the image recovery script remains dry-run safe and compatible with preserved
  browser image artifacts;
- deployment and production execution remain explicit follow-up actions.
