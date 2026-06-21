import { useEffect, useState } from "react";
import { Check, Edit2, ExternalLink, PencilLine, Tag, Trash2, X } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type { Item, RelatedItem } from "../api/types";
import ArtifactCitation, { artifactCitationFromItem } from "../components/ArtifactCitation";
import PageHeader from "../components/PageHeader";
import ProvenanceDrawer, { relatedItemsToProvenanceRelationships } from "../components/ProvenanceDrawer";
import RelationshipBadge from "../components/RelationshipBadge";
import SourceIcon from "../components/SourceIcon";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";

function metadataString(item: Item, key: string): string | null {
  const value = item.metadata_?.[key];
  return typeof value === "string" ? value : null;
}

function formatDate(value: string | null | undefined, options?: Intl.DateTimeFormatOptions): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleString(undefined, options);
}

function parseTags(tagsInput: string): string[] {
  return tagsInput
    .split(",")
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function isSystemProvenanceTag(tag: string): boolean {
  return [
    "skill-",
    "scope-",
    "workspace-",
    "session-",
    "hermes-memory-",
  ].some((prefix) => tag.startsWith(prefix));
}

interface DetailSectionProps {
  title: string;
  description?: string;
  children: React.ReactNode;
}

function DetailSection({ title, description, children }: DetailSectionProps) {
  return (
    <section className="sb-panel sb-panel-padding">
      <div className="flex flex-col gap-1 border-b border-zinc-800/80 pb-4">
        <p className="sb-section-title">{title}</p>
        {description ? <p className="text-sm leading-6 text-zinc-400">{description}</p> : null}
      </div>
      <div className="pt-4">{children}</div>
    </section>
  );
}

interface MetadataRowProps {
  label: string;
  value?: React.ReactNode;
}

function MetadataRow({ label, value }: MetadataRowProps) {
  return (
    <div className="flex flex-col gap-1 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <dt className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">{label}</dt>
      <dd className="min-w-0 text-sm leading-6 text-zinc-200 sm:max-w-[28rem] sm:text-right">{value ?? <span className="text-zinc-500">Unavailable</span>}</dd>
    </div>
  );
}

export default function ItemDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();

  const [item, setItem] = useState<Item | null>(null);
  const [related, setRelated] = useState<RelatedItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editingTags, setEditingTags] = useState(false);
  const [tagsInput, setTagsInput] = useState("");
  const [savingTags, setSavingTags] = useState(false);

  useEffect(() => {
    if (!id) {
      setLoadError("Missing item identifier.");
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setLoadError(null);

    Promise.all([api.getItem(id), api.getRelated(id)])
      .then(([itemData, relData]) => {
        if (cancelled) return;
        setItem(itemData);
        setRelated(relData.relationships);
        setTagsInput(itemData.tags.join(", "));
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(err instanceof ApiError ? err.message : String(err));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [id]);

  const handleSaveTags = async () => {
    if (!item || !id) return;
    setSavingTags(true);
    try {
      const updated = await api.updateItem(id, { tags: parseTags(tagsInput) });
      setItem(updated);
      setTagsInput(updated.tags.join(", "));
      setEditingTags(false);
      toast.success("Tags updated");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSavingTags(false);
    }
  };

  const handleCancelTags = () => {
    if (!item) return;
    setTagsInput(item.tags.join(", "));
    setEditingTags(false);
  };

  const handleDelete = async () => {
    if (!id) return;
    if (!window.confirm("Remove this item from the library? Operators can restore it later.")) return;
    try {
      await api.deleteItem(id);
      toast.success("Item removed from the library");
      navigate("/browse");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    }
  };

  if (loading) {
    return (
      <div className="sb-page">
        <div className="space-y-3">
          <div className="sb-panel-muted h-28 animate-pulse" />
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.95fr)]">
            <div className="sb-panel-muted h-72 animate-pulse" />
            <div className="sb-panel-muted h-72 animate-pulse" />
          </div>
          <div className="sb-panel-muted h-64 animate-pulse" />
        </div>
      </div>
    );
  }

  if (loadError || !item) {
    return (
      <StatePanel
        icon={X}
        variant="error"
        title="This item is unavailable."
        description={loadError ?? "Item not found"}
        action={
          <Link to="/browse" className="sb-button-secondary">
            Back to Library
          </Link>
        }
      />
    );
  }

  const createdAt = formatDate(item.created_at, { dateStyle: "medium", timeStyle: "short" });
  const feedName = metadataString(item, "feed_name");
  const feedUrl = metadataString(item, "feed_url");
  const feedAuthor = metadataString(item, "author");
  const feedPublished = formatDate(metadataString(item, "published"), { dateStyle: "medium", timeStyle: "short" });
  const parsedTags = parseTags(tagsInput);
  const systemTags = item.tags.filter(isSystemProvenanceTag);
  const semanticTags = item.tags.filter((tag) => !isSystemProvenanceTag(tag));
  const artifactCitation = artifactCitationFromItem(item);

  return (
    <div className="sb-page">
      <PageHeader
        eyebrow="Item detail"
        title={item.title}
        description="Inspect source provenance, edit tags, and review the relationships Palace derived from this captured item."
        actions={
          <>
            <ProvenanceDrawer
              provenance={{
                title: item.title,
                subtitle: "Full item provenance, captured source links, derived artifacts, and relationship evidence.",
                kind: artifactCitation ? "derived_artifact" : "raw_source",
                itemId: item.id,
                sourceType: item.source_type,
                sourceUrl: item.source_url,
                summary: item.summary,
                excerpt: item.raw_content?.slice(0, 1000),
                artifact: artifactCitation,
                relationships: relatedItemsToProvenanceRelationships(related),
                metadata: [
                  { label: "Status", value: item.status.replace(/_/g, " ") },
                  { label: "Captured", value: createdAt },
                  { label: "Categories", value: item.categories.join(", ") },
                  { label: "Tags", value: item.tags.join(", ") },
                ],
              }}
            />
            <button
              type="button"
              onClick={handleDelete}
              className="sb-button-secondary border-rose-900/70 bg-rose-950/20 text-rose-100 hover:border-rose-500/70 hover:bg-rose-950/40"
            >
              <Trash2 className="h-4 w-4" />
              Remove item
            </button>
          </>
        }
        meta={
          <>
            <span className="sb-chip sb-chip-inactive capitalize">
              <SourceIcon sourceType={item.source_type} className="h-3.5 w-3.5" />
              {item.source_type.replace(/_/g, " ")}
            </span>
            {createdAt ? <span className="sb-chip sb-chip-inactive">Captured {createdAt}</span> : null}
            <span className="sb-chip sb-chip-inactive">{item.tags.length} tag{item.tags.length === 1 ? "" : "s"}</span>
            {item.source_url ? (
              <a
                href={item.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="sb-chip sb-chip-active"
              >
                Open source
                <ExternalLink className="h-3 w-3" />
              </a>
            ) : null}
          </>
        }
      />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.95fr)]">
        <div className="min-w-0 space-y-4">
          {item.summary ? (
            <section className="sb-panel sb-panel-padding border border-sky-900/70 bg-sky-950/20">
              <p className="sb-section-title text-sky-300/80">Summary</p>
              <p className="mt-3 text-sm leading-7 text-zinc-100">{item.summary}</p>
            </section>
          ) : (
            <StatePanel
              icon={PencilLine}
              compact
              title="Summary is still unavailable."
              description="This item was captured, but no AI summary has been generated yet."
              action={null}
            />
          )}

          <DetailSection
            title="Metadata"
            description="Core provenance and ingestion state for this item."
          >
            <dl className="divide-y divide-zinc-800/70">
              <MetadataRow label="Status" value={<span className="capitalize">{item.status.replace(/_/g, " ")}</span>} />
              <MetadataRow label="Source type" value={<span className="capitalize">{item.source_type.replace(/_/g, " ")}</span>} />
              <MetadataRow
                label="Source URL"
                value={
                  item.source_url ? (
                    <a
                      href={item.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex min-w-0 max-w-full items-center gap-2 text-sky-200 transition hover:text-white"
                    >
                      <span className="truncate">{item.source_url}</span>
                      <ExternalLink className="h-4 w-4 shrink-0" />
                    </a>
                  ) : undefined
                }
              />
              <MetadataRow label="Captured" value={createdAt} />
              <MetadataRow
                label="Categories"
                value={
                  item.categories.length > 0 ? (
                    <div className="flex flex-wrap justify-end gap-2">
                      {item.categories.map((category) => (
                        <span key={category} className="sb-chip border-indigo-800/70 bg-indigo-950/30 text-indigo-200">
                          {category}
                        </span>
                      ))}
                    </div>
                  ) : undefined
                }
              />
            </dl>
          </DetailSection>

          {item.raw_content ? (
            <DetailSection
              title="Raw content"
              description="The preserved extracted text that powers search, summaries, and related-item analysis."
            >
              <details className="group overflow-hidden rounded-[24px] border border-zinc-800/80 bg-zinc-950/50">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm text-zinc-300 transition hover:text-white">
                  <span>Expand raw extracted content</span>
                  <span className="text-xs uppercase tracking-[0.22em] text-zinc-500 transition group-open:text-zinc-300">
                    Toggle
                  </span>
                </summary>
                <pre className="max-h-96 overflow-auto border-t border-zinc-800/80 px-4 py-4 text-xs leading-6 text-zinc-400 whitespace-pre-wrap">
                  {item.raw_content}
                </pre>
              </details>
            </DetailSection>
          ) : null}
        </div>

        <div className="min-w-0 space-y-4">
          {artifactCitation ? (
            <DetailSection
              title="Visual artifact"
              description="Image provenance captured with this item, including source and original artifact inspection paths when available."
            >
              <ArtifactCitation citation={artifactCitation} />
            </DetailSection>
          ) : null}

          <DetailSection
            title="Tags"
            description="Tags stay browseable and control how this item appears in filtered library views."
          >
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 text-sm text-zinc-400">
                <Tag className="h-4 w-4" />
                <span>{item.tags.length > 0 ? `${item.tags.length} assigned` : "No tags assigned"}</span>
              </div>
              {!editingTags ? (
                <button type="button" onClick={() => setEditingTags(true)} className="sb-button-ghost">
                  <Edit2 className="h-4 w-4" />
                  Edit tags
                </button>
              ) : (
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={handleSaveTags}
                    disabled={savingTags}
                    className="sb-button-primary px-3 py-2"
                  >
                    <Check className="h-4 w-4" />
                    {savingTags ? "Saving…" : "Save"}
                  </button>
                  <button type="button" onClick={handleCancelTags} disabled={savingTags} className="sb-button-ghost">
                    <X className="h-4 w-4" />
                    Cancel
                  </button>
                </div>
              )}
            </div>

            {editingTags ? (
              <div className="mt-4 space-y-3">
                <label className="block">
                  <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Comma-separated tags</span>
                  <input
                    type="text"
                    value={tagsInput}
                    onChange={(event) => setTagsInput(event.target.value)}
                    className="sb-input py-2.5"
                    placeholder="research, launch-plan, rss"
                  />
                </label>
                <p className="text-xs leading-6 text-zinc-500">
                  Clean tags become browse filters immediately. Preview:{" "}
                  {parsedTags.length > 0 ? parsedTags.join(", ") : "no tags"}
                </p>
              </div>
            ) : item.tags.length > 0 ? (
              <div className="mt-4 space-y-4">
                {semanticTags.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {semanticTags.map((tag) => (
                      <Link
                        key={tag}
                        to={`/browse?tag=${encodeURIComponent(tag)}`}
                        className="sb-chip sb-chip-inactive px-3 py-1.5"
                      >
                        {tag}
                      </Link>
                    ))}
                  </div>
                ) : null}
                {systemTags.length > 0 ? (
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3">
                    <p className="text-xs font-medium uppercase tracking-[0.22em] text-zinc-500">System provenance</p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      {systemTags.map((tag) => (
                        <Link
                          key={tag}
                          to={`/browse?tag=${encodeURIComponent(tag)}`}
                          className="sb-chip sb-chip-inactive border-emerald-900/60 bg-emerald-950/30 px-3 py-1.5 text-emerald-100"
                        >
                          {tag}
                        </Link>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ) : (
              <p className="mt-4 text-sm leading-6 text-zinc-500">
                Add tags to connect this item to saved library views and later retrieval workflows.
              </p>
            )}
          </DetailSection>

          {item.source_type === "feed_article" ? (
            <DetailSection
              title="Feed source"
              description="Original feed metadata captured alongside the article item."
            >
              <dl className="divide-y divide-zinc-800/70">
                <MetadataRow label="Feed" value={feedName ?? undefined} />
                <MetadataRow
                  label="Feed URL"
                  value={
                    feedUrl ? (
                      <a
                        href={feedUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex min-w-0 max-w-full items-center gap-2 text-amber-200 transition hover:text-white"
                      >
                        <span className="truncate">{feedUrl}</span>
                        <ExternalLink className="h-4 w-4 shrink-0" />
                      </a>
                    ) : undefined
                  }
                />
                <MetadataRow label="Author" value={feedAuthor ?? undefined} />
                <MetadataRow label="Published" value={feedPublished ?? undefined} />
              </dl>
            </DetailSection>
          ) : null}

          <DetailSection
            title="Related items"
            description="Relationship extraction links this item to other captured sources once enrichment finishes."
          >
            {related.length === 0 ? (
              <p className="text-sm leading-6 text-zinc-500">
                No relationships are available yet. Palace will show them here after related-item analysis runs.
              </p>
            ) : (
              <div className="space-y-2">
                {related.map((rel) => (
                  <article
                    key={rel.item_id}
                    className="sb-list-card flex min-w-0 flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="flex min-w-0 items-start gap-3">
                      <div className="mt-0.5 rounded-2xl border border-zinc-800 bg-zinc-950/80 p-2 text-zinc-300">
                        <SourceIcon sourceType={rel.source_type} className="h-4 w-4" />
                      </div>
                      <div className="min-w-0">
                        <Link to={`/items/${rel.item_id}`} className="block truncate text-sm font-medium text-zinc-100 transition hover:text-sky-100">
                          {rel.title}
                        </Link>
                        <p className="mt-1 text-xs uppercase tracking-[0.22em] text-zinc-500">
                          {rel.source_type.replace(/_/g, " ")}
                        </p>
                      </div>
                    </div>
                    <div className="flex w-full shrink-0 items-center justify-end gap-2 sm:w-auto">
                      <RelationshipBadge relationship={rel.relationship} />
                      <span className="text-xs text-zinc-500">{(rel.confidence * 100).toFixed(0)}%</span>
                      <ProvenanceDrawer
                        compact
                        triggerLabel="Evidence"
                        provenance={{
                          title: rel.title,
                          subtitle: `Relationship evidence connected to ${item.title}.`,
                          kind: "canonical_memory",
                          itemId: rel.item_id,
                          sourceType: rel.source_type,
                          scores: [{ label: "Relationship confidence", value: rel.confidence, tone: rel.confidence >= 0.75 ? "good" : "default" }],
                          relationships: [{
                            item_id: item.id,
                            title: item.title,
                            source_type: item.source_type,
                            relationship: rel.relationship,
                            confidence: rel.confidence,
                          }],
                          metadata: [
                            { label: "Relationship", value: rel.relationship.replace(/_/g, " ") },
                            { label: "Source type", value: rel.source_type.replace(/_/g, " ") },
                          ],
                        }}
                      />
                    </div>
                  </article>
                ))}
              </div>
            )}
          </DetailSection>
        </div>
      </div>
    </div>
  );
}
