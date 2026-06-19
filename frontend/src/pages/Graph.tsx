import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import { Network, RefreshCw, X } from "lucide-react";

import { api, ApiError } from "../api/client";
import type { GraphEdge, GraphNode } from "../api/types";
import PageHeader from "../components/PageHeader";
import SourceIcon from "../components/SourceIcon";
import StatePanel from "../components/StatePanel";
import { useDebounce } from "../hooks/useDebounce";

const NODE_COLORS: Record<string, string> = {
  youtube: "#ef4444",
  media: "#ef4444",
  webpage: "#3b82f6",
  pdf: "#f97316",
  doc: "#f97316",
  image: "#ec4899",
  note: "#a855f7",
  feed_article: "#f59e0b",
};

const DEFAULT_NODE_COLOR = "#6b7280";
const DIMMED_NODE_COLOR = "#172033";
const SELECTED_NODE_COLOR = "#7dd3fc";
const SOURCE_TYPE_CHIPS = ["youtube", "webpage", "doc", "image", "note", "media", "feed_article"];
const GRAPH_QUERY_LIMITS = { node_limit: 200, edge_limit: 500, include_orphans: true };

interface GraphData {
  nodes: (GraphNode & { val?: number })[];
  links: GraphLink[];
}

type GraphEndpoint = string | (GraphNode & { id: string });

type GraphLink = Omit<GraphEdge, "source" | "target"> & { source: GraphEndpoint; target: GraphEndpoint };

interface SelectedNode extends GraphNode {
  edges: Array<{ otherTitle: string; relationship: string; confidence: number }>;
}

interface CanvasDimensions {
  width: number;
  height: number;
}

function endpointId(endpoint: GraphEndpoint): string {
  return typeof endpoint === "string" ? endpoint : endpoint.id;
}

export default function Graph() {
  const [graphData, setGraphData] = useState<GraphData>({ nodes: [], links: [] });
  const [selectedNode, setSelectedNode] = useState<SelectedNode | null>(null);
  const [orphanedReadyItems, setOrphanedReadyItems] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [containerElement, setContainerElement] = useState<HTMLDivElement | null>(null);
  const [dimensions, setDimensions] = useState<CanvasDimensions | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [activeTypes, setActiveTypes] = useState<Set<string>>(new Set(SOURCE_TYPE_CHIPS));
  const [selectedTagQuery, setSelectedTagQuery] = useState<string>("");
  const debouncedSearch = useDebounce(searchQuery, 300);

  const loadGraph = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const data = await api.getGraph(GRAPH_QUERY_LIMITS);
      setGraphData({
        nodes: data.nodes.map((node) => ({ ...node, val: 1 })),
        links: data.edges.map((edge) => ({
          source: edge.source,
          target: edge.target,
          relationship: edge.relationship,
          confidence: edge.confidence,
        })),
      });
      setOrphanedReadyItems(data.meta?.orphaned_ready_items ?? 0);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setGraphData({ nodes: [], links: [] });
      setOrphanedReadyItems(0);
    } finally {
      setLoading(false);
    }
  }, []);

  useLayoutEffect(() => {
    if (!containerElement) {
      setDimensions(null);
      return;
    }

    let animationFrame = 0;
    const measureCanvas = () => {
      const { width, height } = containerElement.getBoundingClientRect();
      const nextDimensions = {
        width: Math.floor(width),
        height: Math.floor(height),
      };

      if (nextDimensions.width < 1 || nextDimensions.height < 1) return;

      setDimensions((previous) =>
        previous?.width === nextDimensions.width && previous.height === nextDimensions.height ? previous : nextDimensions,
      );
    };

    const scheduleMeasure = () => {
      cancelAnimationFrame(animationFrame);
      animationFrame = requestAnimationFrame(measureCanvas);
    };

    measureCanvas();
    const observer = new ResizeObserver(scheduleMeasure);
    observer.observe(containerElement);
    window.addEventListener("resize", scheduleMeasure);
    window.visualViewport?.addEventListener("resize", scheduleMeasure);

    return () => {
      cancelAnimationFrame(animationFrame);
      observer.disconnect();
      window.removeEventListener("resize", scheduleMeasure);
      window.visualViewport?.removeEventListener("resize", scheduleMeasure);
    };
  }, [containerElement]);

  useEffect(() => {
    void loadGraph();
  }, [loadGraph]);

  const allTags = useMemo(
    () => [...new Set(graphData.nodes.flatMap((node) => (node as GraphNode & { tags?: string[] }).tags ?? []))].sort(),
    [graphData],
  );

  const matchingTagOptions = useMemo(() => {
    const query = selectedTagQuery.trim().toLowerCase();
    if (!query) {
      return allTags.slice(0, 100);
    }
    return allTags.filter((tag) => tag.toLowerCase().includes(query)).slice(0, 50);
  }, [allTags, selectedTagQuery]);

  const visibleNodeIds = useMemo(() => {
    const normalizedTagQuery = selectedTagQuery.trim().toLowerCase();
    return new Set(
      graphData.nodes
        .filter((node) => activeTypes.has(node.source_type))
        .filter(
          (node) =>
            !normalizedTagQuery ||
            ((node as GraphNode & { tags?: string[] }).tags ?? []).some((tag) =>
              tag.toLowerCase().includes(normalizedTagQuery),
            ),
        )
        .map((node) => node.id),
    );
  }, [graphData, activeTypes, selectedTagQuery]);

  const filteredData = useMemo(
    () => ({
      nodes: graphData.nodes.filter((node) => visibleNodeIds.has(node.id)),
      links: graphData.links.filter(
        (link) => visibleNodeIds.has(endpointId(link.source)) && visibleNodeIds.has(endpointId(link.target)),
      ),
    }),
    [graphData, visibleNodeIds],
  );

  const handleNodeClick = useCallback(
    (node: GraphNode) => {
      const edges = graphData.links
        .filter((link) => endpointId(link.source) === node.id || endpointId(link.target) === node.id)
        .map((link) => {
          const sourceId = endpointId(link.source);
          const targetId = endpointId(link.target);
          const otherId = sourceId === node.id ? targetId : sourceId;
          const other = graphData.nodes.find((candidate) => candidate.id === otherId);
          return {
            otherTitle: other?.title ?? otherId,
            relationship: link.relationship,
            confidence: link.confidence,
          };
        });
      setSelectedNode({ ...node, edges });
    },
    [graphData],
  );

  const resetFilters = () => {
    setSearchQuery("");
    setSelectedTagQuery("");
    setActiveTypes(new Set(SOURCE_TYPE_CHIPS));
  };

  const isAnyFilterActive = Boolean(
    searchQuery || selectedTagQuery || activeTypes.size < SOURCE_TYPE_CHIPS.length,
  );

  const isEmpty = graphData.nodes.length === 0;
  const noVisibleGraph = !isEmpty && filteredData.nodes.length === 0;
  const selectedNodeId = selectedNode?.id ?? null;
  const canvasDimensions = dimensions && dimensions.width > 0 && dimensions.height > 0 ? dimensions : null;

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Relationships"
        title="Knowledge graph"
        description="Inspect how captured items connect, then narrow the view by source type, tags, or a title search without losing graph context."
        meta={
          <>
            <span className={`sb-chip ${loading ? "sb-chip-active" : "sb-chip-inactive"}`}>
              {loading ? "Loading live graph" : `${filteredData.nodes.length} nodes`}
            </span>
            <span className="sb-chip sb-chip-inactive">
              {error ? "Refresh failed" : `${filteredData.links.length} links`}
            </span>
            <span className="sb-chip sb-chip-inactive">
              {orphanedReadyItems === 1 ? "1 unlinked memory" : `${orphanedReadyItems} unlinked memories`}
            </span>
          </>
        }
        actions={
          <button type="button" onClick={() => void loadGraph()} className="sb-button-secondary" disabled={loading}>
            <RefreshCw className="h-4 w-4" />
            {loading ? "Refreshing…" : "Reload graph"}
          </button>
        }
      />

      {loading ? (
        <section className="sb-panel sb-panel-padding">
          <StatePanel
            icon={RefreshCw}
            title="Loading the live graph."
            description="The Palace shell is ready. Relationship data is still loading from the backend so you can stay oriented while the graph snapshot arrives."
          />
        </section>
      ) : error ? (
        <section className="sb-panel sb-panel-padding">
          <StatePanel
            icon={RefreshCw}
            variant="error"
            title="The graph is not available right now."
            description={error}
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <button type="button" onClick={() => void loadGraph()} className="sb-button-primary">
                  Try again
                </button>
                <a href="/browse" className="sb-button-secondary">
                  Open Library
                </a>
              </div>
            }
          />
        </section>
      ) : isEmpty ? (
        <section className="sb-panel sb-panel-padding">
          <StatePanel
            icon={Network}
            variant="empty"
            title="No relationships are mapped yet."
            description="The graph becomes useful after the library has enough linked material. Capture more connected items first, then come back here to inspect how concepts relate."
            action={
              <a href="/browse" className="sb-button-primary">
                Open Library
              </a>
            }
          />
        </section>
      ) : (
        <>
          <section className="sb-panel sb-panel-padding space-y-4">
            <div className="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
              <div>
                <p className="sb-section-title">Filters</p>
                <p className="mt-2 text-sm text-zinc-400">
                  Source filters affect visibility. Text search dims non-matching nodes instead of deleting them.
                </p>
              </div>
              {isAnyFilterActive ? (
                <button onClick={resetFilters} className="sb-button-secondary">
                  Reset filters
                </button>
              ) : null}
            </div>

            <div className="grid gap-3 lg:grid-cols-[minmax(0,0.9fr),minmax(0,1.1fr)]">
              <input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search node titles…"
                className="sb-input"
              />
              {allTags.length > 0 ? (
                <div className="space-y-2">
                  <input
                    list="graph-tag-options"
                    value={selectedTagQuery}
                    onChange={(e) => setSelectedTagQuery(e.target.value)}
                    placeholder="Filter by tag…"
                    className="sb-input"
                  />
                  <datalist id="graph-tag-options">
                    {matchingTagOptions.map((tag) => (
                      <option key={tag} value={tag} />
                    ))}
                  </datalist>
                </div>
              ) : (
                <div className="sb-panel-muted flex items-center px-4 py-3 text-sm text-zinc-500">
                  No tags available yet for graph filtering.
                </div>
              )}
            </div>

            <div className="space-y-3">
              <div className="sb-chip-group">
                {SOURCE_TYPE_CHIPS.map((type) => (
                  <button
                    key={type}
                    onClick={() =>
                      setActiveTypes((prev) => {
                        const next = new Set(prev);
                        next.has(type) ? next.delete(type) : next.add(type);
                        return next;
                      })
                    }
                    className={`sb-chip cursor-pointer capitalize ${activeTypes.has(type) ? "sb-chip-active" : "sb-chip-inactive"}`}
                  >
                    {type.replace("_", " ")}
                  </button>
                ))}
              </div>

              <div className="flex flex-wrap gap-3">
                {Object.entries(NODE_COLORS)
                  .filter(([type]) => type !== "media")
                  .map(([type, color]) => (
                    <div key={type} className="flex items-center gap-2 text-xs text-zinc-500">
                      <div className="h-3 w-3 rounded-full" style={{ backgroundColor: color }} />
                      <span className="capitalize">{type.replace("_", " ")}</span>
                    </div>
                  ))}
              </div>
            </div>
          </section>

          <div className="flex min-h-[min(620px,calc(100dvh-10rem))] min-w-0 flex-col gap-4 xl:h-[calc(100dvh-16rem)] xl:min-h-[480px] xl:flex-row">
            <section className="sb-panel flex min-h-[420px] min-w-0 flex-1 flex-col overflow-hidden xl:min-h-0">
              <div className="border-b border-zinc-800/80 px-5 py-4 md:px-6">
                <p className="sb-section-title">Graph canvas</p>
              </div>
              <div
                ref={setContainerElement}
                className="min-h-[360px] min-w-0 flex-1 overflow-hidden bg-[#030712] xl:min-h-0"
                data-testid="graph-canvas-viewport"
              >
                {noVisibleGraph ? (
                  <div className="flex h-full items-center justify-center p-4">
                    <StatePanel
                      icon={Network}
                      compact
                      variant="empty"
                      title="No nodes match the current filters."
                      description="The graph still has data, but the active source-type, tag, or search filters hid all of it."
                      action={
                        <button type="button" onClick={resetFilters} className="sb-button-primary">
                          Reset filters
                        </button>
                      }
                    />
                  </div>
                ) : !canvasDimensions ? (
                  <div className="flex h-full items-center justify-center p-4 text-sm text-zinc-500" aria-live="polite">
                    Sizing graph canvas.
                  </div>
                ) : (
                  <ForceGraph3D
                    width={canvasDimensions.width}
                    height={canvasDimensions.height}
                    graphData={filteredData}
                    nodeColor={(node) => {
                      const graphNode = node as GraphNode;
                      if (selectedNodeId === graphNode.id) return SELECTED_NODE_COLOR;
                      if (debouncedSearch && !graphNode.title.toLowerCase().includes(debouncedSearch.toLowerCase())) {
                        return DIMMED_NODE_COLOR;
                      }
                      return NODE_COLORS[graphNode.source_type] ?? DEFAULT_NODE_COLOR;
                    }}
                    nodeLabel={(node) => (node as GraphNode).title}
                    nodeVal={(node) => (selectedNodeId === (node as GraphNode).id ? 2.4 : 1)}
                    nodeOpacity={0.82}
                    nodeResolution={4}
                    linkWidth={(link) => Math.max(0.35, ((link as { confidence: number }).confidence ?? 0.5) * 1.15)}
                    linkColor={() => "#475569"}
                    linkOpacity={0.22}
                    onNodeClick={(node) => handleNodeClick(node as GraphNode)}
                    warmupTicks={35}
                    cooldownTicks={70}
                    d3VelocityDecay={0.45}
                    enablePointerInteraction={filteredData.nodes.length <= 600}
                    backgroundColor="#030712"
                    showNavInfo={false}
                  />
                )}
              </div>
            </section>

            {selectedNode ? (
              <aside className="sb-panel sb-panel-padding w-full xl:max-h-full xl:w-80 xl:shrink-0 xl:overflow-y-auto">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="sb-section-title">Selected node</p>
                    <div className="mt-3 flex items-start gap-2">
                      <SourceIcon sourceType={selectedNode.source_type} className="mt-0.5 h-4 w-4 shrink-0" />
                      <h2 className="text-lg font-semibold text-zinc-100">{selectedNode.title}</h2>
                    </div>
                  </div>
                  <button onClick={() => setSelectedNode(null)} className="sb-button-ghost p-2" aria-label="Close panel">
                    <X className="h-4 w-4" />
                  </button>
                </div>

                <div className="mt-4">
                  <span className="sb-chip sb-chip-inactive capitalize">{selectedNode.source_type}</span>
                </div>

                {selectedNode.edges.length > 0 ? (
                  <div className="mt-5 space-y-2">
                    <p className="sb-section-title">Connections ({selectedNode.edges.length})</p>
                    {selectedNode.edges.map((edge, index) => (
                      <div key={index} className="sb-panel-muted p-3">
                        <p className="truncate text-sm font-medium text-zinc-200">{edge.otherTitle}</p>
                        <div className="mt-2 flex items-center justify-between text-xs text-zinc-500">
                          <span>{edge.relationship.replace(/_/g, " ")}</span>
                          <span>{(edge.confidence * 100).toFixed(0)}%</span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : null}

                <a href={`/items/${selectedNode.id}`} className="sb-button-primary mt-5 w-full">
                  View full item
                </a>
              </aside>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
