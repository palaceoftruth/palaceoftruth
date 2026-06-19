import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, Clock3, Eye, Pause, Play, Plus, RefreshCw, RotateCcw, Trash2, Youtube } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { SourceSubscription, SourceSubscriptionEntry, SourceSubscriptionPreview } from "../api/types";
import PageHeader from "../components/PageHeader";
import StatePanel from "../components/StatePanel";
import StatsCard from "../components/StatsCard";
import { useToast } from "../context/ToastContext";

const POLL_INTERVAL_OPTIONS = [
  { label: "15 min", value: 900 },
  { label: "30 min", value: 1800 },
  { label: "1 hour", value: 3600 },
  { label: "6 hours", value: 21600 },
  { label: "24 hours", value: 86400 },
];

function formatRelative(value: string | null): string {
  if (!value) return "Never";
  const diff = Date.now() - new Date(value).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function formatTimestamp(value: string | null): string {
  if (!value) return "Never";
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function formatPollInterval(seconds: number): string {
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} hr`;
  return `${Math.round(seconds / 86400)} day`;
}

function parseTags(value: string): string[] {
  return value.split(",").map((tag) => tag.trim()).filter(Boolean);
}

function sourceName(source: SourceSubscription): string {
  return source.display_name || source.external_url || source.source_url;
}

function backfillSummary(source: SourceSubscription): string {
  const backfill = source.cursor.backfill;
  if (!backfill || typeof backfill !== "object") return "Off";
  const policy = backfill as { enabled?: unknown; completed?: unknown; limit?: unknown; remaining?: unknown; published_after?: unknown };
  if (!policy.enabled && !policy.completed) return "Off";
  if (policy.completed) return "Completed";
  if (typeof policy.remaining === "number") return `${policy.remaining} left`;
  if (typeof policy.limit === "number") return `${policy.limit} max`;
  if (typeof policy.published_after === "string" && policy.published_after) {
    return `Since ${new Date(policy.published_after).toLocaleDateString()}`;
  }
  return "On";
}

function apiErrorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function statusChip(status: SourceSubscription["status"], lastError: string | null) {
  if (status === "paused") {
    return "border-zinc-700 bg-zinc-950/80 text-zinc-300";
  }
  if (lastError) {
    return "border-amber-700/40 bg-amber-950/30 text-amber-200";
  }
  return "border-emerald-700/40 bg-emerald-950/30 text-emerald-200";
}

function entryChip(status: SourceSubscriptionEntry["status"]) {
  if (status === "captured") return "border-emerald-700/40 bg-emerald-950/30 text-emerald-200";
  if (status === "failed") return "border-rose-700/40 bg-rose-950/30 text-rose-200";
  if (status === "skipped") return "border-zinc-700 bg-zinc-950/80 text-zinc-300";
  if (status === "queued") return "border-sky-700/40 bg-sky-950/30 text-sky-200";
  return "border-zinc-700 bg-zinc-950/80 text-zinc-400";
}

function AddSourceForm({ onCancel, onSuccess }: { onCancel: () => void; onSuccess: (source: SourceSubscription) => void }) {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [tags, setTags] = useState("");
  const [pollInterval, setPollInterval] = useState(3600);
  const [backfillEnabled, setBackfillEnabled] = useState(false);
  const [backfillLimit, setBackfillLimit] = useState("25");
  const [backfillSince, setBackfillSince] = useState("");
  const [preview, setPreview] = useState<SourceSubscriptionPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const parsedBackfillLimit = backfillLimit.trim() ? Number(backfillLimit) : undefined;
  const body = {
    source_url: url.trim(),
    display_name: displayName.trim() || undefined,
    auto_tags: parseTags(tags),
    poll_interval_seconds: pollInterval,
    backfill_enabled: backfillEnabled,
    backfill_limit: backfillEnabled ? parsedBackfillLimit : undefined,
    backfill_published_after: backfillEnabled && backfillSince ? new Date(`${backfillSince}T00:00:00`).toISOString() : undefined,
  };
  const backfillReady = !backfillEnabled || Boolean(parsedBackfillLimit || backfillSince);

  const handlePreview = async () => {
    if (!url.trim()) return;
    setPreviewing(true);
    try {
      const resolved = await api.previewSourceSubscription(body);
      setPreview(resolved);
    } catch (err) {
      setPreview(null);
      toast.error(apiErrorMessage(err));
    } finally {
      setPreviewing(false);
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    try {
      const created = await api.createSourceSubscription(body);
      toast.success("Source added");
      onSuccess(created);
    } catch (err) {
      toast.error(apiErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="sb-panel sb-panel-padding space-y-5">
      <div>
        <p className="sb-section-title">New YouTube channel</p>
        <p className="mt-2 max-w-3xl text-sm leading-7 text-zinc-400">
          Palace watches new uploads by default. Turn on bounded backfill when older uploads should be captured too.
        </p>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <label className="mb-1 block text-xs text-zinc-400">
            YouTube channel URL or handle <span className="text-red-400">*</span>
          </label>
          <input
            required
            value={url}
            onChange={(event) => {
              setUrl(event.target.value);
              setPreview(null);
            }}
            placeholder="https://www.youtube.com/@example"
            className="sb-input"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-zinc-400">Display name</label>
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="Optional label"
            className="sb-input"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-zinc-400">Auto-tags</label>
          <input
            value={tags}
            onChange={(event) => setTags(event.target.value)}
            placeholder="research, market"
            className="sb-input"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-zinc-400">Poll interval</label>
          <select value={pollInterval} onChange={(event) => setPollInterval(Number(event.target.value))} className="sb-select">
            {POLL_INTERVAL_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="sm:col-span-2">
          <label className="flex items-center gap-2 text-sm text-zinc-200">
            <input
              type="checkbox"
              checked={backfillEnabled}
              onChange={(event) => {
                setBackfillEnabled(event.target.checked);
                setPreview(null);
              }}
              className="h-4 w-4 rounded border-zinc-700 bg-zinc-950 text-sky-500 focus:ring-sky-500"
            />
            Backfill older uploads
          </label>
          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-zinc-400">Maximum uploads</label>
              <input
                type="number"
                min={1}
                max={500}
                value={backfillLimit}
                disabled={!backfillEnabled}
                onChange={(event) => {
                  setBackfillLimit(event.target.value);
                  setPreview(null);
                }}
                className="sb-input disabled:cursor-not-allowed disabled:opacity-50"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-zinc-400">Published after</label>
              <input
                type="date"
                value={backfillSince}
                disabled={!backfillEnabled}
                onChange={(event) => {
                  setBackfillSince(event.target.value);
                  setPreview(null);
                }}
                className="sb-input disabled:cursor-not-allowed disabled:opacity-50"
              />
            </div>
          </div>
        </div>
      </div>
      {preview ? (
        <div className="sb-panel-muted p-4">
          <div className="flex items-start gap-3">
            <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-300" />
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-zinc-100">{preview.display_name || preview.external_url}</p>
              <p className="mt-1 truncate text-xs text-zinc-500">{preview.external_url || preview.source_url}</p>
              <p className="mt-3 text-xs text-zinc-400">Resolved channel ID {preview.external_id}</p>
              <p className="mt-1 text-xs text-zinc-400">
                {preview.backfill_enabled ? "Backfill will run with the selected bounds." : "Backfill is off for this source."}
              </p>
            </div>
          </div>
        </div>
      ) : null}
      <div className="flex flex-wrap justify-end gap-2">
        <button type="button" onClick={onCancel} className="sb-button-secondary">Cancel</button>
        <button type="button" onClick={handlePreview} disabled={previewing || !url.trim() || !backfillReady} className="sb-button-secondary">
          <Eye className="h-4 w-4" />
          {previewing ? "Previewing..." : "Preview"}
        </button>
        <button type="submit" disabled={submitting || !url.trim() || !backfillReady} className="sb-button-primary">
          <Plus className="h-4 w-4" />
          {submitting ? "Adding..." : "Add source"}
        </button>
      </div>
    </form>
  );
}

export default function Sources() {
  const toast = useToast();
  const [sources, setSources] = useState<SourceSubscription[]>([]);
  const [entries, setEntries] = useState<SourceSubscriptionEntry[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [retryingEntryId, setRetryingEntryId] = useState<string | null>(null);

  const selected = sources.find((source) => source.id === selectedId) ?? sources[0] ?? null;
  const activeCount = sources.filter((source) => source.status === "active").length;
  const capturedCount = entries.filter((entry) => entry.status === "captured").length;
  const failedCount = entries.filter((entry) => entry.status === "failed").length;

  const selectedEntries = useMemo(
    () => entries.filter((entry) => !selected || entry.subscription_id === selected.id),
    [entries, selected],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [sourceResponse, entryResponse] = await Promise.all([
        api.listSourceSubscriptions(),
        api.listRecentSourceSubscriptionEntries(100),
      ]);
      setSources(sourceResponse.subscriptions);
      setEntries(entryResponse.entries);
      setSelectedId((current) => current && sourceResponse.subscriptions.some((source) => source.id === current)
        ? current
        : sourceResponse.subscriptions[0]?.id ?? null);
    } catch (err) {
      setLoadError(apiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const replaceSource = (updated: SourceSubscription) => {
    setSources((current) => current.map((source) => source.id === updated.id ? updated : source));
  };

  const runAction = async (source: SourceSubscription, action: "sync" | "pause" | "resume" | "delete") => {
    setBusyId(source.id);
    try {
      if (action === "sync") {
        await api.syncSourceSubscription(source.id);
        toast.success("Manual sync queued");
      } else if (action === "pause") {
        replaceSource(await api.pauseSourceSubscription(source.id));
        toast.success("Source paused");
      } else if (action === "resume") {
        replaceSource(await api.resumeSourceSubscription(source.id));
        toast.success("Source resumed");
      } else {
        await api.deleteSourceSubscription(source.id);
        setSources((current) => current.filter((candidate) => candidate.id !== source.id));
        setEntries((current) => current.filter((entry) => entry.subscription_id !== source.id));
        setSelectedId(null);
        toast.success("Source removed");
      }
      if (action === "sync") await load();
    } catch (err) {
      toast.error(apiErrorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const retryEntry = async (entry: SourceSubscriptionEntry) => {
    setRetryingEntryId(entry.id);
    try {
      await api.retrySourceSubscriptionEntry(entry.id);
      toast.success("Entry retry queued");
      await load();
    } catch (err) {
      toast.error(apiErrorMessage(err));
    } finally {
      setRetryingEntryId(null);
    }
  };

  if (loading) {
    return <StatePanel icon={RefreshCw} title="Loading sources" description="Checking source subscriptions and recent capture state." />;
  }

  if (loadError) {
    return (
      <StatePanel
        icon={AlertCircle}
        title="Sources could not load"
        description={loadError}
        variant="error"
        action={<button type="button" onClick={() => void load()} className="sb-button-secondary">Retry</button>}
      />
    );
  }

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Capture"
        title="Sources"
        description="Manage recurring source subscriptions separately from one-time captures and feed polling."
        actions={
          <button type="button" onClick={() => setShowAddForm((value) => !value)} className="sb-button-primary">
            <Plus className="h-4 w-4" />
            Add source
          </button>
        }
        meta={
          <>
            <span className="sb-chip sb-chip-active">YouTube channels</span>
            <span className="sb-chip sb-chip-inactive">Optional bounded backfill</span>
          </>
        }
      />

      {showAddForm ? (
        <AddSourceForm
          onCancel={() => setShowAddForm(false)}
          onSuccess={(source) => {
            setSources((current) => [source, ...current]);
            setSelectedId(source.id);
            setShowAddForm(false);
          }}
        />
      ) : null}

      <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <StatsCard label="Sources" value={sources.length} icon={Youtube} detail={`${activeCount} active`} />
        <StatsCard label="Captured" value={capturedCount} icon={CheckCircle2} detail="Recent subscription entries" />
        <StatsCard label="Failed" value={failedCount} icon={AlertCircle} detail="Needs operator review" />
      </section>

      {sources.length === 0 ? (
        <StatePanel
          icon={Youtube}
          title="No sources are connected yet."
          description="Add a YouTube channel to let Palace watch for new uploads as recurring source entries."
          variant="empty"
          action={<button type="button" onClick={() => setShowAddForm(true)} className="sb-button-primary">Add source</button>}
        />
      ) : (
        <section className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.45fr)]">
          <div className="space-y-3">
            {sources.map((source) => (
              <button
                key={source.id}
                type="button"
                onClick={() => setSelectedId(source.id)}
                className={`sb-list-card w-full cursor-pointer p-4 text-left ${selected?.id === source.id ? "border-sky-700/60 bg-sky-950/20" : ""}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-zinc-100">{sourceName(source)}</p>
                    <p className="mt-1 truncate text-xs text-zinc-500">{source.external_url || source.source_url}</p>
                  </div>
                  <span className={`sb-chip shrink-0 ${statusChip(source.status, source.last_error)}`}>
                    {source.status === "active" && source.last_error ? "needs review" : source.status}
                  </span>
                </div>
                <div className="mt-4 flex flex-wrap gap-2 text-xs text-zinc-500">
                  <span>Every {formatPollInterval(source.poll_interval_seconds)}</span>
                  <span>Checked {formatRelative(source.last_checked_at)}</span>
                  <span>Backfill {backfillSummary(source)}</span>
                </div>
              </button>
            ))}
          </div>

          <div className="sb-panel sb-panel-padding space-y-5">
            {selected ? (
              <>
                <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0">
                    <p className="sb-section-title">Selected source</p>
                    <h2 className="mt-3 truncate text-2xl font-semibold tracking-tight text-zinc-100">{sourceName(selected)}</h2>
                    <p className="mt-2 truncate text-sm text-zinc-500">{selected.external_url || selected.source_url}</p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <button type="button" disabled={busyId === selected.id || selected.status !== "active"} onClick={() => void runAction(selected, "sync")} className="sb-button-secondary">
                      <RefreshCw className="h-4 w-4" />
                      Sync
                    </button>
                    {selected.status === "paused" ? (
                      <button type="button" disabled={busyId === selected.id} onClick={() => void runAction(selected, "resume")} className="sb-button-secondary">
                        <Play className="h-4 w-4" />
                        Resume
                      </button>
                    ) : (
                      <button type="button" disabled={busyId === selected.id} onClick={() => void runAction(selected, "pause")} className="sb-button-secondary">
                        <Pause className="h-4 w-4" />
                        Pause
                      </button>
                    )}
                    <button type="button" disabled={busyId === selected.id} onClick={() => void runAction(selected, "delete")} className="sb-button-secondary">
                      <Trash2 className="h-4 w-4" />
                      Remove
                    </button>
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  <div className="sb-panel-muted p-4">
                    <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">Last checked</p>
                    <p className="mt-2 text-sm text-zinc-100">{formatTimestamp(selected.last_checked_at)}</p>
                  </div>
                  <div className="sb-panel-muted p-4">
                    <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">Last discovered</p>
                    <p className="mt-2 text-sm text-zinc-100">{formatTimestamp(selected.last_discovered_at)}</p>
                  </div>
                  <div className="sb-panel-muted p-4">
                    <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">Tags</p>
                    <p className="mt-2 text-sm text-zinc-100">{selected.auto_tags.length ? selected.auto_tags.join(", ") : "None"}</p>
                  </div>
                  <div className="sb-panel-muted p-4">
                    <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">Backfill</p>
                    <p className="mt-2 text-sm text-zinc-100">{backfillSummary(selected)}</p>
                  </div>
                </div>

                {selected.last_error ? (
                  <div role="alert" className="rounded-2xl border border-amber-700/40 bg-amber-950/20 p-4 text-sm text-amber-100">
                    {selected.last_error}
                  </div>
                ) : null}

                <div>
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <p className="sb-section-title">Recent entries</p>
                    <span className="text-xs text-zinc-500">{selectedEntries.length} shown</span>
                  </div>
                  {selectedEntries.length ? (
                    <div className="space-y-2">
                      {selectedEntries.map((entry) => (
                        <div key={entry.id} className="sb-list-card p-4">
                          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                            <div className="min-w-0">
                              <p className="truncate text-sm font-medium text-zinc-100">{entry.title || entry.source_url || "Untitled upload"}</p>
                              <div className="mt-2 flex flex-wrap gap-2 text-xs text-zinc-500">
                                <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" /> {formatRelative(entry.discovered_at)}</span>
                                {entry.source_url ? (
                                  <a className="text-sky-300 hover:text-sky-100" href={entry.source_url} target="_blank" rel="noreferrer">Open item link</a>
                                ) : null}
                              </div>
                              {entry.error_message ? <p className="mt-2 text-xs text-rose-200">{entry.error_message}</p> : null}
                              {entry.skip_reason ? <p className="mt-2 text-xs text-zinc-500">{entry.skip_reason}</p> : null}
                            </div>
                            <div className="flex shrink-0 items-center gap-2">
                              {entry.status === "failed" ? (
                                <button
                                  type="button"
                                  disabled={retryingEntryId === entry.id || selected.status !== "active"}
                                  onClick={() => void retryEntry(entry)}
                                  className="sb-button-secondary"
                                >
                                  <RotateCcw className="h-4 w-4" />
                                  {retryingEntryId === entry.id ? "Retrying..." : "Retry"}
                                </button>
                              ) : null}
                              <span className={`sb-chip ${entryChip(entry.status)}`}>{entry.status}</span>
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <StatePanel
                      icon={Clock3}
                      title="No entries yet."
                      description="New entries will appear here after the source discovers uploads."
                      compact
                    />
                  )}
                </div>
              </>
            ) : null}
          </div>
        </section>
      )}
    </div>
  );
}
