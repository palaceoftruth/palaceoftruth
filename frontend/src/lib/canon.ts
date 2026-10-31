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
