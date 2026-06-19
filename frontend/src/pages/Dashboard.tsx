import { useEffect, useState } from "react";
import { Activity, Database, Layers, Network, RefreshCw, Rss } from "lucide-react";

import { api, ApiError } from "../api/client";
import type { Item, StatsResponse } from "../api/types";
import ItemCard from "../components/ItemCard";
import PageHeader from "../components/PageHeader";
import StatePanel from "../components/StatePanel";
import StatsCard from "../components/StatsCard";
import { useToast } from "../context/ToastContext";

export default function Dashboard() {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [recentItems, setRecentItems] = useState<Item[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const toast = useToast();
  const processingItems = Math.max((stats?.total_items ?? 0) - (stats?.ready_items ?? 0), 0);

  const handleExportAll = async (format: "json" | "markdown") => {
    setExporting(true);
    try {
      await api.exportLibrary({ format });
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setExporting(false);
    }
  };

  useEffect(() => {
    Promise.all([
      api.getStats().catch(() => null),
      api.listItems({ per_page: 10, sort: "created_at", order: "desc" }),
    ])
      .then(([statsData, itemsData]) => {
        setStats(statsData);
        setRecentItems(itemsData.items);
      })
      .catch((err) => setError(err.message));
  }, []);

  if (error) {
    return (
      <StatePanel
        icon={RefreshCw}
        variant="error"
        title="Home could not load right now."
        description={error}
        action={
          <a href="/browse" className="sb-button-secondary">
            Open Library
          </a>
        }
      />
    );
  }

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Overview"
        title="Home"
        description="See what changed, what is still processing, and what is ready to reuse across the shared memory workspace."
        actions={
          <>
            <button
              onClick={() => handleExportAll("json")}
              disabled={exporting}
              className="sb-button-secondary"
            >
              {exporting ? "Exporting…" : "Export JSON"}
            </button>
            <button
              onClick={() => handleExportAll("markdown")}
              disabled={exporting}
              className="sb-button-primary"
            >
              Export Markdown
            </button>
          </>
        }
        meta={
          <>
            <span className="sb-chip sb-chip-inactive">Shared corpus</span>
            <span className="sb-chip sb-chip-inactive">
              {stats?.active_jobs ?? 0} active jobs
            </span>
          </>
        }
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <StatsCard
          label="Library Items"
          value={stats?.total_items ?? 0}
          icon={Database}
          detail={
            stats
              ? processingItems > 0
                ? `${stats.ready_items} ready • ${processingItems} still processing`
                : `${stats.ready_items} ready for browse and search`
              : undefined
          }
        />
        <StatsCard
          label="Indexed Items"
          value={stats?.indexed_items ?? 0}
          icon={Layers}
          detail={
            stats
              ? `${stats.embedding_chunks} vector chunks stored`
              : undefined
          }
        />
        <StatsCard label="Active Jobs" value={stats?.active_jobs ?? 0} icon={Activity} />
        <StatsCard
          label="Graph Gaps"
          value={stats?.orphaned_ready_items ?? 0}
          icon={Network}
          detail={
            stats
              ? stats.orphaned_ready_items > 0
                ? "unlinked memory objects need relationship enrichment"
                : "ready items have graph connections"
              : undefined
          }
        />
        <StatsCard label="Feeds" value={stats?.feed_count ?? 0} icon={Rss} />
      </div>

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="sb-section-title">Recent captures</p>
            <p className="mt-2 text-sm text-zinc-400">
              The newest items in the library, including anything a fresh agent would need to read first.
            </p>
          </div>
          <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">
            {recentItems.length} visible
          </p>
        </div>

        {recentItems.length === 0 ? (
          <StatePanel
            icon={Database}
            compact
            variant="empty"
            title="Nothing has landed yet."
            description="Capture a note, page, file, or feed and Home will start showing what is ready, what is processing, and what changed most recently."
            action={
              <a href="/ingest" className="sb-button-primary">
                Capture your first item
              </a>
            }
          />
        ) : (
          <div className="space-y-3">
            {recentItems.map((item) => (
              <ItemCard key={item.id} item={item} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
