# Palace of Truth Design System

This document is the source of truth for the current Palace of Truth UI. It codifies the shell and Palace-specific language that already ship in `frontend/` so future screens extend the same system instead of copying pages by feel.

## Product posture

- Palace of Truth is an operator workspace, not a consumer social app.
- The interface should feel calm, high-signal, and infrastructural.
- Every screen should make state legible before it tries to feel expressive.
- New UI should preserve the existing dark-shell direction unless a deliberate redesign replaces it repo-wide.

## Authority and scope

`DESIGN.md` governs product UI decisions for `frontend/`. It is intentionally
descriptive before it is aspirational: shipped tokens, shared components, route
structure, and Palace vocabulary are the baseline for future work.

When this document conflicts with a one-off page implementation, update the page
to match the document unless the divergence is deliberate and useful enough to
record here. When a product change introduces a new visual state, route family,
or Palace concept, update this document in the same PR as the UI change.

Use this document for:

- page layout and route hierarchy
- shared shell, surface, and control patterns
- Palace-specific copy, state, and freshness semantics
- design review acceptance criteria

Do not use this document to duplicate backend API contracts, deployment
procedures, or one-time project plans.

## Core principles

1. Lead with operational clarity. The primary question on each page is "what state is the system in right now?"
2. Prefer durable chrome over novelty. Reuse the shell, panels, chips, and action patterns before inventing new component families.
3. Keep copy concrete. Labels should describe real entities or real system behavior, never decorative abstractions.
4. Make hierarchy obvious. Page title, state summary, primary actions, and detail surfaces should read in that order.
5. Treat Palace as a place. Wings, rooms, tunnels, snapshots, and freshness states are product language, not throwaway metaphors.

## Shell rules

### App shell

- The app uses a dark workspace shell with a slate-to-sky background glow.
- `frontend/src/index.css` is the canonical source for shared shell tokens and utility classes.
- The main content column caps at `max-w-[1500px]`.
- Desktop keeps a persistent sidebar; mobile uses a compact top bar plus drawer menu.
- Backdrop blur is reserved for global shell and elevated panels, not sprinkled across every component.

### Shared surfaces

- Use `sb-panel` for primary elevated containers.
- Use `sb-panel-muted` for secondary or loading surfaces.
- Prefer large radii already in the system:
  - major panels: `rounded-[28px]`
  - secondary cards: `rounded-[24px]` or `rounded-2xl`
  - chips and pills: `rounded-full`
- Borders should stay soft and low-contrast, usually zinc-based with translucency.
- Shadows should create depth without reading as glossy cards.

### Page structure

- Start pages with `PageHeader` unless there is a very strong reason not to.
- Header order is:
  - eyebrow
  - title
  - supporting description
  - meta chips
  - actions
- Use `sb-page` vertical rhythm for route-level spacing.
- Prefer stacked sections over dense dashboard mosaics unless the information is truly peer-level.

### Route conformance

Every route should be visibly part of one workspace. The route may vary density
and content type, but it should not introduce a new app shell, color grammar, or
navigation model.

| Route family | Primary job | Required structure | Notes |
| --- | --- | --- | --- |
| Core routes (`Home`, `Library`, `Palace`, `Chat`) | Orient, inspect, retrieve, and act on memory state | `PageHeader`, live state summary, primary content surface | Keep Palace and Chat as the high-attention destinations. |
| Capture routes (`Capture`, `Saved Web`, `Sources`, `Feeds`) | Add, inspect, or refresh source material | `PageHeader`, explicit form state, recent job feedback | Show what will be changed before asking the operator to submit. Browser-extension capture should preserve URL provenance and saved-state feedback. |
| Utility routes (`Search`, `Graph`, `API`, `Settings`) | Inspect or configure supporting systems | `PageHeader`, compact purpose statement, framed tool surface | Utility routes are subordinate, not visually disconnected. |
| Item drill-down (`items/:id`) | Inspect one durable memory object | route title, source metadata, content body, related actions | Preserve object identity and provenance before derived analysis. |
| Palace operations (`palace/control-tower`) | Monitor and repair Palace maintenance state | telemetry, sync sources, runs, recovery queues | Risky actions need nearby state and clear labels. This is a real route, not an implementation note. |

The sidebar groups these routes into `Core`, `Capture`, and `Tools`. New routes
should join an existing group unless the product has enough durable surface area
to justify a new navigation section.

## Color mapping

Color in Palace of Truth is semantic. Bright accents should mean something.

### Base palette

- Background and shell: deep slate / zinc neutrals.
- Primary text: near-white zinc.
- Secondary text: muted zinc.
- Structural borders: zinc 700-800 range.
- Primary action accent: sky blue.

### Semantic accents

- `sky`: active focus, redirects, guided navigation, and primary action.
- `emerald`: fresh or currently indexing system state that is progressing correctly.
- `amber`: fallback, caution, degraded certainty, or work that needs operator attention soon.
- `rose`: conflict, destructive risk, or invalid state requiring intervention.
- `zinc`: stale, inactive, neutral, or baseline structural state.

Do not introduce new semantic colors for Palace-specific states unless the meaning cannot be expressed with the existing set.

## Typography

- The live app currently uses an Inter-based sans stack from `:root`.
- Headings should stay compact, high-contrast, and slightly tight-tracked.
- Eyebrows are uppercase, low-noise, and heavily letter-spaced.
- Body copy should remain concise and operational; avoid long marketing paragraphs inside the product UI.
- Numeric or status-heavy summaries should favor tabular clarity over decorative type treatment.

### Current type roles

- Page eyebrow: `text-[11px]`, uppercase, wide tracking.
- Page title: `text-3xl` to `sm:text-[2.2rem]`, semibold, tight tracking.
- Page description: `text-sm`, generous line height, muted zinc.
- Section title: compact uppercase utility label.
- Body / controls: mostly `text-sm`.
- Microcopy and metadata: `text-xs` or `text-[11px]`.

## Spacing

- Default route rhythm comes from `sb-page`: `space-y-6` on mobile, `space-y-8` on medium screens and up.
- Standard panel padding is `p-5 md:p-6`.
- Header groups and action clusters should breathe; use `gap-2` for tight controls and `gap-4` for section-level grouping.
- Prefer consistent internal spacing over bespoke per-card tuning.
- If a layout feels cramped, remove elements before shrinking spacing tokens.

## Controls and interaction

- Primary actions use `sb-button-primary`.
- Secondary actions use `sb-button-secondary`.
- Lightweight, reversible actions use `sb-button-ghost`.
- Filter chips should use the active/inactive chip pattern already established in Browse and Palace.
- Inputs should use the shared `sb-input`, `sb-select`, and `sb-textarea` styles.
- Hover and focus states should sharpen clarity, not create spectacle.
- Respect reduced-motion preferences. Motion should confirm state changes, not decorate idle surfaces.

### Accessibility baseline

- Interactive controls must have a visible text label or an `aria-label`.
- Icon-only buttons should use Lucide icons that match the action literally.
- Keyboard focus must be visible through `focus-visible` or the existing shared focus styles.
- Loading and error states should expose real copy, not only spinners or color.
- Error panels that require attention should use alert semantics where practical.
- Text contrast should remain at WCAG AA or better against the active surface.
- Motion must respect `prefers-reduced-motion`; avoid animation as the only state cue.

### Responsive baseline

- Design mobile-first, then widen into tablet and desktop layouts.
- Mobile route headers should remain scannable before the first primary action.
- Persistent side-by-side layouts should collapse into a clear reading order on narrow screens.
- Controls should not reflow in a way that changes the meaning of grouped actions.
- Fixed-format UI such as graphs, counters, and chip groups needs stable dimensions or wrapping behavior.
- Do not hide critical state on mobile merely to preserve desktop density.

## State banners and freshness language

Palace relies on explicit status semantics. Banners and pills must stay consistent across pages.

### Banner kinds

- `redirected`: explain when the user or retrieval flow landed in a different room than originally requested.
- `conflict`: explain contradictory or blocked state that needs operator judgment.
- `fallback`: explain when retrieval or navigation fell back to a broader Palace result.
- `stale`: explain that a view is real but behind current source truth.
- `indexing`: explain that work is underway and some derived artifacts are still catching up.

Banner copy should state:
- what happened
- why it matters
- what the operator should expect next

### Freshness pills

- `fresh`: derived artifact reflects current indexed state.
- `indexing`: refresh is in progress.
- `redirected`: artifact is available through lineage rather than direct freshness.
- `stale`: artifact exists but is behind the current source or generation.

Use freshness pills for narrow subsystem status, not broad alert messaging.

## Palace page patterns

### Empty and pre-built states

- If no corpus source exists, say so plainly and point to the next real action.
- If sources exist but rooms do not, explain that the corpus is connected and the Palace has not been built yet.
- Empty states should feel intentional, not apologetic.

### Utility routes

- Utility routes are still first-class Palace screens. They should use the shared shell, `sb-page` rhythm, and `PageHeader` unless there is a concrete usability reason not to.
- Utility pages should feel subordinate to Palace and Control Tower, but not visually disconnected from them.
- Lead with what the utility surface is for, then show the live reference or tool itself.
- Supporting explanation should stay short and operational. Utility routes are for inspection and execution, not long-form product education.
- When the page embeds an external-style surface inside Palace chrome, frame it with a local section header so users stay oriented inside the workspace.
- Utility routes should prefer real metadata chips such as `Live OpenAPI`, `Read-only reference`, `Admin-only`, or `Generated from current tenant state` over decorative status pills.
- If a utility route links to a raw backend surface, keep that link secondary to the in-shell experience.

### Control Tower

- Control Tower is the operator cockpit for sync, backlog, and recovery state.
- Counters and cards should read as system telemetry, not growth metrics.
- If a value is a generation counter or backlog signal, label it that way rather than implying a document count.

### Palace drill-down

- Palace should read as a sequence:
  - overview
  - wing selection
  - room detail
  - retrieval trace
- Keep the currently selected room obvious at all times.
- When showing derived room artifacts, pair them with freshness indicators.

## Page addenda

These rules extend the shared shell for the two Palace-specific operator pages that define the current product language.

### Palace page

- The Palace route is the spatial browsing surface, not a generic dashboard.
- The page should always preserve this reading order:
  - page header and top-level action
  - state banner or blocking empty state
  - overview metrics
  - wing and room navigation
  - selected room detail
  - retrieval trace
- Overview metrics should summarize Palace state, not compete with the room detail pane.
- Wing selection should feel like navigational structure. Room selection should feel one level deeper and remain visibly tied to the chosen wing.
- Room detail is the editorial center of gravity. Summaries, pinned items, memberships, freshness pills, and redirect context belong there.
- Retrieval trace is subordinate to room detail. It should explain why Palace answered the way it did after the user has oriented to the room itself.
- Empty-state language should stay architectural:
  - no source connected: explain that Palace needs a real corpus first
  - source connected but not built: explain that a Palace run will create wings, rooms, summaries, and tunnels
- When a room is stale, indexing, redirected, or in fallback, the banner should sit close to the affected room content rather than detached elsewhere on the page.

### Control Tower page

- Control Tower is the operational maintenance page, not the exploratory retrieval page.
- The page should usually group into these sections:
  - top-level telemetry and primary actions
  - sync source management
  - recent Palace runs
  - memory or recovery queues
- Prefer stacked operational sections over dense side-by-side panels when the page mixes forms, lists, and retry actions.
- Forms should be explicit and infrastructural. Hidden magic is the wrong tone here.
- Risky actions such as delete, rotate, or retry should stay visually subordinate to safe read-only telemetry until the operator reaches the relevant section.
- Counters should explain live system posture:
  - sync freshness
  - backlog or drift
  - failed work that can be retried
- A section with actions should show the relevant current state nearby so operators do not have to cross-reference another panel before acting.

### API Docs route

- `API Docs` is the canonical utility-screen example.
- The page should keep this order:
  - utility eyebrow and title
  - one-sentence explanation of what the embedded reference is for
  - a compact reference-surface intro card
  - the live contract explorer
- `Open raw spec` is a secondary escape hatch, not the primary call to action.
- The embedded API reference should sit inside a Palace panel so the route still reads as part of the workspace rather than a third-party docs island.
- Copy should emphasize live schema inspection, request and response verification, and auth expectations. Avoid generic developer-portal language.

### Page-specific copy examples

- Good Palace empty-state copy names the missing prerequisite and the next safe action.
- Good Control Tower copy explains whether a value is telemetry, backlog, drift, or failure recovery.
- Good room-level copy says what artifact is stale or redirected, not just that "something changed."
- Avoid generic placeholders like "No data available" or "Configuration updated successfully" when the page can name the exact Palace object involved.

## Palace vocabulary

Use these terms consistently in UI copy and docs:

- `Palace`: the retrieval and organization control plane.
- `Control Tower`: the operational view for sync sources, Palace runs, and recovery state.
- `source`: a connected folder, repo, or bucket that feeds Palace.
- `wing`: a top-level Palace grouping.
- `room`: a navigable topic space inside a wing.
- `tunnel`: a derived connection between rooms.
- `snapshot`: the synthesized room summary/state derived from indexed items.
- `membership`: the set of items Palace currently assigns to a room.
- `freshness`: whether a derived Palace artifact matches current indexed truth.
- `redirect`: lineage-driven handoff from one room to another.
- `fallback`: retrieval widened scope because the requested room could not fully satisfy the query.

Avoid replacing this vocabulary with generic terms like "category", "folder", or "section" unless the backend concept is actually different.

## Copy rules

- Prefer direct verbs: `Run Palace now`, `Reload Palace`, `Export view`, `Retry`.
- Prefer nouns that match backend concepts exactly.
- Use sentence case for body copy and button labels.
- Avoid inflated claims such as "intelligent", "magical", or "revolutionary".
- If the system is waiting, say what is processing.
- If the system failed, say which subsystem failed and whether retry is safe.

## Anti-patterns

- Do not add bright accent colors that compete with semantic state colors.
- Do not create fake dashboard widgets, fake notifications, or decorative charts.
- Do not hide important state inside tooltips when it can be shown inline.
- Do not mix Palace metaphors with unrelated library metaphors on the same screen.
- Do not use banner styles interchangeably; users should be able to learn them.
- Do not introduce visual noise to "make it feel designed." The product already has a strong shell; use it.

## Design review checklist

Before a UI PR is ready for review, check:

- The route uses the shared shell, header, panels, controls, and route rhythm unless the PR updates this document with a justified exception.
- The first viewport answers what the page is for, what state it is in, and what the operator can safely do next.
- Copy names real Palace objects, backend concepts, or user actions; no placeholder, decorative, or inflated language remains.
- Semantic colors match the meanings in this document.
- Empty, loading, stale, indexing, fallback, redirected, and error states are explicit.
- Controls are reachable by keyboard, have clear labels, and preserve focus visibility.
- Mobile, tablet, and desktop layouts keep text readable and avoid overlapping controls.
- Visual surfaces are clean: no noisy gradients, fake widgets, malformed icons, or decorative controls that imply unavailable functionality.
- Any new route family, state kind, vocabulary term, or reusable UI pattern is recorded here.

## Implementation notes

- Shared shell and token definitions live in `frontend/src/index.css`.
- The canonical page header pattern lives in `frontend/src/components/PageHeader.tsx`.
- Palace state semantics are currently expressed in `frontend/src/components/PalaceStateBanner.tsx` and `frontend/src/components/PalaceFreshnessPill.tsx`.
- The main reference pages for this system are `frontend/src/pages/Palace.tsx`, `frontend/src/pages/PalaceControlTower.tsx`, and `frontend/src/pages/ApiDocs.tsx`.

Future page-specific addenda can extend this document, but they should not contradict the shared rules here without updating this file.
