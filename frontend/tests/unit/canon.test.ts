import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import {
  BODY_MAX_CHARS,
  BODY_WARN_CHARS,
  SECTION_ORDER,
  bodyLengthDelta,
  bodyLengthState,
  formToPayload,
  groupPagesBySection,
} from "../../src/lib/canon.ts";
import type { CanonPage, CanonRevision } from "../../src/api/types.ts";

function page(section: CanonPage["section"], slug: string, extra: Partial<CanonPage> = {}): CanonPage {
  return {
    slug,
    title: slug,
    summary: `${slug} summary`,
    section,
    status: "published",
    room_id: null,
    parent_slug: null,
    priority: 100,
    pinned_in_digest: false,
    char_count: 10,
    revision_count: 1,
    created_by_subject: null,
    updated_by_subject: null,
    last_confirmed_generation: 0,
    last_confirmed_at: null,
    created_at: null,
    updated_at: null,
    ...extra,
  };
}

function revision(number: number, body: string): CanonRevision {
  return {
    revision: number,
    summary: `rev ${number}`,
    change_note: null,
    created_by_subject: null,
    created_at: null,
    body_md: body,
  };
}

test("groups appear in the fixed reading order, not insertion order", () => {
  const groups = groupPagesBySection([
    page("reference", "ref-one"),
    page("identity", "who-we-are"),
    page("charter", "company-charter"),
  ]);

  assert.deepEqual(
    groups.map((group) => group.section),
    ["identity", "charter", "reference"],
  );
});

test("a section with no pages is omitted rather than rendered empty", () => {
  const groups = groupPagesBySection([page("rules", "naming-rules")]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].section, "rules");
  assert.ok(groups.every((group) => group.pages.length > 0));
});

test("pages keep their order inside a section", () => {
  const groups = groupPagesBySection([
    page("rules", "first"),
    page("rules", "second"),
  ]);

  assert.deepEqual(groups[0].pages.map((entry) => entry.slug), ["first", "second"]);
});

test("a section the frontend does not know is kept, not dropped", () => {
  // The backend owns the section list. If it grows, a page in a new section must
  // still be visible rather than silently disappearing from the list.
  const groups = groupPagesBySection([
    page("reference", "ref-one"),
    page("appendix" as CanonPage["section"], "future-section"),
  ]);

  assert.deepEqual(
    groups.map((group) => group.section),
    ["reference", "appendix"],
  );
});

test("every known section has a label", () => {
  const groups = groupPagesBySection(SECTION_ORDER.map((section) => page(section, `${section}-page`)));

  assert.equal(groups.length, SECTION_ORDER.length);
  assert.ok(groups.every((group) => group.label.length > 0));
});

test("an empty list produces no groups", () => {
  assert.deepEqual(groupPagesBySection([]), []);
});

test("body length state follows the warn and hard caps", () => {
  assert.equal(bodyLengthState(0), "ok");
  assert.equal(bodyLengthState(BODY_WARN_CHARS - 1), "ok");
  assert.equal(bodyLengthState(BODY_WARN_CHARS), "warn");
  assert.equal(bodyLengthState(BODY_MAX_CHARS), "warn");
  assert.equal(bodyLengthState(BODY_MAX_CHARS + 1), "over");
});

test("a length delta is described as a delta, never as a diff", () => {
  const older = revision(1, "short");
  const longer = bodyLengthDelta(revision(2, "short and longer"), older);

  assert.equal(longer, "+11 characters changed");
  assert.ok(!/diff/i.test(longer));
});

test("a shrink and an unchanged length read correctly", () => {
  assert.equal(bodyLengthDelta(revision(2, "abc"), revision(1, "abcdef")), "−3 characters changed");
  assert.equal(bodyLengthDelta(revision(2, "abc"), revision(1, "xyz")), "No length change");
});

test("the first revision says so instead of inventing a comparison", () => {
  const message = bodyLengthDelta(revision(1, "abc"), undefined);

  assert.match(message, /First revision/);
  assert.ok(!/\d+ characters changed/.test(message));
});

test("an empty room id becomes null, never an empty string", () => {
  const payload = formToPayload({
    slug: " company-charter ",
    title: " Company charter ",
    summary: " What we are for. ",
    section: "charter",
    roomId: "   ",
    priority: "100",
    pinnedInDigest: true,
    body: "Body.",
  });

  assert.equal(payload.room_id, null);
  assert.equal(payload.slug, "company-charter");
  assert.equal(payload.title, "Company charter");
  assert.equal(payload.summary, "What we are for.");
  assert.equal(payload.pinned_in_digest, true);
});

test("a real room id is kept", () => {
  const payload = formToPayload({
    slug: "s",
    title: "t",
    summary: "x",
    section: "reference",
    roomId: " 7f2a4c1e-0000-4000-8000-000000000000 ",
    priority: "5",
    pinnedInDigest: false,
    body: "b",
  });

  assert.equal(payload.room_id, "7f2a4c1e-0000-4000-8000-000000000000");
  assert.equal(payload.priority, 5);
});

test("a blank or nonsense priority falls back instead of sending NaN", () => {
  const base = {
    slug: "s",
    title: "t",
    summary: "x",
    section: "reference" as const,
    roomId: "",
    pinnedInDigest: false,
    body: "b",
  };

  assert.equal(formToPayload({ ...base, priority: "" }).priority, 100);
  assert.equal(formToPayload({ ...base, priority: "abc" }).priority, 100);
});

test("the charter renders above the derived content in the room detail pane", () => {
  // The acceptance for this packet is that a reader meets the charter before the
  // snapshot and never has to guess which is which. That is a property of the
  // JSX order, so it is asserted against the source rather than trusted.
  const source = readFileSync(new URL("../../src/pages/Palace.tsx", import.meta.url), "utf8");

  const charterAt = source.indexOf("authored, authoritative");
  const drawersAt = source.indexOf("Representative drawers");

  assert.ok(charterAt > 0, "the charter block must be labelled");
  assert.ok(drawersAt > 0, "the derived content must still be present");
  assert.ok(charterAt < drawersAt, "the charter must render before the derived content");
});

test("the charter is never labelled as a snapshot", () => {
  // The word "snapshot" is allowed in the empty state, because design section 10
  // mandates the sentence "This room has a derived snapshot. Write a charter to
  // state what this room is for." — that sentence contrasts the two. What is
  // forbidden is labelling the charter itself as a snapshot.
  const source = readFileSync(new URL("../../src/pages/Palace.tsx", import.meta.url), "utf8");
  const start = source.indexOf("authored, authoritative");
  const labelArea = source.slice(start - 400, start + 400);

  assert.ok(labelArea.length > 0);
  assert.ok(!/charter[^.]{0,40}snapshot/i.test(labelArea), "the charter must not be described as a snapshot");
  assert.ok(!/snapshot[^.]{0,40}charter/i.test(labelArea), "a snapshot must not be described as a charter");
});

test("the empty state uses the mandated contrast sentence", () => {
  const source = readFileSync(new URL("../../src/pages/Palace.tsx", import.meta.url), "utf8");

  assert.ok(
    source.includes(
      "This room has a derived snapshot. Write a charter to state what this room is for.",
    ),
  );
});
