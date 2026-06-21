import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ExternalLink, FileSearch, Link as LinkIcon, Route, ShieldCheck, Sparkles, X } from "lucide-react";
import { Link } from "react-router-dom";

import type { ArtifactCitation, RelatedItem } from "../api/types";
import ArtifactCitationView from "./ArtifactCitation";
import RelationshipBadge from "./RelationshipBadge";
import SourceIcon from "./SourceIcon";

export type ProvenanceKind = "raw_source" | "canonical_memory" | "derived_artifact" | "room" | "retrieval_trace";

export interface ProvenanceScore {
  label: string;
  value: number | string;
  tone?: "default" | "good" | "warning";
}

export interface ProvenanceTraceStep {
  title: string;
  detail: string;
}

export interface ProvenanceRelationship {
  item_id: string;
  title: string;
  source_type: string;
  relationship: string;
  confidence: number;
}

export interface ProvenanceDrawerData {
  title: string;
  subtitle?: string | null;
  kind: ProvenanceKind;
  itemId?: string | null;
  sourceType?: string | null;
  sourceUrl?: string | null;
  sourceLabel?: string | null;
  summary?: string | null;
  excerpt?: string | null;
  room?: {
    name?: string | null;
    wing?: string | null;
    scope?: string | null;
  };
  artifact?: ArtifactCitation | null;
  scores?: ProvenanceScore[];
  traceSteps?: ProvenanceTraceStep[];
  relationships?: ProvenanceRelationship[];
  metadata?: Array<{ label: string; value?: string | number | null }>;
}

interface ProvenanceDrawerProps {
  provenance: ProvenanceDrawerData;
  triggerLabel?: string;
  compact?: boolean;
}

const KIND_COPY: Record<ProvenanceKind, { label: string; description: string; className: string }> = {
  raw_source: {
    label: "Raw source",
    description: "Captured source evidence before Palace synthesis.",
    className: "border-sky-700/50 bg-sky-950/40 text-sky-100",
  },
  canonical_memory: {
    label: "Canonical memory",
    description: "Stored Palace memory item available to retrieval.",
    className: "border-emerald-700/50 bg-emerald-950/35 text-emerald-100",
  },
  derived_artifact: {
    label: "Derived artifact",
    description: "Synthesized or extracted evidence derived from captured sources.",
    className: "border-amber-700/50 bg-amber-950/35 text-amber-100",
  },
  room: {
    label: "Room evidence",
    description: "Palace organization evidence from rooms, wings, and memberships.",
    className: "border-violet-700/50 bg-violet-950/35 text-violet-100",
  },
  retrieval_trace: {
    label: "Retrieval trace",
    description: "Ranking and routing evidence for why this result was shown.",
    className: "border-cyan-700/50 bg-cyan-950/35 text-cyan-100",
  },
};

function scoreClassName(tone: ProvenanceScore["tone"] = "default") {
  if (tone === "good") return "border-emerald-800/70 bg-emerald-950/25 text-emerald-100";
  if (tone === "warning") return "border-amber-800/70 bg-amber-950/25 text-amber-100";
  return "border-zinc-800 bg-zinc-950/70 text-zinc-200";
}

function formatScoreValue(value: number | string) {
  if (typeof value === "string") return value;
  if (Number.isInteger(value) && value > 1) return String(value);
  if (value >= 0 && value <= 1) return `${Math.round(value * 100)}%`;
  return value.toFixed(3);
}

function hasContent(provenance: ProvenanceDrawerData) {
  return Boolean(
    provenance.sourceUrl ||
      provenance.itemId ||
      provenance.summary ||
      provenance.excerpt ||
      provenance.artifact ||
      provenance.room ||
      provenance.scores?.length ||
      provenance.traceSteps?.length ||
      provenance.relationships?.length ||
      provenance.metadata?.some((row) => row.value !== null && row.value !== undefined && row.value !== ""),
  );
}

function relationshipFromRelated(item: RelatedItem): ProvenanceRelationship {
  return {
    item_id: item.item_id,
    title: item.title,
    source_type: item.source_type,
    relationship: item.relationship,
    confidence: item.confidence,
  };
}

export function relatedItemsToProvenanceRelationships(items: RelatedItem[]): ProvenanceRelationship[] {
  return items.map(relationshipFromRelated);
}

function DrawerSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-2xl border border-zinc-800 bg-zinc-950/55 p-4">
      <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">{title}</p>
      <div className="mt-3">{children}</div>
    </section>
  );
}

export default function ProvenanceDrawer({ provenance, triggerLabel = "Inspect evidence", compact = false }: ProvenanceDrawerProps) {
  const [open, setOpen] = useState(false);
  const kind = KIND_COPY[provenance.kind];
  const canOpen = hasContent(provenance);
  const sourceHost = useMemo(() => {
    if (!provenance.sourceUrl) return null;
    try {
      return new URL(provenance.sourceUrl).hostname;
    } catch {
      return provenance.sourceUrl;
    }
  }, [provenance.sourceUrl]);

  useEffect(() => {
    if (!open) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        disabled={!canOpen}
        className={compact ? "sb-chip sb-chip-inactive cursor-pointer px-2.5 py-1 disabled:cursor-not-allowed disabled:opacity-50" : "sb-button-secondary disabled:cursor-not-allowed disabled:opacity-50"}
      >
        <FileSearch className="h-4 w-4" />
        {triggerLabel}
      </button>

      {open ? (
        <div className="fixed inset-0 z-50">
          <button
            type="button"
            aria-label="Close provenance drawer"
            className="absolute inset-0 cursor-default bg-slate-950/72 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />
          <aside
            role="dialog"
            aria-modal="true"
            aria-label={`Provenance for ${provenance.title}`}
            className="absolute right-0 top-0 flex h-full w-full max-w-[46rem] flex-col border-l border-zinc-800 bg-slate-950 shadow-[0_24px_90px_rgba(0,0,0,0.55)] sm:w-[min(92vw,46rem)]"
          >
            <div className="flex items-start justify-between gap-4 border-b border-zinc-800 px-5 py-5 md:px-6">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium ${kind.className}`}>
                    {provenance.kind === "derived_artifact" ? <Sparkles className="h-3.5 w-3.5" /> : <ShieldCheck className="h-3.5 w-3.5" />}
                    {kind.label}
                  </span>
                  {provenance.sourceType ? (
                    <span className="inline-flex items-center gap-1.5 rounded-full border border-zinc-800 bg-zinc-950 px-3 py-1 text-xs text-zinc-300">
                      <SourceIcon sourceType={provenance.sourceType} className="h-3.5 w-3.5" />
                      {provenance.sourceType.replace(/_/g, " ")}
                    </span>
                  ) : null}
                </div>
                <h2 className="mt-4 text-xl font-semibold tracking-tight text-zinc-50">{provenance.title}</h2>
                <p className="mt-2 text-sm leading-6 text-zinc-400">{provenance.subtitle ?? kind.description}</p>
              </div>
              <button type="button" onClick={() => setOpen(false)} className="sb-button-ghost shrink-0 px-2.5" aria-label="Close provenance drawer">
                <X className="h-5 w-5" />
              </button>
            </div>

            <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5 md:px-6">
              <DrawerSection title="Evidence links">
                <div className="flex flex-wrap gap-2">
                  {provenance.itemId ? (
                    <Link to={`/items/${provenance.itemId}`} onClick={() => setOpen(false)} className="sb-chip sb-chip-active">
                      <LinkIcon className="h-3.5 w-3.5" />
                      Item {provenance.itemId}
                    </Link>
                  ) : null}
                  {provenance.sourceUrl ? (
                    <a href={provenance.sourceUrl} target="_blank" rel="noopener noreferrer" className="sb-chip sb-chip-inactive">
                      <ExternalLink className="h-3.5 w-3.5" />
                      {provenance.sourceLabel ?? sourceHost ?? "Open source"}
                    </a>
                  ) : null}
                  {!provenance.itemId && !provenance.sourceUrl ? (
                    <p className="text-sm text-zinc-500">No direct source link is available for this evidence.</p>
                  ) : null}
                </div>
              </DrawerSection>

              {provenance.summary || provenance.excerpt ? (
                <DrawerSection title="Captured content">
                  {provenance.summary ? <p className="text-sm leading-7 text-zinc-200">{provenance.summary}</p> : null}
                  {provenance.excerpt ? (
                    <blockquote className="mt-3 border-l-2 border-sky-700/50 pl-3 text-sm leading-7 text-zinc-400">
                      {provenance.excerpt}
                    </blockquote>
                  ) : null}
                </DrawerSection>
              ) : null}

              {provenance.room ? (
                <DrawerSection title="Room and wing">
                  <div className="grid gap-3 sm:grid-cols-3">
                    <div className="rounded-xl border border-zinc-800 bg-zinc-950/70 p-3">
                      <p className="text-xs text-zinc-500">Room</p>
                      <p className="mt-1 text-sm text-zinc-100">{provenance.room.name ?? "Global scope"}</p>
                    </div>
                    <div className="rounded-xl border border-zinc-800 bg-zinc-950/70 p-3">
                      <p className="text-xs text-zinc-500">Wing</p>
                      <p className="mt-1 text-sm text-zinc-100">{provenance.room.wing ?? "Unassigned"}</p>
                    </div>
                    <div className="rounded-xl border border-zinc-800 bg-zinc-950/70 p-3">
                      <p className="text-xs text-zinc-500">Scope</p>
                      <p className="mt-1 text-sm text-zinc-100">{provenance.room.scope ?? "Tenant corpus"}</p>
                    </div>
                  </div>
                </DrawerSection>
              ) : null}

              {provenance.scores?.length ? (
                <DrawerSection title="Confidence and freshness">
                  <div className="grid gap-2 sm:grid-cols-2">
                    {provenance.scores.map((score) => (
                      <div key={`${score.label}-${score.value}`} className={`rounded-xl border p-3 ${scoreClassName(score.tone)}`}>
                        <p className="text-xs opacity-70">{score.label}</p>
                        <p className="mt-1 text-sm font-medium">{formatScoreValue(score.value)}</p>
                      </div>
                    ))}
                  </div>
                </DrawerSection>
              ) : null}

              {provenance.artifact ? (
                <DrawerSection title="Derived artifact">
                  <ArtifactCitationView citation={provenance.artifact} />
                </DrawerSection>
              ) : null}

              {provenance.traceSteps?.length ? (
                <DrawerSection title="Retrieval trace">
                  <div className="space-y-2">
                    {provenance.traceSteps.map((step) => (
                      <div key={`${step.title}-${step.detail}`} className="rounded-xl border border-zinc-800 bg-zinc-950/70 p-3">
                        <div className="flex items-center gap-2 text-sm font-medium text-zinc-100">
                          <Route className="h-4 w-4 text-cyan-200" />
                          {step.title}
                        </div>
                        <p className="mt-1 text-xs leading-5 text-zinc-400">{step.detail}</p>
                      </div>
                    ))}
                  </div>
                </DrawerSection>
              ) : null}

              {provenance.relationships?.length ? (
                <DrawerSection title="Linked relationships">
                  <div className="space-y-2">
                    {provenance.relationships.map((relationship) => (
                      <Link
                        key={`${relationship.relationship}-${relationship.item_id}`}
                        to={`/items/${relationship.item_id}`}
                        onClick={() => setOpen(false)}
                        className="flex min-w-0 flex-col gap-3 rounded-xl border border-zinc-800 bg-zinc-950/70 p-3 transition hover:border-zinc-600 sm:flex-row sm:items-center sm:justify-between"
                      >
                        <div className="flex min-w-0 items-start gap-3">
                          <SourceIcon sourceType={relationship.source_type} className="mt-0.5 h-4 w-4 shrink-0" />
                          <div className="min-w-0">
                            <p className="truncate text-sm font-medium text-zinc-100">{relationship.title}</p>
                            <p className="mt-1 text-xs text-zinc-500">Item {relationship.item_id}</p>
                          </div>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <RelationshipBadge relationship={relationship.relationship} />
                          <span className="text-xs text-zinc-500">{Math.round(relationship.confidence * 100)}%</span>
                        </div>
                      </Link>
                    ))}
                  </div>
                </DrawerSection>
              ) : null}

              {provenance.metadata?.some((row) => row.value !== null && row.value !== undefined && row.value !== "") ? (
                <DrawerSection title="Metadata">
                  <dl className="divide-y divide-zinc-800/70">
                    {provenance.metadata.map((row) => row.value !== null && row.value !== undefined && row.value !== "" ? (
                      <div key={row.label} className="flex flex-col gap-1 py-2 sm:flex-row sm:justify-between sm:gap-4">
                        <dt className="text-xs uppercase tracking-[0.2em] text-zinc-500">{row.label}</dt>
                        <dd className="break-words text-sm text-zinc-200 sm:text-right">{row.value}</dd>
                      </div>
                    ) : null)}
                  </dl>
                </DrawerSection>
              ) : null}
            </div>
          </aside>
        </div>
      ) : null}
    </>
  );
}
