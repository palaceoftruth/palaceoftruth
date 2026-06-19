import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ChevronLeft, ChevronRight, Download, Library } from "lucide-react";

import { api, ApiError } from "../api/client";
import type { Item } from "../api/types";
import ItemCard from "../components/ItemCard";
import PageHeader from "../components/PageHeader";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";

const SOURCE_TABS = [
  { value: "", label: "All" },
  { value: "media", label: "Media" },
  { value: "webpage", label: "Webpage" },
  { value: "doc", label: "Doc" },
  { value: "image", label: "Image" },
  { value: "note", label: "Note" },
  { value: "feed_article", label: "Feed" },
];

const SORT_OPTIONS = [
  { value: "created_at|desc", label: "Newest" },
  { value: "created_at|asc", label: "Oldest" },
  { value: "title|asc", label: "Title A–Z" },
];

const PER_PAGE = 20;

export default function Browse() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();

  const sourceType = searchParams.get("source_type") ?? "";
  const tag = searchParams.get("tag") ?? "";
  const page = parseInt(searchParams.get("page") ?? "1", 10);
  const sortParam = searchParams.get("sort") ?? "created_at|desc";

  const [items, setItems] = useState<Item[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const exportMenuRef = useRef<HTMLDivElement>(null);

  const [sort, order] = sortParam.split("|");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: Record<string, string | number> = {
        page,
        per_page: PER_PAGE,
        sort,
        order,
      };
      if (sourceType) params.source_type = sourceType;
      if (tag) params.tags = tag;
      const response = await api.listItems(params);
      setItems(response.items);
      setTotal(response.total);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, sort, order, sourceType, tag]);

  const handleExport = useCallback(
    async (format: "json" | "markdown") => {
      setShowExportMenu(false);
      setExporting(true);
      try {
        await api.exportLibrary({
          format,
          ...(sourceType ? { source_type: sourceType } : {}),
          ...(tag ? { tags: tag } : {}),
        });
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : String(err));
      } finally {
        setExporting(false);
      }
    },
    [sourceType, tag, toast],
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!showExportMenu) return;
    const handleClick = (event: MouseEvent) => {
      if (!exportMenuRef.current?.contains(event.target as Node)) {
        setShowExportMenu(false);
      }
    };
    window.addEventListener("mousedown", handleClick);
    return () => window.removeEventListener("mousedown", handleClick);
  }, [showExportMenu]);

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    next.set("page", "1");
    setSearchParams(next);
  };

  const setPage = (nextPage: number) => {
    const next = new URLSearchParams(searchParams);
    next.set("page", String(nextPage));
    setSearchParams(next);
  };

  const totalPages = Math.ceil(total / PER_PAGE);

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Library"
        title="Browse captured memory"
        description="Filter the shared corpus by source type, tags, and recency without losing the overview of what is already in the system."
        meta={<span className="sb-chip sb-chip-inactive">{total} items in library</span>}
      />

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div>
            <p className="sb-section-title">Filters</p>
            <p className="mt-2 text-sm text-zinc-400">
              Narrow the corpus without breaking the reading flow.
            </p>
          </div>
          <div className="relative" ref={exportMenuRef}>
            <button
              onClick={() => setShowExportMenu((value) => !value)}
              disabled={exporting}
              className="sb-button-secondary"
            >
              <Download className="h-4 w-4" />
              {exporting ? "Exporting…" : "Export view"}
            </button>
            {showExportMenu ? (
              <div className="absolute right-0 z-10 mt-2 w-40 overflow-hidden rounded-2xl border border-zinc-700 bg-zinc-950 shadow-xl">
                <button
                  onClick={() => handleExport("json")}
                  className="w-full px-4 py-3 text-left text-sm text-zinc-300 transition hover:bg-zinc-900 hover:text-white"
                >
                  JSON
                </button>
                <button
                  onClick={() => handleExport("markdown")}
                  className="w-full px-4 py-3 text-left text-sm text-zinc-300 transition hover:bg-zinc-900 hover:text-white"
                >
                  Markdown
                </button>
              </div>
            ) : null}
          </div>
        </div>

        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="sb-chip-group">
            {SOURCE_TABS.map((tab) => (
              <button
                key={tab.value}
                onClick={() => setParam("source_type", tab.value)}
                className={`sb-chip cursor-pointer ${sourceType === tab.value ? "sb-chip-active" : "sb-chip-inactive"}`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {tag ? (
              <span className="sb-chip sb-chip-active">
                #{tag}
                <button onClick={() => setParam("tag", "")} className="ml-1 text-sky-100 transition hover:text-white">
                  ×
                </button>
              </span>
            ) : null}
            <select
              value={sortParam}
              onChange={(e) => {
                const next = new URLSearchParams(searchParams);
                next.set("sort", e.target.value);
                next.set("page", "1");
                setSearchParams(next);
              }}
              className="sb-select py-2.5 text-sm"
            >
              {SORT_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </section>

      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, index) => (
            <div key={index} className="sb-panel-muted h-20 animate-pulse" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <StatePanel
          icon={Library}
          compact
          variant="empty"
          title={sourceType || tag ? "Nothing matches this view." : "Your library is still empty."}
          description={
            sourceType || tag
              ? "Try a broader filter, remove the active tag, or switch back to the full library."
              : "Capture your first note, file, page, or feed and it will show up here."
          }
          action={
            sourceType || tag ? (
              <button
                type="button"
                onClick={() => {
                  const next = new URLSearchParams(searchParams);
                  next.delete("source_type");
                  next.delete("tag");
                  next.set("page", "1");
                  setSearchParams(next);
                }}
                className="sb-button-secondary"
              >
                Clear filters
              </button>
            ) : (
              <a href="/ingest" className="sb-button-primary">
                Capture something
              </a>
            )
          }
        />
      ) : (
        <section className="sb-panel sb-panel-padding space-y-3">
          <div className="flex items-center justify-between">
            <p className="sb-section-title">Results</p>
            <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">
              Page {page} of {Math.max(totalPages, 1)}
            </p>
          </div>

          {items.map((item) => (
            <ItemCard
              key={item.id}
              item={item}
              onTagClick={(nextTag) => {
                const next = new URLSearchParams(searchParams);
                next.set("tag", nextTag);
                next.set("page", "1");
                setSearchParams(next);
                navigate(`/browse?${next.toString()}`);
              }}
            />
          ))}
        </section>
      )}

      {totalPages > 1 ? (
        <div className="flex items-center justify-between pt-2">
          <button onClick={() => setPage(page - 1)} disabled={page <= 1} className="sb-button-secondary">
            <ChevronLeft className="h-4 w-4" />
            Prev
          </button>
          <span className="text-sm text-zinc-500">
            Page {page} of {totalPages}
          </span>
          <button onClick={() => setPage(page + 1)} disabled={page >= totalPages} className="sb-button-secondary">
            Next
            <ChevronRight className="h-4 w-4" />
          </button>
        </div>
      ) : null}
    </div>
  );
}
