import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { AlertCircle, ArrowLeft, ChevronLeft, ChevronRight, Clock3, Link2, RefreshCw, Rss, Trash2 } from "lucide-react";
import { api, ApiError } from "../api/client";
import type { Feed, Item } from "../api/types";
import PageHeader from "../components/PageHeader";
import StatsCard from "../components/StatsCard";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";

const POLL_INTERVAL_OPTIONS = [
  { label: "5 min", value: 300 },
  { label: "15 min", value: 900 },
  { label: "30 min", value: 1800 },
  { label: "1 hour", value: 3600 },
  { label: "6 hours", value: 21600 },
  { label: "24 hours", value: 86400 },
];

function formatRelative(dateStr: string | null): string {
  if (!dateStr) return "Never";
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function feedDisplayName(feed: Feed): string {
  if (feed.name) return feed.name;
  if (feed.feed_metadata?.feed_title) return feed.feed_metadata.feed_title;
  try {
    return new URL(feed.url).hostname;
  } catch {
    return feed.url;
  }
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function formatPollInterval(seconds: number): string {
  if (seconds < 3600) {
    const minutes = Math.round(seconds / 60);
    return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }

  if (seconds < 86400) {
    const hours = Math.round(seconds / 3600);
    return `${hours} hour${hours === 1 ? "" : "s"}`;
  }

  const days = Math.round(seconds / 86400);
  return `${days} day${days === 1 ? "" : "s"}`;
}

function formatTimestamp(dateStr: string | null): string {
  if (!dateStr) return "Never";

  return new Date(dateStr).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function feedStatus(feed: Feed): { label: string; className: string } {
  if (!feed.enabled) {
    return {
      label: feed.paused_reason ?? "Paused",
      className: "border-zinc-700 bg-zinc-950/80 text-zinc-300",
    };
  }

  if (feed.consecutive_failures > 0 || feed.last_error) {
    return {
      label: `${feed.consecutive_failures || 1} failure${feed.consecutive_failures === 1 ? "" : "s"}`,
      className: "border-amber-700/40 bg-amber-950/30 text-amber-200",
    };
  }

  return {
    label: "Running",
    className: "border-emerald-700/40 bg-emerald-950/30 text-emerald-200",
  };
}

function StatusChip({ feed }: { feed: Feed }) {
  const status = feedStatus(feed);

  return <span className={`sb-chip ${status.className}`}>{status.label}</span>;
}

interface AddFeedFormProps {
  onCancel: () => void;
  onSuccess: (feed: Feed) => void;
}

function AddFeedForm({ onCancel, onSuccess }: AddFeedFormProps) {
  const toast = useToast();
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [tags, setTags] = useState("");
  const [pollInterval, setPollInterval] = useState(3600);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    try {
      const autoTags = tags
        ? tags.split(",").map((t) => t.trim()).filter(Boolean)
        : [];
      const feed = await api.createFeed({
        url: url.trim(),
        name: name.trim() || undefined,
        auto_tags: autoTags.length > 0 ? autoTags : undefined,
        poll_interval: pollInterval,
      });
      toast.success("Feed added — polling started");
      onSuccess(feed);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="sb-panel sb-panel-padding space-y-4"
    >
      <div>
        <p className="sb-section-title">New feed</p>
        <p className="mt-2 text-sm text-zinc-400">
          Add a durable RSS or Atom source so Palace of Truth can keep ingesting fresh articles without manual capture.
        </p>
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <label className="mb-1 block text-xs text-zinc-400">
            Feed URL <span className="text-red-400">*</span>
          </label>
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://example.com/feed.xml"
            className="sb-input"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-zinc-400">
            Name (optional)
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My Feed"
            className="sb-input"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-zinc-400">
            Tags (comma-separated, optional)
          </label>
          <input
            type="text"
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            placeholder="tech, news"
            className="sb-input"
          />
        </div>
        <div>
          <label className="mb-1 block text-xs text-zinc-400">
            Poll interval
          </label>
          <select
            value={pollInterval}
            onChange={(e) => setPollInterval(Number(e.target.value))}
            className="sb-select"
          >
            {POLL_INTERVAL_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="sb-button-secondary"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting || !url.trim()}
          className="sb-button-primary"
        >
          {submitting ? "Adding…" : "Add Feed"}
        </button>
      </div>
    </form>
  );
}

interface FeedDetailProps {
  feed: Feed;
  onBack: () => void;
  onPoll: () => Promise<void>;
}

function FeedDetail({ feed, onBack, onPoll }: FeedDetailProps) {
  const navigate = useNavigate();
  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const PER_PAGE = 10;

  const loadItems = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await api.getFeedItems(feed.id, page, PER_PAGE);
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [feed.id, page]);

  useEffect(() => {
    void loadItems();
  }, [loadItems]);

  const totalPages = Math.ceil(total / PER_PAGE);
  const displayName = feedDisplayName(feed);
  const status = feedStatus(feed);
  const siteUrl = feed.feed_metadata?.site_url ?? null;

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Capture"
        title={displayName}
        description={
          feed.feed_metadata?.description
            ? feed.feed_metadata.description
            : "Inspect polling state, source metadata, and the articles this feed has already landed into the shared library."
        }
        actions={
          <>
            <button type="button" onClick={onBack} className="sb-button-secondary">
              <ArrowLeft className="h-4 w-4" />
              Back to feeds
            </button>
            <button type="button" onClick={() => void onPoll()} className="sb-button-primary">
              <RefreshCw className="h-4 w-4" />
              Poll this feed now
            </button>
          </>
        }
        meta={
          <>
            <span className={`sb-chip ${status.className}`}>{status.label}</span>
            <span className="sb-chip sb-chip-inactive">{feed.item_count} article{feed.item_count === 1 ? "" : "s"}</span>
            <span className="sb-chip sb-chip-inactive">Every {formatPollInterval(feed.poll_interval)}</span>
          </>
        }
      />

      {feed.last_error ? (
        <StatePanel
          icon={RefreshCw}
          compact
          variant="error"
          title="This feed needs operator attention."
          description={`The most recent poll failed: ${feed.last_error}`}
          action={
            <button type="button" onClick={() => void onPoll()} className="sb-button-secondary">
              Retry poll
            </button>
          }
        />
      ) : null}

      <section className="sb-panel sb-panel-padding space-y-5">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <p className="sb-section-title">Feed status</p>
            <p className="mt-2 text-sm text-zinc-400">
              Keep the polling cadence, source link, and article volume visible before you act on this feed.
            </p>
          </div>
          <a href={feed.url} target="_blank" rel="noopener noreferrer" className="sb-button-secondary">
            <Link2 className="h-4 w-4" />
            Open feed source
          </a>
        </div>

        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <StatsCard label="Articles" value={feed.item_count} icon={Rss} detail="Captured from this feed" />
          <StatsCard
            label="Polling cadence"
            value={formatPollInterval(feed.poll_interval)}
            icon={Clock3}
            detail="Scheduled interval"
          />
          <StatsCard label="Last fetched" value={formatRelative(feed.last_fetched_at)} icon={RefreshCw} />
          <StatsCard
            label="Failures"
            value={feed.consecutive_failures}
            icon={AlertCircle}
            detail={feed.enabled ? "Current streak" : "Polling paused"}
          />
        </div>

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.1fr),minmax(0,0.9fr)]">
          <div className="sb-panel-muted p-4 md:p-5">
            <p className="sb-section-title">Source details</p>
            <dl className="mt-4 space-y-3 text-sm">
              <div className="flex items-start justify-between gap-4">
                <dt className="text-zinc-500">Feed URL</dt>
                <dd className="max-w-[70%] truncate text-right text-zinc-200">{feed.url}</dd>
              </div>
              {siteUrl ? (
                <div className="flex items-start justify-between gap-4">
                  <dt className="text-zinc-500">Site URL</dt>
                  <dd className="max-w-[70%] truncate text-right text-zinc-200">{siteUrl}</dd>
                </div>
              ) : null}
              <div className="flex items-start justify-between gap-4">
                <dt className="text-zinc-500">Created</dt>
                <dd className="text-right text-zinc-200">{formatTimestamp(feed.created_at)}</dd>
              </div>
              <div className="flex items-start justify-between gap-4">
                <dt className="text-zinc-500">Last fetched</dt>
                <dd className="text-right text-zinc-200">{formatTimestamp(feed.last_fetched_at)}</dd>
              </div>
              <div className="flex items-start justify-between gap-4">
                <dt className="text-zinc-500">Status</dt>
                <dd className="text-right text-zinc-200">{status.label}</dd>
              </div>
            </dl>
          </div>

          <div className="sb-panel-muted p-4 md:p-5">
            <p className="sb-section-title">Auto-tags</p>
            <p className="mt-2 text-sm text-zinc-400">
              Tags are applied to new articles from this feed during ingestion.
            </p>
            {feed.auto_tags.length > 0 ? (
              <div className="mt-4 sb-chip-group">
                {feed.auto_tags.map((tag) => (
                  <span key={tag} className="sb-chip sb-chip-active">
                    #{tag}
                  </span>
                ))}
              </div>
            ) : (
              <div className="mt-4 rounded-2xl border border-zinc-800/70 bg-zinc-950/70 px-4 py-3 text-sm text-zinc-500">
                No auto-tags configured for this feed yet.
              </div>
            )}
          </div>
        </div>
      </section>

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="sb-section-title">Articles</p>
            <p className="mt-2 text-sm text-zinc-400">
              Open the captured items that this feed has already contributed to the library.
            </p>
          </div>
          <span className="sb-chip sb-chip-inactive">{total} total</span>
        </div>
        {loading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="sb-panel-muted h-14 animate-pulse" />
            ))}
          </div>
        ) : loadError ? (
          <StatePanel
            icon={RefreshCw}
            compact
            variant="error"
            title="Feed articles are unavailable right now."
            description={loadError}
            action={
              <button type="button" onClick={() => void loadItems()} className="sb-button-secondary">
                Try again
              </button>
            }
          />
        ) : items.length === 0 ? (
          <StatePanel
            icon={Rss}
            compact
            variant="empty"
            title="No articles have landed from this feed yet."
            description="The feed is connected, but nothing has been ingested into the library yet. Run a poll if you want to force the first pass now."
            action={
              <button type="button" onClick={() => void onPoll()} className="sb-button-primary">
                Poll this feed now
              </button>
            }
          />
        ) : (
          <div className="space-y-2">
            {items.map((item) => (
              <button
                key={item.id}
                onClick={() => navigate(`/items/${item.id}`)}
                className="sb-list-card group flex w-full items-center justify-between gap-4 px-4 py-3 text-left"
              >
                <span className="truncate text-sm text-zinc-200 group-hover:text-white">
                  {item.title}
                </span>
                <span className="shrink-0 text-xs text-zinc-500">
                  {formatRelative(item.created_at)}
                </span>
              </button>
            ))}
          </div>
        )}

        {totalPages > 1 && (
          <div className="flex items-center justify-between pt-3">
            <button
              onClick={() => setPage((p) => p - 1)}
              disabled={page <= 1}
              className="sb-button-secondary"
            >
              <ChevronLeft className="h-4 w-4" /> Prev
            </button>
            <span className="text-sm text-zinc-500">
              Page {page} of {totalPages}
            </span>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={page >= totalPages}
              className="sb-button-secondary"
            >
              Next <ChevronRight className="h-4 w-4" />
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

export default function Feeds() {
  const toast = useToast();
  const opmlInputRef = useRef<HTMLInputElement>(null);

  const [feeds, setFeeds] = useState<Feed[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [selectedFeed, setSelectedFeed] = useState<Feed | null>(null);
  const [pollingId, setPollingId] = useState<string | null>(null);
  const activeFeedCount = feeds.filter((feed) => feed.enabled).length;
  const issueFeedCount = feeds.filter((feed) => feed.consecutive_failures > 0 || feed.last_error).length;
  const totalArticles = feeds.reduce((sum, feed) => sum + feed.item_count, 0);

  const loadFeeds = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await api.listFeeds();
      setFeeds(res.feeds);
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadFeeds();
  }, [loadFeeds]);

  const pollFeed = async (feed: Feed) => {
    setPollingId(feed.id);
    try {
      await api.pollFeed(feed.id);
      toast.success(`Polling "${feedDisplayName(feed)}"…`);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setPollingId(null);
    }
  };

  const handleAddSuccess = (feed: Feed) => {
    setFeeds((prev) => [feed, ...prev]);
    setShowAddForm(false);
  };

  const handleDelete = async (e: React.MouseEvent, feed: Feed) => {
    e.stopPropagation();
    if (!confirm(`Remove feed "${feedDisplayName(feed)}" from active polling? Operators can restore it later.`)) return;
    try {
      await api.deleteFeed(feed.id);
      setFeeds((prev) => prev.filter((f) => f.id !== feed.id));
      toast.success("Feed removed from active polling");
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  const handlePoll = async (e: React.MouseEvent, feed: Feed) => {
    e.stopPropagation();
    await pollFeed(feed);
  };

  const handleToggleEnabled = async (e: React.MouseEvent, feed: Feed) => {
    e.stopPropagation();
    try {
      const updated = feed.enabled
        ? await api.disableFeed(feed.id)
        : await api.enableFeed(feed.id);
      setFeeds((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
      toast.success(updated.enabled ? "Feed enabled" : "Feed disabled");
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  const handleOPMLChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    try {
      const result = await api.importOPML(file);
      const importSummary =
        result.created > 0
          ? `Imported ${result.created} feed${result.created === 1 ? "" : "s"}`
          : "No new feeds imported";
      const skippedSummary =
        result.skipped > 0
          ? ` • ${result.skipped} already existed`
          : "";
      toast.success(`${importSummary}${skippedSummary}`);
      void loadFeeds();
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  if (selectedFeed) {
    return (
      <FeedDetail
        feed={selectedFeed}
        onBack={() => setSelectedFeed(null)}
        onPoll={() => pollFeed(selectedFeed)}
      />
    );
  }

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Capture"
        title="Feeds"
        description="Keep the library warm from recurring sources instead of manually copying articles into separate agent workspaces."
        meta={
          <>
            <span className="sb-chip sb-chip-inactive">{feeds.length} subscribed feed{feeds.length === 1 ? "" : "s"}</span>
            <span className="sb-chip sb-chip-inactive">{activeFeedCount} active</span>
          </>
        }
        actions={
          <div className="flex items-center gap-2">
          <input
            ref={opmlInputRef}
            type="file"
            accept=".opml,.xml"
            className="hidden"
            onChange={handleOPMLChange}
          />
          <button
            onClick={() => opmlInputRef.current?.click()}
            className="sb-button-secondary"
          >
            Import OPML
          </button>
          <button
            onClick={() => setShowAddForm((v) => !v)}
            className="sb-button-primary"
          >
            {showAddForm ? "Cancel" : "Add Feed"}
          </button>
          </div>
        }
      />

      {/* Add feed inline form */}
      {showAddForm && (
        <AddFeedForm
          onCancel={() => setShowAddForm(false)}
          onSuccess={handleAddSuccess}
        />
      )}

      {feeds.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          <StatsCard label="Subscribed feeds" value={feeds.length} icon={Rss} detail="Connected recurring sources" />
          <StatsCard label="Active polling" value={activeFeedCount} icon={RefreshCw} detail="Feeds currently running" />
          <StatsCard
            label="Captured articles"
            value={totalArticles}
            icon={Clock3}
            detail={
              issueFeedCount > 0
                ? `${issueFeedCount} feed${issueFeedCount === 1 ? "" : "s"} ${issueFeedCount === 1 ? "needs" : "need"} attention`
                : "No active feed failures"
            }
          />
        </div>
      ) : null}

      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="sb-panel-muted h-16 animate-pulse" />
          ))}
        </div>
      ) : loadError && feeds.length === 0 ? (
        <StatePanel
          icon={RefreshCw}
          variant="error"
          title="Feeds are unavailable right now."
          description={loadError}
          action={
            <button type="button" onClick={() => void loadFeeds()} className="sb-button-secondary">
              Try again
            </button>
          }
        />
      ) : feeds.length === 0 ? (
        <StatePanel
          icon={Rss}
          variant="empty"
          title="No feeds are connected yet."
          description="Bring in one RSS or Atom feed and Palace of Truth will keep the library warm without manual copying."
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <button type="button" onClick={() => opmlInputRef.current?.click()} className="sb-button-secondary">
                Import OPML
              </button>
              <button type="button" onClick={() => setShowAddForm(true)} className="sb-button-primary">
                Add first feed
              </button>
            </div>
          }
        />
      ) : (
        <div className="space-y-3">
          {loadError ? (
            <StatePanel
              icon={RefreshCw}
              compact
              variant="error"
              title="Feed refresh failed."
              description={loadError}
              action={
                <button type="button" onClick={() => void loadFeeds()} className="sb-button-secondary">
                  Reload feeds
                </button>
              }
            />
          ) : null}
          <section className="sb-panel sb-panel-padding space-y-4">
            <div className="flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
              <div>
                <p className="sb-section-title">Feed roster</p>
                <p className="mt-2 text-sm text-zinc-400">
                  Review recurring sources, force a poll, and open a feed detail view without losing the current library posture.
                </p>
              </div>
              <span className="sb-chip sb-chip-inactive">{feeds.length} connected</span>
            </div>

            <div className="space-y-3">
            {feeds.map((feed) => (
              <div
                key={feed.id}
                className="sb-list-card px-4 py-4 md:px-5"
              >
                <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="truncate text-sm font-medium text-zinc-100">{feedDisplayName(feed)}</p>
                      <StatusChip feed={feed} />
                      <span className="sb-chip sb-chip-inactive">{feed.item_count} article{feed.item_count === 1 ? "" : "s"}</span>
                    </div>
                    <p className="mt-2 truncate text-xs text-zinc-500">{feed.url}</p>
                    <div className="mt-3 flex flex-wrap items-center gap-2">
                      <span className="sb-chip sb-chip-inactive">Every {formatPollInterval(feed.poll_interval)}</span>
                      <span className="sb-chip sb-chip-inactive">Last fetched {formatRelative(feed.last_fetched_at)}</span>
                      {feed.auto_tags.slice(0, 3).map((tag) => (
                        <span key={tag} className="sb-chip sb-chip-active">
                          #{tag}
                        </span>
                      ))}
                    </div>
                    {feed.last_error ? (
                      <p className="mt-3 text-sm text-amber-200">Latest poll issue: {feed.last_error}</p>
                    ) : null}
                  </div>

                  <div className="flex flex-wrap items-center gap-2 xl:justify-end">
                    <button type="button" onClick={() => setSelectedFeed(feed)} className="sb-button-secondary">
                      Open details
                    </button>
                    <button
                      type="button"
                      onClick={(e) => handlePoll(e, feed)}
                      disabled={pollingId === feed.id}
                      className="sb-button-secondary"
                    >
                      <RefreshCw className={`h-4 w-4 ${pollingId === feed.id ? "animate-spin" : ""}`} />
                      {pollingId === feed.id ? "Polling…" : "Poll now"}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => handleToggleEnabled(e, feed)}
                      className="sb-button-ghost"
                    >
                      {feed.enabled ? "Pause" : "Resume"}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => handleDelete(e, feed)}
                      className="sb-button-ghost text-rose-200 hover:bg-rose-950/30 hover:text-rose-100"
                    >
                      <Trash2 className="h-4 w-4" />
                      Remove
                    </button>
                  </div>
                </div>
              </div>
            ))}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
