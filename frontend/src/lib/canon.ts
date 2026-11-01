// Canon UI logic that does not need React.
//
// These live outside the page so they can be tested directly. The page imports
// them; nothing here touches the DOM or the API.
//
// The cap numbers are the ones the backend enforces (services/canon.py). They are
// duplicated here only to drive counters and warnings, never to make a decision
// the backend would then reject: the editor warns, the API refuses.

import type { CanonPage, CanonRevision, CanonSection } from "../api/types.ts";

export const SECTION_ORDER: readonly CanonSection[] = [
  "identity",
  "rules",
  "glossary",
  "runbook",
  "charter",
  "faq",
  "reference",
] as const;

export const SECTION_LABELS: Record<CanonSection, string> = {
  identity: "Identity",
  rules: "Rules",
  glossary: "Glossary",
  runbook: "Runbook",
  charter: "Charter",
  faq: "FAQ",
  reference: "Reference",
};

export const BODY_WARN_CHARS = 8000;
export const BODY_MAX_CHARS = 20000;
export const SUMMARY_MAX_CHARS = 200;

export interface CanonSectionGroup {
  section: CanonSection;
  label: string;
  pages: CanonPage[];
}

/**
 * Group pages by section in the fixed reading order.
 *
 * Only sections that hold pages appear, so the page never renders an empty
 * heading. A section the backend adds later but this list does not know would be
 * dropped silently, so unknown sections are appended rather than lost.
 */
export function groupPagesBySection(pages: readonly CanonPage[]): CanonSectionGroup[] {
  const known = new Set<string>(SECTION_ORDER);
  const buckets = new Map<string, CanonPage[]>();
  for (const page of pages) {
    const bucket = buckets.get(page.section);
    if (bucket) bucket.push(page);
    else buckets.set(page.section, [page]);
  }

  const ordered: CanonSectionGroup[] = [];
  for (const section of SECTION_ORDER) {
    const bucket = buckets.get(section);
    if (bucket && bucket.length > 0) {
      ordered.push({ section, label: SECTION_LABELS[section], pages: bucket });
    }
  }
  for (const [section, bucket] of buckets) {
    if (!known.has(section) && bucket.length > 0) {
      ordered.push({
        section: section as CanonSection,
        label: section,
        pages: bucket,
      });
    }
  }
  return ordered;
}

export type BodyLengthState = "ok" | "warn" | "over";

export function bodyLengthState(length: number): BodyLengthState {
  if (length > BODY_MAX_CHARS) return "over";
  if (length >= BODY_WARN_CHARS) return "warn";
  return "ok";
}

/**
 * Describe how a revision's length differs from the one before it.
 *
 * This is a length delta, not a diff. The UI labels it that way because calling
 * a character count a diff would overstate what the reader is being shown.
 */
export function bodyLengthDelta(current: CanonRevision, older: CanonRevision | undefined): string {
  if (!older) return "First revision of this page; no earlier length to compare against.";
  const delta = current.body_md.length - older.body_md.length;
  if (delta === 0) return "No length change";
  const sign = delta > 0 ? "+" : "−";
  return `${sign}${Math.abs(delta)} characters changed`;
}

export interface CanonPayload {
  slug: string;
  title: string;
  summary: string;
  section: CanonSection;
  body: string;
  room_id: string | null;
  priority: number;
  pinned_in_digest: boolean;
}

/**
 * Turn editor field strings into the API payload.
 *
 * An empty Room ID becomes null rather than an empty string: the API expects a
 * UUID or nothing, and "" would be a validation error the user cannot see the
 * cause of. A blank priority falls back to the default instead of NaN.
 */
export function formToPayload(form: {
  slug: string;
  title: string;
  summary: string;
  section: CanonSection;
  roomId: string;
  priority: string;
  pinnedInDigest: boolean;
  body: string;
}): CanonPayload {
  const trimmedRoom = form.roomId.trim();
  const parsedPriority = Number.parseInt(form.priority, 10);
  return {
    slug: form.slug.trim(),
    title: form.title.trim(),
    summary: form.summary.trim(),
    section: form.section,
    body: form.body,
    room_id: trimmedRoom === "" ? null : trimmedRoom,
    priority: Number.isFinite(parsedPriority) ? parsedPriority : 100,
    pinned_in_digest: form.pinnedInDigest,
  };
}

// --- Folder import --------------------------------------------------------
//
// These mirror the server's caps so the operator sees a problem before
// uploading. They are a courtesy, not a gate: the API refuses what it must
// refuse, and this file never decides that an import is allowed.

export const IMPORT_MAX_FILES = 500;
export const IMPORT_MAX_FILE_BYTES = 250_000;
export const IMPORT_MAX_TOTAL_BYTES = 5_000_000;
export const IMPORT_EXTENSIONS = [".md", ".mdx", ".txt"] as const;

export interface ImportCandidate {
  name: string;
  size: number;
}

export interface ImportPreflight {
  ok: boolean;
  problems: string[];
  fileCount: number;
  totalBytes: number;
}

export function importPreflight(candidates: ImportCandidate[]): ImportPreflight {
  const problems: string[] = [];
  const totalBytes = candidates.reduce((sum, file) => sum + file.size, 0);

  if (candidates.length === 0) {
    problems.push("Choose a folder first. Nothing is uploaded until you preview it.");
  }
  if (candidates.length > IMPORT_MAX_FILES) {
    problems.push(
      `${candidates.length} files is over the ${IMPORT_MAX_FILES} file limit. Import a smaller folder.`,
    );
  }
  const tooBig = candidates.filter((file) => file.size > IMPORT_MAX_FILE_BYTES);
  if (tooBig.length > 0) {
    problems.push(
      `${tooBig.length} file${tooBig.length === 1 ? " is" : "s are"} over ${formatBytes(IMPORT_MAX_FILE_BYTES)}: ${tooBig
        .slice(0, 3)
        .map((file) => file.name)
        .join(", ")}${tooBig.length > 3 ? ", and more" : ""}.`,
    );
  }
  if (totalBytes > IMPORT_MAX_TOTAL_BYTES) {
    problems.push(
      `The folder is ${formatBytes(totalBytes)}, over the ${formatBytes(IMPORT_MAX_TOTAL_BYTES)} total limit.`,
    );
  }
  const wrongKind = candidates.filter((file) => !isImportableName(file.name));
  if (wrongKind.length > 0) {
    problems.push(
      `${wrongKind.length} file${wrongKind.length === 1 ? " is" : "s are"} not ${IMPORT_EXTENSIONS.join(", ")}: ${wrongKind
        .slice(0, 3)
        .map((file) => file.name)
        .join(", ")}${wrongKind.length > 3 ? ", and more" : ""}.`,
    );
  }

  return { ok: problems.length === 0, problems, fileCount: candidates.length, totalBytes };
}

export function isImportableName(name: string): boolean {
  const lower = name.toLowerCase();
  return IMPORT_EXTENSIONS.some((extension) => lower.endsWith(extension));
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(0)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function importActionLabel(action: string): string {
  switch (action) {
    case "create":
      return "New page";
    case "update":
      return "Updates draft";
    case "skip-unchanged":
      return "Unchanged";
    case "conflict":
      return "Conflict";
    default:
      return action;
  }
}

export interface ImportTally {
  create: number;
  update: number;
  unchanged: number;
  conflict: number;
  writable: number;
}

/** What the preview header says before the operator commits anything. */
export function tallyImportActions(
  files: { action: string }[],
  decisions: Record<string, string> = {},
  slugs: string[] = [],
): ImportTally {
  const tally: ImportTally = { create: 0, update: 0, unchanged: 0, conflict: 0, writable: 0 };
  files.forEach((file, index) => {
    const slug = slugs[index] ?? "";
    const decision = decisions[slug] ?? "skip";
    switch (file.action) {
      case "create":
        tally.create += 1;
        tally.writable += 1;
        break;
      case "update":
        tally.update += 1;
        tally.writable += 1;
        break;
      case "skip-unchanged":
        tally.unchanged += 1;
        break;
      case "conflict":
        tally.conflict += 1;
        // A conflict only becomes a write when the operator says so. Silence is
        // not consent: an unanswered conflict is skipped, never overwritten.
        if (decision === "apply" || decision === "rename") {
          tally.writable += 1;
        }
        break;
      default:
        break;
    }
  });
  return tally;
}

// --- Drift ----------------------------------------------------------------
//
// The states come from the API. This file only decides how they read and how
// strongly they should be shown; it never decides that drift exists.

export const DRIFT_STATES = ["none", "drift-suspected", "stale"] as const;

export function driftStateLabel(state: string): string {
  switch (state) {
    case "drift-suspected":
      return "Drift suspected";
    case "stale":
      return "Stale";
    case "none":
      return "Current";
    default:
      return state;
  }
}

/** Drift needs a human to look at it; none does not. */
export function isDriftActionable(state: string): boolean {
  return state === "drift-suspected" || state === "stale";
}

export interface CharterFreshness {
  label: string;
  actionable: boolean;
}

/** What the charter panel says about a page, given the room's live generation. */
export function charterFreshness(
  page: { last_confirmed_generation: number; status: string } | null,
  confirmedGeneration: number | null,
): CharterFreshness | null {
  if (!page) {
    return null;
  }
  const confirmed = page.last_confirmed_generation ?? 0;
  if (confirmed === 0) {
    return page.status === "published"
      ? { label: "Stale: never ratified", actionable: true }
      : { label: "Never ratified", actionable: false };
  }
  if (confirmedGeneration !== null && confirmedGeneration > confirmed) {
    return {
      label: `Drift suspected: room is at generation ${confirmedGeneration}, confirmed at ${confirmed}`,
      actionable: true,
    };
  }
  return { label: `Confirmed at generation ${confirmed}`, actionable: false };
}
