import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

import {
  IMPORT_MAX_FILES,
  IMPORT_MAX_FILE_BYTES,
  formatBytes,
  importActionLabel,
  importPreflight,
  isImportableName,
  tallyImportActions,
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

// --- folder import ---------------------------------------------------------

const kb = (n: number) => ({ name: `page-${n}.md`, size: 1024 });

test("a folder inside every limit passes preflight", () => {
  const result = importPreflight([kb(1), kb(2), { name: "notes.txt", size: 10 }]);
  assert.equal(result.ok, true);
  assert.deepEqual(result.problems, []);
  assert.equal(result.fileCount, 3);
});

test("an empty selection is not a silent no-op", () => {
  const result = importPreflight([]);
  assert.equal(result.ok, false);
  assert.equal(result.problems.length, 1);
});

test("more files than the limit is refused, and the count is named", () => {
  const files = Array.from({ length: IMPORT_MAX_FILES + 1 }, (_, index) => kb(index));
  const result = importPreflight(files);
  assert.equal(result.ok, false);
  assert.match(result.problems.join(" "), new RegExp(String(IMPORT_MAX_FILES)));
});

test("one oversize file is refused even when the total is small", () => {
  const result = importPreflight([
    kb(1),
    { name: "huge.md", size: IMPORT_MAX_FILE_BYTES + 1 },
  ]);
  assert.equal(result.ok, false);
  assert.match(result.problems.join(" "), /huge\.md/);
});

test("a total over the batch limit is refused", () => {
  const big = { name: "big.md", size: IMPORT_MAX_FILE_BYTES };
  const result = importPreflight(Array.from({ length: 25 }, () => big));
  assert.equal(result.ok, false);
  assert.match(result.problems.join(" "), /total limit/);
});

test("only markdown-ish extensions are importable", () => {
  assert.equal(isImportableName("a.md"), true);
  assert.equal(isImportableName("a.MDX"), true);
  assert.equal(isImportableName("a.txt"), true);
  assert.equal(isImportableName("a.pdf"), false);
  assert.equal(isImportableName("a.md.bak"), false);
});

test("preflight names the files of the wrong kind", () => {
  const result = importPreflight([kb(1), { name: "scan.pdf", size: 20 }]);
  assert.equal(result.ok, false);
  assert.match(result.problems.join(" "), /scan\.pdf/);
});

test("bytes are formatted for a human", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(250_000), "244 KB");
  assert.equal(formatBytes(5_000_000), "4.8 MB");
});

test("actions read as what they do, not as api values", () => {
  assert.equal(importActionLabel("create"), "New page");
  assert.equal(importActionLabel("skip-unchanged"), "Unchanged");
  assert.equal(importActionLabel("conflict"), "Conflict");
});

test("an unanswered conflict is never counted as a write", () => {
  const tally = tallyImportActions([{ action: "conflict" }], {}, ["charter"]);
  assert.equal(tally.conflict, 1);
  assert.equal(tally.writable, 0, "silence must not become an overwrite");
});

test("an explicit conflict decision becomes a write", () => {
  const applied = tallyImportActions([{ action: "conflict" }], { charter: "apply" }, ["charter"]);
  const renamed = tallyImportActions([{ action: "conflict" }], { charter: "rename" }, ["charter"]);
  const skipped = tallyImportActions([{ action: "conflict" }], { charter: "skip" }, ["charter"]);
  assert.equal(applied.writable, 1);
  assert.equal(renamed.writable, 1);
  assert.equal(skipped.writable, 0);
});

test("the tally separates new, updated and unchanged", () => {
  const tally = tallyImportActions(
    [
      { action: "create" },
      { action: "update" },
      { action: "skip-unchanged" },
      { action: "conflict" },
    ],
    { d: "apply" },
    ["a", "b", "c", "d"],
  );
  assert.equal(tally.create, 1);
  assert.equal(tally.update, 1);
  assert.equal(tally.unchanged, 1);
  assert.equal(tally.conflict, 1);
  assert.equal(tally.writable, 3);
});

test("an unchanged folder offers nothing to commit", () => {
  const tally = tallyImportActions(
    [{ action: "skip-unchanged" }, { action: "skip-unchanged" }],
    {},
    ["a", "b"],
  );
  assert.equal(tally.writable, 0, "a button that writes nothing should be disabled");
});
