import { useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";
import {
  Archive,
  ArrowUpRight,
  CheckCircle2,
  FileText,
  Grid2X2,
  Link2,
  List,
  Loader2,
  PanelRightClose,
  Search,
  Tags,
  Video,
} from "lucide-react";

import { api, ApiError } from "../api/client";
import type { RelatedItem, WebSave, WebSaveCaptureKind } from "../api/types";
import PageHeader from "../components/PageHeader";
import SourceIcon from "../components/SourceIcon";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";

type ViewMode = "grid" | "list";
type SortValue = "saved_at|desc" | "saved_at|asc" | "title|asc";

const KIND_FILTERS: Array<{ value: WebSaveCaptureKind | ""; label: string; icon: typeof Link2 }> = [
  { value: "", label: "All", icon: Link2 },
  { value: "webpage", label: "Pages", icon: FileText },
  { value: "social_post", label: "Social", icon: Tags },
  { value: "media", label: "Media", icon: Video },
  { value: "selection_note", label: "Selections", icon: FileText },
];

const SORT_OPTIONS: Array<{ value: SortValue; label: string }> = [
  { value: "saved_at|desc", label: "Newest saved" },
  { value: "saved_at|asc", label: "Oldest saved" },
  { value: "title|asc", label: "Title A-Z" },
];

function labelForKind(kind: WebSaveCaptureKind): string {
  return kind
    .split("_")
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

function dateLabel(value: string): string {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(value));
}

function hostLabel(save: WebSave): string {
  try {
    return save.source_domain || new URL(save.normalized_url).hostname;
  } catch {
    return save.source_domain || "unknown source";
  }
}

function titleFor(save: WebSave): string {
  return save.source_title || save.item.title || hostLabel(save);
}

function uniqueTags(saves: WebSave[]): string[] {
  return Array.from(new Set(saves.flatMap((save) => save.user_tags))).sort((a, b) => a.localeCompare(b));
}

function StatusPill({ status }: { status: string }) {
  const ready = status === "ready";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium ${
      ready ? "border-emerald-600/40 bg-emerald-950/35 text-emerald-200" : "border-amber-600/40 bg-amber-950/30 text-amber-200"
    }`}>
      <CheckCircle2 className="h-3.5 w-3.5" />
      {ready ? "Processed" : status}
    </span>
  );
}

function WebSaveCard({
  save,
  view,
  selected,
  onSelect,
}: {
  save: WebSave;
  view: ViewMode;
  selected: boolean;
  onSelect: () => void;
}) {
  const title = titleFor(save);
  const content = (
    <>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="sb-chip sb-chip-inactive px-2.5 py-1">{labelForKind(save.capture_kind)}</span>
            <StatusPill status={save.item.status} />
          </div>
          <h2 className="mt-3 line-clamp-2 text-base font-semibold leading-6 text-zinc-50">{title}</h2>
        </div>
        <SourceIcon sourceType={save.item.source_type} className="mt-1 h-5 w-5 shrink-0 text-sky-200" />
      </div>
      <p className="mt-2 truncate text-sm text-zinc-500">{hostLabel(save)}</p>
      {save.item.summary ? <p className="mt-3 line-clamp-2 text-sm leading-6 text-zinc-400">{save.item.summary}</p> : null}
      <div className="mt-4 flex flex-wrap gap-1.5">
        {save.user_tags.slice(0, 4).map((tag) => (
          <span key={tag} className="rounded-full border border-zinc-800 bg-zinc-950/80 px-2.5 py-1 text-xs text-zinc-400">#{tag}</span>
        ))}
      </div>
      <p className="mt-4 text-xs uppercase tracking-[0.2em] text-zinc-600">Saved {dateLabel(save.saved_at)}</p>
    </>
  );

  return (
    <button
      type="button"
      onClick={onSelect}
      className={`sb-list-card cursor-pointer p-4 text-left transition ${
        selected ? "border-sky-500/70 bg-sky-950/25" : "hover:border-zinc-600"
      } ${view === "list" ? "w-full" : "min-h-[15rem]"}`}
    >
      {content}
    </button>
  );
}

function DetailDrawer({
  save,
  related,
  loadingRelated,
  archiving,
  onClose,
  onArchive,
}: {
  save: WebSave | null;
  related: RelatedItem[];
  loadingRelated: boolean;
  archiving: boolean;
  onClose: () => void;
  onArchive: () => void;
}) {
  if (!save) return null;
  return (
    <aside className="sb-panel sb-panel-padding h-fit space-y-5 xl:sticky xl:top-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="sb-section-title">Saved Web Detail</p>
          <h2 className="mt-3 text-xl font-semibold tracking-tight text-zinc-50">{titleFor(save)}</h2>
        </div>
        <button type="button" onClick={onClose} className="sb-button-ghost px-2.5" title="Close detail">
          <PanelRightClose className="h-4 w-4" />
        </button>
      </div>

      <dl className="space-y-3 text-sm">
        <div>
          <dt className="text-zinc-500">Original URL</dt>
          <dd className="mt-1 break-all text-zinc-200">
            <a href={save.original_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-sky-200 hover:text-sky-100">
              {save.original_url}
              <ArrowUpRight className="h-3.5 w-3.5 shrink-0" />
            </a>
          </dd>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <dt className="text-zinc-500">Kind</dt>
            <dd className="mt-1 text-zinc-200">{labelForKind(save.capture_kind)}</dd>
          </div>
          <div>
            <dt className="text-zinc-500">Saved</dt>
            <dd className="mt-1 text-zinc-200">{dateLabel(save.saved_at)}</dd>
          </div>
          <div>
            <dt className="text-zinc-500">Domain</dt>
            <dd className="mt-1 text-zinc-200">{hostLabel(save)}</dd>
          </div>
          <div>
            <dt className="text-zinc-500">Processing</dt>
            <dd className="mt-1 text-zinc-200">{save.item.status}</dd>
          </div>
        </div>
      </dl>

      <div>
        <p className="sb-section-title">Tags</p>
        <div className="mt-3 flex flex-wrap gap-2">
          {save.user_tags.length ? save.user_tags.map((tag) => (
            <span key={tag} className="sb-chip sb-chip-inactive">#{tag}</span>
          )) : <span className="text-sm text-zinc-500">No user tags saved.</span>}
        </div>
      </div>

      <div>
        <p className="sb-section-title">Related Items</p>
        <div className="mt-3 space-y-2">
          {loadingRelated ? (
            <p className="flex items-center gap-2 text-sm text-zinc-500"><Loader2 className="h-4 w-4 animate-spin" /> Loading related items</p>
          ) : related.length ? related.slice(0, 5).map((item) => (
            <a key={item.item_id} href={`/items/${item.item_id}`} className="block rounded-2xl border border-zinc-800 bg-zinc-950/60 p-3 transition hover:border-zinc-600">
              <p className="line-clamp-1 text-sm font-medium text-zinc-200">{item.title}</p>
              <p className="mt-1 text-xs text-zinc-500">{item.relationship} · score {item.confidence.toFixed(2)}</p>
            </a>
          )) : (
            <p className="text-sm leading-6 text-zinc-500">No related saved-web items are available yet.</p>
          )}
        </div>
      </div>

      <button type="button" onClick={onArchive} disabled={archiving} className="sb-button-secondary w-full border-amber-700/50 text-amber-100 hover:border-amber-500 hover:bg-amber-950/35">
        {archiving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Archive className="h-4 w-4" />}
        Archive saved page
      </button>
    </aside>
  );
}

export default function SavedWeb() {
  const toast = useToast();
  const [saves, setSaves] = useState<WebSave[]>([]);
  const [countSaves, setCountSaves] = useState<WebSave[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query);
  const [kind, setKind] = useState<WebSaveCaptureKind | "">("");
  const [tag, setTag] = useState("");
  const [sortValue, setSortValue] = useState<SortValue>("saved_at|desc");
  const [view, setView] = useState<ViewMode>("grid");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [related, setRelated] = useState<RelatedItem[]>([]);
  const [loadingRelated, setLoadingRelated] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const selected = saves.find((save) => save.id === selectedId) ?? null;
  const tags = useMemo(() => uniqueTags(countSaves), [countSaves]);

  const counts = useMemo(() => {
    const next: Record<string, number> = { "": countSaves.length, webpage: 0, social_post: 0, media: 0, selection_note: 0 };
    countSaves.forEach((save) => {
      next[save.capture_kind] += 1;
    });
    return next;
  }, [countSaves]);

  const load = useCallback(async () => {
    setLoading(true);
    const [sort, order] = sortValue.split("|") as ["saved_at" | "title", "asc" | "desc"];
    try {
      const baseParams = {
        page: 1,
        per_page: 100,
        active_only: true,
        q: deferredQuery.trim() || undefined,
        tag,
        sort,
        order,
      };
      const [response, countResponse] = await Promise.all([
        api.listWebSaves({ ...baseParams, capture_kind: kind }),
        kind ? api.listWebSaves(baseParams) : Promise.resolve(null),
      ]);
      setSaves(response.web_saves);
      setCountSaves((countResponse ?? response).web_saves);
      setTotal(response.total);
      setSelectedId((current) => (current && response.web_saves.some((save) => save.id === current) ? current : response.web_saves[0]?.id ?? null));
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [deferredQuery, kind, sortValue, tag, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setRelated([]);
      return;
    }
    setLoadingRelated(true);
    api.getRelated(selected.item_id)
      .then((response) => setRelated(response.relationships))
      .catch(() => setRelated([]))
      .finally(() => setLoadingRelated(false));
  }, [selected]);

  const archiveSelected = async () => {
    if (!selected) return;
    setArchiving(true);
    try {
      await api.archiveWebSave(selected.id, true);
      toast.success("Saved page archived.");
      await load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setArchiving(false);
    }
  };

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Browser Memory"
        title="Saved Web"
        description="Review pages explicitly saved from the browser, keep active saves visible, and archive items without hard-deleting Palace memory."
        meta={<span className="sb-chip sb-chip-active">{total} active saves</span>}
      />

      <section className="sb-panel sb-panel-padding space-y-4">
        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_auto_auto] xl:items-end">
          <label className="block">
            <span className="mb-2 block text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Search saved web</span>
            <span className="relative block">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="sb-input pl-10"
                placeholder="Search saved pages"
              />
            </span>
          </label>

          <label className="block">
            <span className="mb-2 block text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">Sort</span>
            <select value={sortValue} onChange={(event) => setSortValue(event.target.value as SortValue)} className="sb-select min-w-48">
              {SORT_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>

          <div>
            <span className="mb-2 block text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">View</span>
            <div className="inline-flex rounded-2xl border border-zinc-800 bg-zinc-950/80 p-1">
              <button type="button" onClick={() => setView("grid")} className={`rounded-xl p-2 transition ${view === "grid" ? "bg-sky-950/70 text-sky-100" : "text-zinc-500 hover:text-zinc-200"}`} title="Grid view">
                <Grid2X2 className="h-4 w-4" />
              </button>
              <button type="button" onClick={() => setView("list")} className={`rounded-xl p-2 transition ${view === "list" ? "bg-sky-950/70 text-sky-100" : "text-zinc-500 hover:text-zinc-200"}`} title="List view">
                <List className="h-4 w-4" />
              </button>
            </div>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          {KIND_FILTERS.map(({ value, label, icon: Icon }) => (
            <button key={value || "all"} type="button" onClick={() => setKind(value)} className={`sb-chip cursor-pointer ${kind === value ? "sb-chip-active" : "sb-chip-inactive"}`}>
              <Icon className="h-3.5 w-3.5" />
              {label}
              <span className="text-zinc-500">{counts[value]}</span>
            </button>
          ))}
        </div>

        {tags.length ? (
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={() => setTag("")} className={`sb-chip cursor-pointer ${tag === "" ? "sb-chip-active" : "sb-chip-inactive"}`}>All tags</button>
            {tags.slice(0, 10).map((nextTag) => (
              <button key={nextTag} type="button" onClick={() => setTag(nextTag)} className={`sb-chip cursor-pointer ${tag === nextTag ? "sb-chip-active" : "sb-chip-inactive"}`}>#{nextTag}</button>
            ))}
          </div>
        ) : null}
      </section>

      {loading ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => <div key={index} className="sb-panel-muted h-56 animate-pulse" />)}
        </div>
      ) : saves.length === 0 ? (
        <StatePanel
          icon={Link2}
          variant="empty"
          title={query || kind || tag ? "No saved pages match this view." : "No active saved pages yet."}
          description={query || kind || tag ? "Clear the search or filters to return to the active saved-web library." : "Explicit browser saves will appear here after they are captured by Palace."}
          action={query || kind || tag ? (
            <button type="button" onClick={() => { setQuery(""); setKind(""); setTag(""); }} className="sb-button-secondary">Clear filters</button>
          ) : null}
        />
      ) : (
        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem]">
          <section className={view === "grid" ? "grid items-start gap-3 md:grid-cols-2" : "space-y-3"}>
            {saves.map((save) => (
              <WebSaveCard
                key={save.id}
                save={save}
                view={view}
                selected={save.id === selectedId}
                onSelect={() => setSelectedId(save.id)}
              />
            ))}
          </section>
          <DetailDrawer
            save={selected}
            related={related}
            loadingRelated={loadingRelated}
            archiving={archiving}
            onClose={() => setSelectedId(null)}
            onArchive={archiveSelected}
          />
        </div>
      )}
    </div>
  );
}
