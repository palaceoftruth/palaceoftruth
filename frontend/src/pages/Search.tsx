import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Library, Search as SearchIcon } from "lucide-react";

import { api, ApiError } from "../api/client";
import type { SearchResult } from "../api/types";
import ArtifactCitation from "../components/ArtifactCitation";
import PageHeader from "../components/PageHeader";
import ProvenanceDrawer from "../components/ProvenanceDrawer";
import SourceIcon from "../components/SourceIcon";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";
import { useDebounce } from "../hooks/useDebounce";

const SOURCE_TYPES = [
  { value: "", label: "All types" },
  { value: "media", label: "Media / Audio" },
  { value: "webpage", label: "Webpage" },
  { value: "doc", label: "Document" },
  { value: "image", label: "Image" },
  { value: "note", label: "Note" },
];

export default function Search() {
  const [query, setQuery] = useState("");
  const [sourceType, setSourceType] = useState("");
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const navigate = useNavigate();
  const toast = useToast();
  const debouncedQuery = useDebounce(query, 500);
  const latestSearchIdRef = useRef(0);

  const handleSearch = async (q = query, nextSourceType = sourceType) => {
    const trimmedQuery = q.trim();
    const requestId = latestSearchIdRef.current + 1;
    latestSearchIdRef.current = requestId;

    if (!trimmedQuery) {
      setResults(null);
      setLoading(false);
      setSearchError(null);
      return;
    }

    setLoading(true);
    setSearchError(null);
    try {
      const body: { query: string; source_type?: string; limit: number } = {
        query: trimmedQuery,
        limit: 20,
      };
      if (nextSourceType) body.source_type = nextSourceType;
      const res = await api.search(body);
      if (latestSearchIdRef.current !== requestId) return;
      setResults(res.results);
      setSearchError(null);
    } catch (err) {
      if (latestSearchIdRef.current !== requestId) return;
      const message = err instanceof ApiError ? err.message : String(err);
      setResults(null);
      setSearchError(message);
      toast.error(message);
    } finally {
      if (latestSearchIdRef.current !== requestId) return;
      setLoading(false);
    }
  };

  useEffect(() => {
    void handleSearch(debouncedQuery, sourceType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery, sourceType]);

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Retrieval"
        title="Search the memory graph"
        description="Search across notes, files, media, and pages with the same semantic retrieval path the agents use."
        meta={
          <>
            <span className={`sb-chip ${loading ? "sb-chip-active" : "sb-chip-inactive"}`}>
              {loading ? "Searching live index" : "Semantic ranking"}
            </span>
            <span className="sb-chip sb-chip-inactive">
              {sourceType ? SOURCE_TYPES.find((option) => option.value === sourceType)?.label : "All source types"}
            </span>
            <span className="sb-chip sb-chip-inactive">Tenant corpus</span>
            {results !== null ? (
              <span className="sb-chip sb-chip-inactive">{results.length} results</span>
            ) : null}
          </>
        }
        actions={
          <a href="/browse" className="sb-button-secondary">
            <Library className="h-4 w-4" />
            Open Library
          </a>
        }
      />

      <section className="sb-panel sb-panel-padding space-y-4">
        <div>
          <p className="sb-section-title">Search query</p>
          <p className="mt-2 text-sm text-zinc-400">
            Use topic language, title fragments, or recovery questions instead of isolated keywords.
          </p>
        </div>

        <div className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_16rem_auto]">
          <div className="relative min-w-0">
            <SearchIcon className="absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSearch()}
              aria-label="Search query"
              placeholder="Search your knowledge base…"
              className="sb-input pl-11"
            />
          </div>
          <select
            value={sourceType}
            onChange={(e) => setSourceType(e.target.value)}
            aria-label="Source type"
            className="sb-select"
          >
            {SOURCE_TYPES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <button
            onClick={() => handleSearch()}
            disabled={!query.trim() || loading}
            className="sb-button-primary"
          >
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
      </section>

      {searchError ? (
        <section className="sb-panel sb-panel-padding">
          <StatePanel
            icon={SearchIcon}
            variant="error"
            title="Search is not available right now."
            description={searchError}
            action={
              <button type="button" onClick={() => handleSearch()} className="sb-button-primary">
                Try search again
              </button>
            }
          />
        </section>
      ) : loading && results === null ? (
        <section className="sb-panel sb-panel-padding">
          <StatePanel
            icon={SearchIcon}
            title="Searching the live memory index."
            description="The query is running against the backend semantic index. Results will stay inside this Palace shell when the response arrives."
          />
        </section>
      ) : results !== null ? (
        <section className="sb-panel sb-panel-padding space-y-3">
          <div className="flex items-center justify-between">
            <p className="sb-section-title">Results</p>
            <p className="text-xs uppercase tracking-[0.22em] text-zinc-500">{results.length} returned</p>
          </div>

          {results.length === 0 ? (
            <StatePanel
              icon={SearchIcon}
              compact
              variant="empty"
              title={`No results for “${query}”`}
              description="Try a shorter phrase, switch the type filter, or search for the source title instead of the summary wording."
            />
          ) : (
            results.map((result) => (
              <article
                key={`${result.item_id}-${result.chunk_text.slice(0, 20)}`}
                className="sb-list-card w-full space-y-2 p-4 text-left"
              >
                <button
                  type="button"
                  onClick={() => navigate(`/items/${result.item_id}`)}
                  className="w-full cursor-pointer space-y-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/30"
                >
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex min-w-0 items-center gap-2">
                      <SourceIcon sourceType={result.source_type} />
                      <h3 className="truncate text-sm font-medium text-zinc-100">{result.title}</h3>
                    </div>
                    <span className="shrink-0 font-mono text-xs text-sky-300">
                      {(result.score * 100).toFixed(1)}%
                    </span>
                  </div>
                  {result.summary ? (
                    <p className="line-clamp-2 text-sm text-zinc-400">{result.summary}</p>
                  ) : null}
                  <blockquote className="line-clamp-3 border-l-2 border-sky-700/40 pl-3 text-xs italic text-zinc-500">
                    {result.chunk_text}
                  </blockquote>
                </button>
                <div className="flex flex-col gap-3 border-t border-zinc-800/70 pt-3 sm:flex-row sm:items-start sm:justify-between">
                  <ArtifactCitation citation={result.artifact_citation} compact />
                  <div className="shrink-0">
                    <ProvenanceDrawer
                      compact
                      triggerLabel="Evidence"
                      provenance={{
                        title: result.title,
                        subtitle: "Search ranking evidence for this returned memory item.",
                        kind: result.artifact_citation ? "derived_artifact" : "retrieval_trace",
                        itemId: result.item_id,
                        sourceType: result.source_type,
                        sourceUrl: result.source_url,
                        summary: result.summary,
                        excerpt: result.chunk_text,
                        artifact: result.artifact_citation,
                        scores: [{ label: "Search score", value: result.score, tone: result.score >= 0.7 ? "good" : "default" }],
                        metadata: [
                          { label: "Result ID", value: result.item_id },
                          { label: "Source type", value: result.source_type.replace(/_/g, " ") },
                          { label: "Tags", value: result.tags?.join(", ") },
                        ],
                      }}
                    />
                  </div>
                </div>
              </article>
            ))
          )}
        </section>
      ) : !loading ? (
        <StatePanel
          icon={SearchIcon}
          variant="neutral"
          title="Search across everything you have captured."
          description="Use a phrase, title fragment, or topic. Search works best when you describe the thing you want to recover, not just a single keyword."
        />
      ) : null}
    </div>
  );
}
