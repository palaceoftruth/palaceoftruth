# Tenant Canon: authored corpus for agents

Status: design approved in principle 2026-10-30. Build packets pending.
Owner: Andrew Sarver. Author: Barbara.

## 1. Problem

Every derived artifact in Palace today is generated: room snapshots, retrieval
hints, temporal facts, wake-up briefs. Generations move when the corpus moves.
That is correct for recall and wrong for canon.

A tenant needs one place where a human writes down what the company is, what the
rules are, and what each area of the business means. Agents must read that first
on connect, treat it as authoritative, and must not drown their context in it.

Three requirements follow:

1. Authored, not generated. The machine never silently rewrites canon.
2. Easy to maintain by a human who is not a developer. Markdown pages, a web
   editor, revision history, and a folder upload. No git workflow in v1.
3. Cheap to consume. An agent gets a small index by default and fetches one page
   on demand. The full corpus is never injected.

## 2. Vocabulary

Add to `DESIGN.md` and use exactly:

- `canon`: the authored corpus. Human owned, human written.
- `page`: one canon document. Short, single topic, stable slug.
- `charter`: the canon page bound to a room. States what the room is for.
- `snapshot`: the derived room summary. Never called a charter.
- `digest`: the bounded index served on connect. Built by Palace, never edited.
- `ratify`: the human act that confirms a page still matches current truth.

`charter` and `snapshot` must never be used interchangeably in UI copy.

## 3. Locked decisions

| Decision | Choice |
| --- | --- |
| Structure | Rooms are the spine. A page may bind to one room and becomes its charter. |
| Tenant-level pages | Live on a root wing (`company`), so they render in the same spatial model. |
| Who writes in v1 | Humans only. Agents may write everywhere else in Palace, never canon. |
| Repo as canon source | Phase 2. Not in v1. |
| Folder upload | In v1. Markdown folder of pages. |
| Context caps | As listed in section 11. |

## 4. Data model

A canon page **is** an item. This is the core of the integration: embeddings,
hybrid search, provenance, citations, and the review inbox already work on items.

### Reuse: `items`

- `source_type = 'canon'`
- `title` = page title
- `raw_content` = page markdown
- `summary` = one line, mandatory, this is the digest index line
- `metadata_.canon = {slug, section, room_id, parent_slug, priority}`
- `tags` = `canon`, `canon-section-<section>`, plus authored tags
- Governance block is used as designed: `governance_owner_subject` (author),
  `governance_verification_state` = `unverified` | `verified`,
  `governance_verified_at`, `governance_reviewer_subject`.

Authoring a page sets `verification_state = 'verified'` because a human wrote it.
That is the whole point of the governance columns.

### New table: `canon_pages`

Migration `072_tenant_canon_pages`. Editorial metadata only, joined to `items`.

```
id                 uuid pk
tenant_id          text not null
item_id            uuid not null, fk items(id) on delete cascade
room_id            uuid null, fk rooms(id) on delete set null
slug               text not null            -- unique per tenant
section            text not null            -- identity|rules|glossary|runbook|charter|faq|reference
parent_slug        text null                -- one level of nesting, no deeper
summary            text not null            -- <=200 chars, mirrors items.summary
priority           int not null default 100 -- lower wins in digest
pinned_in_digest   boolean not null default false
status             text not null default 'published'  -- draft|published|archived
last_confirmed_generation int not null default 0
last_confirmed_at  timestamp tz null
char_count         int not null default 0
created_by_subject text null
updated_by_subject text null
created_at, updated_at timestamp tz
unique (tenant_id, slug)
index  (tenant_id, room_id) where status='published'
index  (tenant_id, status, priority)
check  section in (...), status in (...), char_count <= 20000
```

### New table: `canon_page_revisions`

Immutable history. Append on every publish.

```
id, tenant_id, page_id uuid fk canon_pages(id) cascade,
revision int, body_md text, summary text, change_note text,
created_by_subject text, created_at
unique (page_id, revision)
```

### New table: `canon_digest_cache`

Digest is deterministic from page state, so it is cacheable.

```
tenant_id text pk, fingerprint text not null, digest_text text not null,
char_count int not null, page_count int not null, built_at timestamp tz
```

Fingerprint = sha256 of (ordered tuple of page slug, summary, priority, pinned,
status, updated_at) plus the budget. Cache miss rebuilds.

### Room binding

`canon_pages.room_id` is the spine. Rules:

- One charter per room. Partial unique index on `(tenant_id, room_id)` where
  `section = 'charter'` and `status = 'published'`.
- A charter is shown above the derived snapshot in the room detail pane, always
  labeled, never merged.
- Pages without a room appear under the `company` root wing.
- Authored cross references (`related: [slug, ...]` in front matter) also insert
  a `room_tunnels` row with `tunnel_type = 'authored-link'` when both pages have
  rooms. Derived tunnels keep `shared-tag`.
- The charter membership is written by the canon service as
  `room_memberships.source = 'authored'`, `membership_kind = 'primary'`. The room
  rebuild must never write or delete that row.

### Embedding and linking

Palace already has the machinery. No new vector stack.

- Publishing a page calls `process_prebuilt_item` (`backend/app/services/
  item_processing.py`), the same function `wakeup_briefs.py` and
  `diary_rollups.py` already call for Palace's own generated items. That gives
  chunking, embeddings under the default profile, and the generated `tsvector`.
- Embedding uses the **default profile**, the same vector space as every other
  item. A private canon profile is rejected: it would make canon similarity to
  real items impossible and duplicate the ANN index for no benefit.
- `content_hash` guard: re-publishing an unchanged body makes zero new embedding
  calls. Only changed bodies re-embed.
- Linking then falls out of existing services:
  - `item_relationships` via `RelationshipService.find_relationships`, which
    takes the item's embedding centroid, finds the top 5 similar items, and
    classifies the edge with the LLM at confidence >= 0.5. That is how a charter
    acquires "these are the items it governs". It requires a non-empty
    `summary`, which the mandatory 200 char summary already guarantees.
  - `room_memberships` as above, giving room-scoped retrieval the charter.
  - `room_tunnels` for authored cross references.
- **Routing guard, non-negotiable.** `_apply_room_routing` in
  `backend/app/services/palace.py` auto-assigns every ready item to a wing and
  room and bumps `room.membership_generation`. A canon item must be excluded
  explicitly there, and must carry `metadata_.sync_active = false`. Without this
  guard, every canon publish reroutes the page, bumps the room generation, and
  the drift detector fires against the page's own edit. Self-induced drift on
  every save. The guard is the difference between a working feature and a
  permanent false alarm.
- `search.py` gains a canon source label so results display as authored canon
  rather than as an unsourced capture. Canon results count as authored support,
  never as `unsupported` or `partial_source`.

## 5. Authoring API

All under `/api/v1/canon`. Human session principal required for writes.

```
GET    /canon/pages?room_id=&section=&status=   list, paged
POST   /canon/pages                             create draft
GET    /canon/pages/{slug}                      page + revision count
PATCH  /canon/pages/{slug}                      edit, appends a revision
POST   /canon/pages/{slug}/publish              flip draft to published
POST   /canon/pages/{slug}/ratify               confirm current truth, bumps last_confirmed_generation
POST   /canon/pages/{slug}/archive
GET    /canon/pages/{slug}/revisions            history
GET    /canon/pages/{slug}/revisions/{n}        one revision
GET    /canon/digest?budget=                    the digest, capped server side
GET    /canon/search?q=&limit=                  canon-only search, snippets
POST   /canon/import/folder                     multipart folder, preview then commit
```

`import/folder` is two phase. Phase A returns a diff plan: for each file, slug,
title, section, action (`create` | `update` | `skip-unchanged` | `conflict`).
Phase B takes a plan id plus the file hashes and commits. Never partial writes.

## 6. The wall between agents and canon

Canon read is open to any authorized agent scope, because agents must be able to
read it on connect. Canon write is closed.

- Write handlers reject any principal that is an MCP OAuth client binding, with
  `403 canon_human_author_only` and a reason string. Same guard regardless of the
  agent's other grants.
- No MCP write tool is registered for canon in v1. There is nothing to call.
- Agents that want a page changed file a `candidate_curation_artifact` with
  `artifact_kind = 'candidate_canon_edit'` and a proposed body. The human sees it
  in the review inbox and either applies it or rejects it. This is the phase 2
  agent contribution path, and the schema supports it on day one.
- RLS: every canon table carries `tenant_id` and joins the existing tenant RLS
  policy pattern from `067_tenant_rls_enforcement.py`.

## 7. Retrieval integration

Three tiers. One module, `backend/app/services/canon.py`, owns all of them so the
caps cannot drift between callers.

**Tier 0, digest.** Always served on connect. Contains:

1. Identity block: first 400 chars of the page with `section='identity'` and
   lowest priority.
2. Rules block: up to 700 chars from `section='rules'` pages, one bullet per page.
3. Index: one line per page, `slug | summary`, 60 chars per line, max 12 pages,
   ordered by pinned then priority.
4. Overflow: when the index does not fit, emit `section: N pages` counts plus the
   pinned pages only, and note that more exist.

**Tier 1, page get.** `GET /canon/pages/{slug}` through MCP as
`palace_canon(action='get', slug=...)`. Default 6000 chars, hard 12000, then
paged by heading range (`?after_heading=`). Full page is never injected by
default.

**Tier 2, canon search.** `palace_canon(action='search', q=...)`. Returns at most
8 hits of `slug`, heading anchor, and a 300 char snippet. Items with
`source_type='canon'` are additionally boosted in general `palace_search` so a
canon page outranks an equivalent raw capture, but the page body still arrives
only as a snippet.

### Precedence, stated to the agent

The MCP server instructions and the canon tool descriptions state:

> Tenant canon outranks derived memory. Read the digest before answering
> questions about this tenant's identity, rules, naming, or procedures. Cite the
> page slug and revision. If canon is silent, say so before using derived recall.

Order: canon > `tenant_shared` memory entries > derived room snapshots and
temporal facts.

### Wake-up brief

`_render_wakeup_body` in `backend/app/services/wakeup_briefs.py` gains a
`## Canon (authored, authoritative)` block above `## High-signal rooms`. Digest
only. Metadata records `canon: {page_count, chars, truncated}` so budget spend is
visible in the brief record.

## 8. Drift and ratification

Canon never regenerates. It can fall behind. Palace detects that and asks.

- A page is `drift-suspected` when
  `room.snapshot_generation > canon_pages.last_confirmed_generation` for its room,
  or when the room gained more than 10 memberships since that generation.
- Detection emits a `candidate_curation_artifact` with
  `artifact_kind = 'candidate_source_drift'`, target surface
  `canon:<slug>`, and a body naming what changed: item counts, new tag clusters,
  new claims since the confirmed generation.
- The page shows a `stale` or `drift-suspected` freshness pill in the room detail
  pane, matching the existing banner and pill semantics in `DESIGN.md`.
- `POST /canon/pages/{slug}/ratify` sets `last_confirmed_generation` to the
  current room generation, stamps `last_confirmed_at`, and clears the pill. If a
  human edits instead, the publish clears drift too.

The machine proposes. The human ratifies. That ratchet is the feature.

## 9. Folder upload (v1)

The maintenance path for someone with existing notes.

- Accepts `.md`, `.mdx`, `.txt`, max 500 files, 250 KB per file, 5 MB total.
- Optional YAML front matter: `title`, `summary`, `section`, `room` (room slug),
  `tags`, `related`. Missing values are derived: title from first heading, slug
  from filename, summary from first non-empty sentence, section defaults to
  `reference`.
- Allowed front matter keys are an allowlist. Unknown keys are kept under
  `metadata_.canon_extra` and never promoted to columns.
- Two phase preview then commit, as in section 5. Conflict means an existing
  published page with the same slug and a different body hash. The operator picks
  overwrite, rename, or skip per file.
- Imported pages land as `verified`, owned by the uploading subject.

Same mechanism later serves the phase 2 repo binding, so build it once here.

## 10. UI

Two surfaces, both inside existing route families.

**Palace room detail pane.** Adds a `Charter` tab beside the existing summary,
members, and drawers tabs. Shows the charter body, freshness pill, ratify button,
and link to edit. Empty state names the next action: "This room has a derived
snapshot. Write a charter to state what this room is for."

**New `Canon` route** in the capture group of the sidebar. Contents: page list
grouped by section, search, page editor (markdown with preview, slug, summary,
section, room picker, priority, pinned), revision history with diffs, drift
review list, and folder import.

Both surfaces follow `DESIGN.md`: `PageHeader`, `sb-panel`, existing chip and
banner patterns, no new colors.

## 11. Caps

Enforced in `services/canon.py`. These are defaults, configurable per tenant.

| Budget | Value |
| --- | --- |
| Digest total | 2000 chars target, 2400 hard |
| Digest identity block | 400 chars |
| Digest rules block | 700 chars |
| Digest index line | 60 chars, max 12 pages |
| Page get default | 6000 chars |
| Page get hard max | 12000 chars |
| Canon search hits | 8 |
| Canon snippet | 300 chars |
| Per-connect canon budget | 2500 tokens, hard stop |
| Stored page body | 20000 chars max, editor warns at 8000 |
| Summary field | 200 chars, mandatory |
| Pages per tenant | 500 soft |
| Folder upload | 500 files, 250 KB each, 5 MB total |

Truncation is never silent. Every capped response carries `truncated: true` and a
`next` hint (heading range, page cursor, or search offset).

## 12. Migration sketch

`backend/alembic/versions/072_tenant_canon_pages.py`, down_revision
`071_source_drift_artifacts`.

1. `create_table canon_pages`, `canon_page_revisions`, `canon_digest_cache`.
2. FKs: `item_id` cascade, `room_id` set null.
3. Partial unique index for one published charter per room.
4. Check constraints for `section`, `status`, `char_count`.
5. Enable RLS and add tenant policies following `067_tenant_rls_enforcement.py`.
6. Add `candidate_canon_edit` to the `ck_candidate_curation_artifact_kind` check
   constraint, same shape as `071`.

No changes to `items`, `rooms`, or `wakeup_briefs` schema. All additions are new
tables plus one constraint alteration.

## 13. Test plan

- Unit: digest builder respects every cap, including the degenerate case of 300
  pages and one 40 KB identity page.
- Unit: precedence ordering, canon over tenant_shared over derived.
- Contract: canon write endpoints return `403 canon_human_author_only` for every
  MCP OAuth client principal, and 201 for a user session. This test is the wall.
- Contract: folder import preview is idempotent, and re-importing an unchanged
  folder makes zero writes.
- Integration: wake-up brief contains the canon block first, and its metadata
  records the char count.
- Regression: `palace_search` with `source_type='canon'` boost does not break
  existing hybrid ordering tests.
- Drift: after a room generation bump, the page reports `drift-suspected` and a
  curation artifact exists; ratify clears both.
- Routing guard: publishing a canon page creates no `auto` room membership and
  bumps no room generation. This is the self-drift regression test.
- Embedding: publishing a page produces `embeddings` rows plus relationship
  edges; re-publishing an unchanged body produces neither.
- Linking: a charter page's relationships point at the source items it governs,
  and those edges respect the confidence threshold.

## 14. Build packets

Sequenced so each packet ships green on its own.

1. **Schema and service.** Migration 072, models, `services/canon.py` with digest,
   get, search, caps. The `_apply_room_routing` canon guard and the `search.py`
   source label. Tests for caps, digest, and the routing guard.
2. **Authoring API and the wall.** CRUD, publish, ratify, archive, revisions, the
   human-principal guard, contract tests. Publish calls `process_prebuilt_item`
   behind a `content_hash` guard and writes the authored charter membership.
3. **Retrieval wiring.** Canon boost in search, `palace_canon` read tool, MCP
   instruction text, wake-up brief canon block, relationship edges from
   `RelationshipService` now that pages are embedded.
4. **Folder import.** Two phase preview and commit, plus conflict UI copy.
5. **UI.** Charter tab in room detail, Canon route with editor and revision diff.
6. **Drift and ratification.** Detector, curation artifacts, pills, ratify path.

## 15. Phase 2: repo as a canon source

A corpus that lives in git also lives somewhere else. Without a binding, keeping
both in step is a copy-and-paste job with no history, no review, and no proof that
they agree. This section specifies the binding. It replaces the earlier note that
put repo sync out of scope.

### Locked decisions

Andrew, 2026-11-01.

- **One way.** Repo to canon. Palace never opens a pull request against the corpus.
- **A sync run never publishes.** It writes drafts and revisions. A person
  publishes, the same ratchet as section 9. A scheduled job is not a human.
- **A file removed from the repo archives its page.** Nothing is deleted.
- **Pilot binding:** `github.com/sarverenterprises/agent-corpus`, private,
  default branch `master`.

### What is reused, not rebuilt

Palace already carries a sync engine and canon already carries an importer. The
binding joins them.

- `SyncSource` (`app/models/palace.py`) already holds folder, repo and object
  storage sources, `scan_interval_seconds`, `allowed_extensions`, status, and an
  encrypted per-source credential.
- Git transport (`app/services/palace.py`, `_repo_git_environment` and
  `run_sync_run`) already clones remote GitHub repositories over HTTPS or SSH,
  manages checkouts, and resolves blob URLs.
- `SyncRun` and `SyncSourceFile` already record per-run counters and a per-path
  row with `content_hash` and `item_id`. That row is the file-level drift record.
- `app/workers/palace_tasks.py` already schedules rescans, probes for changes, and
  queues runs.
- `services/canon_import.py` already parses front matter, derives missing values,
  plans against existing pages, and commits a batch in one transaction under a
  content-addressed plan id.

Nothing in this phase adds a second sync stack, a second parser, or a second
send path.

### Data model

Additive migration `073_repo_canon_bindings`.

- `sync_sources.target_kind` text, `'rooms'` (server default) or `'canon'`.
- `sync_sources.canon_section_default` text, server default `reference`.
- `sync_sources.canon_room_id` nullable, for a binding that supplies charters.
- `sync_sources.canon_owns_pages` boolean, default true. Records that the source
  created these pages and may therefore update them.
- `sync_source_files.page_slug` text nullable. `item_id` already exists and points
  at the page's item.
- `sync_runs.commit_sha` text nullable, plus `pages_created`, `pages_updated`,
  `pages_archived`, `pages_conflicted` integers. The existing file counters stay.

### Mapping

Front matter wins, then the path decides.

- `title`, `summary`, `section`, `room`, `tags`, `related` from front matter,
  under the existing allowlist. Unknown keys stay in `metadata_.canon_extra`.
- Slug is front matter `slug` when present, else `slug_from_path`.
- Only `.md`, `.mdx` and `.txt` are read. A file outside the allowlist is skipped
  and counted, never imported with a guessed type.
- A binding owns the pages it created. A page a human wrote by hand with the same
  slug is a conflict, not an update.

### Run semantics

One run, in this order.

1. Resolve the branch head to a commit sha. An unreachable repo fails the run
   before any write.
2. List eligible files under the configured prefix. Enforce the import caps on the
   whole batch. Over the cap means the run refuses, not that it imports a slice.
3. Parse each file, then `plan_import` against the existing pages.
4. Apply per file: create becomes a draft page; update becomes a new revision and
   the page returns to draft; skip-unchanged performs no write; conflict files a
   `candidate_canon_edit` artifact targeting `canon:<slug>` and writes nothing.
5. A path that disappeared from the repo archives its page through
   `archive_canon_page` and files an artifact saying the file is gone.
6. Commit once per run. Record the commit sha, the per-file action, and the
   counters on the run.

A run must never call `publish_canon_page`, must never enqueue `palace_run_build`,
and must never bump a room generation. The canon routing guard keeps pages out of
rebuilds; the run must not reintroduce the bump by another path.

### Drift

A repo change is evidence, not ratification. If a page is already drift-suspected
when the file changes, the run updates the draft and the page stays
drift-suspected until a person ratifies it. Only ratify and publish clear drift.

The existing detector already compares the room's snapshot generation with the
generation a page confirmed. File-level change is reported from
`sync_source_files.content_hash` and the last run, beside it.

### Caps

The import caps are the run caps: 500 files, 250 KB per file, 5 MB per run. One
module owns them. A run reports `truncated` and the reason when it refuses a batch,
and the UI shows it. No silent partial sync.

### Test plan

Named tests, each failing when its behaviour is removed.

- `test_a_changed_file_creates_a_draft_and_publishes_nothing`
- `test_a_sync_run_does_not_move_a_room_generation`
- `test_a_deleted_file_archives_the_page`
- `test_a_renamed_file_archives_the_old_slug_and_creates_the_new_one`
- `test_a_hand_written_page_with_the_same_slug_conflicts`
- `test_an_unreachable_repo_fails_the_run_and_leaves_pages_untouched`
- `test_re_running_an_unchanged_repo_performs_zero_writes`
- `test_a_run_over_the_caps_refuses_the_whole_batch`
- `test_a_repo_change_leaves_a_drifting_page_drifting`
- `test_a_canon_target_run_never_enqueues_a_palace_rebuild`

### Build packets

7. **Binding.** Migration 073, the columns, the API to create and inspect a canon
   binding, and refusal when the repo cannot be reached.
8. **Canon-aware run.** Route a `target_kind='canon'` run through
   `plan_import`/`commit_import`. Drafts only, archive on delete, artifact on
   conflict, no rebuild, no generation bump.
9. **Run history.** Surface runs and per-file actions, and show file-level change
   beside drift on the Canon route.
10. **Triggers.** Scheduled rescans, a manual "Sync now" behind the human wall,
    and refusal when a run is already in flight.
11. **UI and vocabulary.** Binding list and editor, run results, conflict
    resolution. `DESIGN.md` gains `binding`, `sync run`, `repo page`.

### Still out of scope

- Agent-proposed canon edits applied automatically. v1 files them for a human.
- Two-way sync, and any automatic publish when the repo merges.
- Multi-language pages.
- Canon-aware room generation, where a charter constrains the derived snapshot.
- Bucket as a canon source. The columns exist; the binding does not.

## 16. Risks

- **Canon rot.** Mitigated by drift detection and ratification, not by generation.
- **Self-induced drift.** A canon item auto-routed into a room bumps the very
  generation the drift detector compares against, so every publish would flag
  itself stale. Mitigated by the routing guard in section 4, and covered by a
  named regression test.
- **Canon flooding general search.** Embedded canon chunks compete with real
  corpus in hybrid retrieval because instruction text is dense and matches
  broadly. Mitigated by the boost being bounded, the canon result cap, and canon
  search as the preferred path. Watch result-mix telemetry after release.
- **Two sources of room truth.** Charter and snapshot can disagree in front of a
  user. Mitigated by strict labeling, charter above snapshot, and never merging
  them in one block.
- **Digest becomes a dumping ground.** Mitigated by the hard 2400 char cap and the
  mandatory 200 char summary. A page that cannot state itself in one line is too
  big and the editor says so.
- **Agents ignore canon.** Mitigated by tool description, MCP instructions, brief
  placement first, and search boost. Measurable: log canon tool calls per session.
