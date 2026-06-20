import { useEffect, useMemo, useState } from "react";
import {
  Check,
  Clock3,
  ExternalLink,
  Inbox,
  Loader2,
  Pin,
  RefreshCw,
  ShieldCheck,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { ReviewInboxAction, ReviewInboxItem, ReviewInboxResponse } from "../api/types";
import PageHeader from "../components/PageHeader";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";

type Filter = "active" | "all" | "needs_source" | "conflicting" | "stale" | "pinned";

const FILTERS: Array<{ value: Filter; label: string }> = [
  { value: "active", label: "Active" },
  { value: "all", label: "All" },
  { value: "needs_source", label: "Needs source" },
  { value: "conflicting", label: "Conflicts" },
  { value: "stale", label: "Stale" },
  { value: "pinned", label: "Pinned" },
];

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatKind(value: string): string {
  return value.replace(/^candidate_/, "").replace(/_/g, " ");
}

function confidenceLabel(value: number | null): string {
  if (value == null) return "No confidence score";
  return `${Math.round(value * 100)}% confidence`;
}

function freshnessClasses(freshness: ReviewInboxItem["freshness"]): string {
  if (freshness === "fresh") return "border-emerald-700/50 bg-emerald-950/40 text-emerald-100";
  if (freshness === "needs_source") return "border-amber-700/50 bg-amber-950/40 text-amber-100";
  if (freshness === "conflicting") return "border-rose-700/50 bg-rose-950/40 text-rose-100";
  return "border-zinc-700 bg-zinc-950/80 text-zinc-300";
}

function summaryStats(data: ReviewInboxResponse | null) {
  const summary = data?.summary;
  return [
    { label: "Candidates", value: summary?.total ?? 0 },
    { label: "Needs source", value: summary?.needs_source ?? 0 },
    { label: "Conflicts", value: summary?.conflicting ?? 0 },
    { label: "Stale", value: summary?.stale ?? 0 },
    { label: "Pinned", value: summary?.pinned ?? 0 },
  ];
}

function canAccept(item: ReviewInboxItem): boolean {
  return item.freshness === "fresh" && ["reviewable", "proposed"].includes(item.artifact.status);
}

export default function PalaceReviewInbox() {
  const [data, setData] = useState<ReviewInboxResponse | null>(null);
  const [filter, setFilter] = useState<Filter>("active");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const toast = useToast();

  const load = async (nextFilter = filter) => {
    setLoading(true);
    setLoadError(null);
    try {
      const includeDeferred = nextFilter === "all";
      setData(await api.getReviewInbox({ include_deferred: includeDeferred, limit: 100 }));
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const items = useMemo(() => {
    const all = data?.items ?? [];
    if (filter === "active") return all.filter((item) => !item.deferred);
    if (filter === "all") return all;
    if (filter === "pinned") return all.filter((item) => item.pinned);
    return all.filter((item) => item.freshness === filter);
  }, [data, filter]);

  useEffect(() => {
    setSelectedIds((current) => current.filter((id) => items.some((item) => item.artifact.id === id)));
  }, [items]);

  const selectedCount = selectedIds.length;
  const allVisibleSelected = items.length > 0 && selectedCount === items.length;

  const runAction = async (action: ReviewInboxAction, artifactIds: string[], note?: string) => {
    setActing(`${action}:${artifactIds.join(",")}`);
    try {
      await api.applyReviewInboxAction({
        action,
        artifact_ids: artifactIds,
        actor: "operator",
        note,
      });
      setSelectedIds([]);
      await load();
      toast.success("Review inbox updated");
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setActing(null);
    }
  };

  const changeFilter = (nextFilter: Filter) => {
    setFilter(nextFilter);
    void load(nextFilter);
  };

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Palace"
        title="Review Inbox"
        description="Triage generated memory candidates, weak source support, stale evidence, and curation proposals before anything is promoted."
        actions={
          <button type="button" onClick={() => void load()} className="sb-button-secondary" disabled={loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            Refresh
          </button>
        }
      />

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        {summaryStats(data).map((stat) => (
          <div key={stat.label} className="sb-stat-card">
            <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">{stat.label}</p>
            <p className="mt-2 text-2xl font-semibold text-zinc-50">{stat.value}</p>
          </div>
        ))}
      </section>

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="sb-chip-group">
            {FILTERS.map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => changeFilter(option.value)}
                className={`sb-chip ${filter === option.value ? "sb-chip-active" : "sb-chip-inactive"}`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="inline-flex cursor-pointer items-center gap-2 rounded-2xl border border-zinc-800 bg-zinc-950/80 px-3 py-2 text-sm text-zinc-300">
              <input
                type="checkbox"
                className="h-4 w-4 rounded border-zinc-700 bg-zinc-950 text-sky-500 focus:ring-sky-500/30"
                checked={allVisibleSelected}
                onChange={(event) => {
                  setSelectedIds(event.target.checked ? items.map((item) => item.artifact.id) : []);
                }}
              />
              Select visible
            </label>
            <button
              type="button"
              className="sb-button-secondary"
              disabled={selectedCount === 0 || acting !== null}
              onClick={() => void runAction("pin", selectedIds, "Pinned from Review Inbox batch triage")}
            >
              <Pin className="h-4 w-4" />
              Pin {selectedCount || ""}
            </button>
            <button
              type="button"
              className="sb-button-secondary"
              disabled={selectedCount === 0 || acting !== null}
              onClick={() => void runAction("defer", selectedIds, "Deferred from Review Inbox batch triage")}
            >
              <Clock3 className="h-4 w-4" />
              Defer {selectedCount || ""}
            </button>
          </div>
        </div>

        {loadError ? (
          <StatePanel
            title="Review inbox unavailable"
            description={loadError}
            icon={Inbox}
            variant="error"
          />
        ) : null}

        {loading ? (
          <div className="flex min-h-64 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-950/60">
            <Loader2 className="h-5 w-5 animate-spin text-zinc-400" />
          </div>
        ) : null}

        {!loading && !loadError && items.length === 0 ? (
          <StatePanel
            title="No review candidates"
            description="The current filter has no generated artifacts waiting for operator triage."
            icon={ShieldCheck}
          />
        ) : null}

        {!loading && !loadError && items.length > 0 ? (
          <div className="space-y-3">
            {items.map((item) => {
              const artifact = item.artifact;
              const isSelected = selectedIds.includes(artifact.id);
              const firstSource = artifact.source_item_ids[0];
              return (
                <article key={artifact.id} className="sb-list-card p-4 md:p-5">
                  <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <label className="inline-flex cursor-pointer items-center">
                          <input
                            type="checkbox"
                            className="h-4 w-4 rounded border-zinc-700 bg-zinc-950 text-sky-500 focus:ring-sky-500/30"
                            checked={isSelected}
                            onChange={(event) => {
                              setSelectedIds((current) => (
                                event.target.checked
                                  ? [...current, artifact.id]
                                  : current.filter((id) => id !== artifact.id)
                              ));
                            }}
                            aria-label={`Select ${artifact.target_surface}`}
                          />
                        </label>
                        <span className={`sb-chip ${freshnessClasses(item.freshness)}`}>
                          {item.freshness.replace("_", " ")}
                        </span>
                        <span className="sb-chip border-zinc-700 bg-zinc-950/80 text-zinc-300">
                          {formatKind(artifact.artifact_kind)}
                        </span>
                        {item.pinned ? (
                          <span className="sb-chip border-sky-700/60 bg-sky-950/40 text-sky-100">Pinned</span>
                        ) : null}
                        {item.deferred ? (
                          <span className="sb-chip border-zinc-700 bg-zinc-900/80 text-zinc-300">Deferred</span>
                        ) : null}
                      </div>
                      <h2 className="mt-4 break-words text-lg font-semibold text-zinc-50">
                        {artifact.target_surface}
                      </h2>
                      <p className="mt-2 line-clamp-3 text-sm leading-6 text-zinc-300">{artifact.candidate_body}</p>
                      <div className="mt-4 grid gap-3 text-sm text-zinc-400 md:grid-cols-3">
                        <div>
                          <p className="text-[11px] uppercase tracking-[0.2em] text-zinc-600">Scope</p>
                          <p className="mt-1 break-words text-zinc-300">{item.affected_scope}</p>
                        </div>
                        <div>
                          <p className="text-[11px] uppercase tracking-[0.2em] text-zinc-600">Evidence</p>
                          <p className="mt-1 text-zinc-300">{item.source_count} sources · {confidenceLabel(item.confidence)}</p>
                        </div>
                        <div>
                          <p className="text-[11px] uppercase tracking-[0.2em] text-zinc-600">Updated</p>
                          <p className="mt-1 text-zinc-300">{formatDate(artifact.updated_at)}</p>
                        </div>
                      </div>
                    </div>
                    <div className="flex min-w-52 flex-wrap gap-2 xl:justify-end">
                      {firstSource ? (
                        <Link to={`/items/${firstSource}`} className="sb-button-ghost">
                          <ExternalLink className="h-4 w-4" />
                          Source
                        </Link>
                      ) : null}
                      <button
                        type="button"
                        className="sb-button-secondary"
                        disabled={acting !== null}
                        onClick={() => void runAction("pin", [artifact.id], "Pinned from Review Inbox")}
                      >
                        <Pin className="h-4 w-4" />
                        Pin
                      </button>
                      <button
                        type="button"
                        className="sb-button-secondary"
                        disabled={acting !== null}
                        onClick={() => void runAction("defer", [artifact.id], "Deferred from Review Inbox")}
                      >
                        <Clock3 className="h-4 w-4" />
                        Defer
                      </button>
                      <button
                        type="button"
                        className="sb-button-primary"
                        disabled={acting !== null || !canAccept(item)}
                        onClick={() => void runAction("accept", [artifact.id], "Accepted from Review Inbox")}
                      >
                        {acting === `accept:${artifact.id}` ? (
                          <Loader2 className="h-4 w-4 animate-spin" />
                        ) : (
                          <Check className="h-4 w-4" />
                        )}
                        Accept
                      </button>
                      <button
                        type="button"
                        className="sb-button-secondary"
                        disabled={acting !== null}
                        onClick={() => void runAction("reject", [artifact.id], "Rejected from Review Inbox")}
                      >
                        <X className="h-4 w-4" />
                        Reject
                      </button>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        ) : null}
      </section>
    </div>
  );
}
