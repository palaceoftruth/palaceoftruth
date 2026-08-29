import { useEffect, useState } from "react";
import { AlertTriangle, Check, Edit2, ExternalLink, PencilLine, ShieldAlert, Tag, Trash2, X } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "../api/client";
import type {
  GovernanceCurrentnessState,
  GovernanceRiskClass,
  GovernanceVerificationState,
  Item,
  ItemGovernance,
  RelatedItem,
} from "../api/types";
import ArtifactCitation, { artifactCitationFromItem } from "../components/ArtifactCitation";
import PageHeader from "../components/PageHeader";
import ProvenanceDrawer, { relatedItemsToProvenanceRelationships } from "../components/ProvenanceDrawer";
import RelationshipBadge from "../components/RelationshipBadge";
import SafeExternalLink from "../components/SafeExternalLink";
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

function emptyToNull(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
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

function isoToLocalInput(value: string | null | undefined): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}T${pad(parsed.getHours())}:${pad(
    parsed.getMinutes(),
  )}`;
}

function isExpiredInPast(value: string | null | undefined): boolean {
  if (!value) return false;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return false;
  return parsed.getTime() < Date.now();
}

function governanceFormFromItem(governance: ItemGovernance | null | undefined): ItemGovernance {
  return {
    owner_subject: governance?.owner_subject ?? null,
    reviewer_subject: governance?.reviewer_subject ?? null,
    verification_state: governance?.verification_state ?? null,
    verified_at: governance?.verified_at ?? null,
    verified_by_subject: governance?.verified_by_subject ?? null,
    verification_deadline: governance?.verification_deadline ?? null,
    risk_class: governance?.risk_class ?? null,
    supersession_reason: governance?.supersession_reason ?? null,
    superseded_by_item_id: governance?.superseded_by_item_id ?? null,
    superseded_at: governance?.superseded_at ?? null,
  };
}

interface GovernanceAuditEntry {
  recorded_at?: string;
  actor_subject?: string | null;
  changes?: Array<{ field: string; previous: unknown; next: unknown }>;
}

function latestGovernanceAudit(item: Item): GovernanceAuditEntry | null {
  const history = item.metadata_?.governance_audit;
  if (!Array.isArray(history) || history.length === 0) return null;
  const last = history[history.length - 1];
  if (!last || typeof last !== "object") return null;
  return last as GovernanceAuditEntry;
}

function formatGovernanceAuditChange(entry: GovernanceAuditEntry): string {
  const changes = entry.changes ?? [];
  if (changes.length === 0) return "Recorded change";
  return changes
    .map((change) => {
      const previous = change.previous === null || change.previous === undefined ? "∅" : String(change.previous);
      const next = change.next === null || change.next === undefined ? "∅" : String(change.next);
      return `${change.field}: ${previous} → ${next}`;
    })
    .join("; ");
}

const GOVERNANCE_VERIFICATION_OPTIONS: GovernanceVerificationState[] = [
  "unverified",
  "verified",
  "stale",
  "rejected",
];

const GOVERNANCE_RISK_OPTIONS: GovernanceRiskClass[] = ["low", "moderate", "high", "critical"];

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

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
  const [editingGovernance, setEditingGovernance] = useState(false);
  const [governanceForm, setGovernanceForm] = useState<ItemGovernance>(governanceFormFromItem(null));
  const [savingGovernance, setSavingGovernance] = useState(false);

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
        setGovernanceForm(governanceFormFromItem(itemData.governance));
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

  const handleSaveGovernance = async () => {
    if (!item || !id) return;
    const trimmedUuid = (governanceForm.superseded_by_item_id ?? "").trim();
    if (trimmedUuid && !UUID_PATTERN.test(trimmedUuid)) {
      toast.error("Superseded by item id must be a valid UUID.");
      return;
    }
    if (
      (governanceForm.supersession_reason ?? "").length > 1000
    ) {
      toast.error("Supersession reason must be 1000 characters or fewer.");
      return;
    }
    setSavingGovernance(true);
    const payload: ItemGovernance = {
      owner_subject: emptyToNull(governanceForm.owner_subject),
      reviewer_subject: emptyToNull(governanceForm.reviewer_subject),
      verification_state: governanceForm.verification_state ?? null,
      verified_at: governanceForm.verified_at,
      verified_by_subject: emptyToNull(governanceForm.verified_by_subject),
      verification_deadline: governanceForm.verification_deadline,
      risk_class: governanceForm.risk_class ?? null,
      supersession_reason: emptyToNull(governanceForm.supersession_reason),
      superseded_by_item_id: trimmedUuid || null,
      superseded_at: governanceForm.superseded_at,
    };
    try {
      const updated = await api.updateItem(id, { governance: payload });
      setItem(updated);
      setGovernanceForm(governanceFormFromItem(updated.governance));
      setEditingGovernance(false);
      toast.success("Governance updated");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSavingGovernance(false);
    }
  };

  const handleCancelGovernance = () => {
    if (!item) return;
    setGovernanceForm(governanceFormFromItem(item.governance));
    setEditingGovernance(false);
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
              <SafeExternalLink href={item.source_url} className="sb-chip sb-chip-active">
                Open source
                <ExternalLink className="h-3 w-3" />
              </SafeExternalLink>
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
                    <SafeExternalLink
                      href={item.source_url}
                      className="inline-flex min-w-0 max-w-full items-center gap-2 text-sky-200 transition hover:text-white"
                    >
                      <span className="truncate">{item.source_url}</span>
                      <ExternalLink className="h-4 w-4 shrink-0" />
                    </SafeExternalLink>
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

          {(() => {
            const governance = item.governance ?? null;
            const verificationState = governance?.verification_state ?? null;
            const riskClass = governance?.risk_class ?? null;
            const deadlineExpired = isExpiredInPast(governance?.verification_deadline);
            const deadlineExpiredHighRisk =
              deadlineExpired && (riskClass === "high" || riskClass === "critical");
            const currentnessState: GovernanceCurrentnessState | null = (() => {
              if (governance?.superseded_by_item_id) return "superseded";
              if (verificationState === "rejected") return "superseded";
              if (deadlineExpired) return "expired";
              if (
                (verificationState === "verified" || verificationState === "stale") &&
                !deadlineExpired
              ) {
                return "current";
              }
              return "unassigned";
            })();
            const showCurrentnessWarning =
              verificationState === "rejected" || currentnessState === "expired";
            const auditEntry = latestGovernanceAudit(item);
            const supersessionReason = governance?.supersession_reason ?? null;
            const supersededById = governance?.superseded_by_item_id ?? null;
            const supersededAt = governance?.superseded_at ?? null;
            const governanceHasData =
              !!governance &&
              Object.values(governance).some((value) => value !== null && value !== "");
            return (
              <DetailSection
                title="Governance"
                description="Accountable owner and expiry on important knowledge. Captures who owns the claim, when it was last verified, and whether it has been superseded."
              >
                {showCurrentnessWarning ? (
                  <StatePanel
                    icon={ShieldAlert}
                    compact
                    variant="error"
                    title={
                      verificationState === "rejected"
                        ? "This item has been rejected."
                        : "This item's governance has expired."
                    }
                    description={
                      verificationState === "rejected"
                        ? "Marked rejected on the governance surface. Treat its claims as untrustworthy until a reviewer re-verifies it."
                        : "The verification deadline has passed. Treat its claims as stale until a reviewer re-verifies it."
                    }
                    action={null}
                  />
                ) : null}

                {deadlineExpiredHighRisk ? (
                  <div className="mt-3">
                    <StatePanel
                      icon={AlertTriangle}
                      compact
                      variant="error"
                      title="Expired high-risk content — verify before citing"
                      description="The verification deadline has passed and the risk class is high or critical. Confirm with the owner or a reviewer before relying on this item."
                      action={null}
                    />
                  </div>
                ) : null}

                <div className="mt-3 flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 text-sm text-zinc-400">
                    <ShieldAlert className="h-4 w-4" />
                    <span>
                      {governanceHasData
                        ? "Governance metadata is set."
                        : "No governance metadata assigned yet."}
                    </span>
                  </div>
                  {!editingGovernance ? (
                    <button
                      type="button"
                      onClick={() => setEditingGovernance(true)}
                      className="sb-button-ghost"
                    >
                      <Edit2 className="h-4 w-4" />
                      Edit governance
                    </button>
                  ) : (
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={handleSaveGovernance}
                        disabled={savingGovernance}
                        className="sb-button-primary px-3 py-2"
                      >
                        <Check className="h-4 w-4" />
                        {savingGovernance ? "Saving…" : "Save"}
                      </button>
                      <button
                        type="button"
                        onClick={handleCancelGovernance}
                        disabled={savingGovernance}
                        className="sb-button-ghost"
                      >
                        <X className="h-4 w-4" />
                        Cancel
                      </button>
                    </div>
                  )}
                </div>

                {supersededById ? (
                  <div className="mt-4">
                    <Link
                      to={`/items/${supersededById}`}
                      className="sb-chip border-amber-800/70 bg-amber-950/30 text-amber-100 hover:border-amber-500"
                    >
                      Superseded by item {supersededById}
                    </Link>
                  </div>
                ) : null}

                {editingGovernance ? (
                  <div className="mt-4 space-y-4">
                    <div className="grid gap-4 sm:grid-cols-2">
                      <label className="block">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Owner subject</span>
                        <input
                          type="text"
                          maxLength={200}
                          value={governanceForm.owner_subject ?? ""}
                          onChange={(event) =>
                            setGovernanceForm((prev) => ({ ...prev, owner_subject: event.target.value }))
                          }
                          className="sb-input py-2.5"
                          placeholder="alice"
                        />
                      </label>
                      <label className="block">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Reviewer subject</span>
                        <input
                          type="text"
                          maxLength={200}
                          value={governanceForm.reviewer_subject ?? ""}
                          onChange={(event) =>
                            setGovernanceForm((prev) => ({ ...prev, reviewer_subject: event.target.value }))
                          }
                          className="sb-input py-2.5"
                          placeholder="bob"
                        />
                      </label>
                      <label className="block">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Verification state</span>
                        <select
                          value={governanceForm.verification_state ?? ""}
                          onChange={(event) => {
                            const next = event.target.value as GovernanceVerificationState | "";
                            setGovernanceForm((prev) => ({
                              ...prev,
                              verification_state: next === "" ? null : next,
                            }));
                          }}
                          className="sb-select"
                        >
                          <option value="">Unset</option>
                          {GOVERNANCE_VERIFICATION_OPTIONS.map((option) => (
                            <option key={option} value={option}>
                              {option}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="block">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Risk class</span>
                        <select
                          value={governanceForm.risk_class ?? ""}
                          onChange={(event) => {
                            const next = event.target.value as GovernanceRiskClass | "";
                            setGovernanceForm((prev) => ({
                              ...prev,
                              risk_class: next === "" ? null : next,
                            }));
                          }}
                          className="sb-select"
                        >
                          <option value="">Unset</option>
                          {GOVERNANCE_RISK_OPTIONS.map((option) => (
                            <option key={option} value={option}>
                              {option}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="block sm:col-span-2">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Verification deadline</span>
                        <input
                          type="datetime-local"
                          value={isoToLocalInput(governanceForm.verification_deadline)}
                          onChange={(event) => {
                            const next = event.target.value
                              ? new Date(event.target.value).toISOString()
                              : null;
                            setGovernanceForm((prev) => ({ ...prev, verification_deadline: next }));
                          }}
                          className="sb-input py-2.5"
                        />
                      </label>
                      <label className="block sm:col-span-2">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Supersession reason</span>
                        <textarea
                          maxLength={1000}
                          rows={3}
                          value={governanceForm.supersession_reason ?? ""}
                          onChange={(event) =>
                            setGovernanceForm((prev) => ({
                              ...prev,
                              supersession_reason: event.target.value,
                            }))
                          }
                          className="sb-textarea"
                          placeholder="Why this item has been replaced (optional)"
                        />
                        <span className="mt-1 block text-xs text-zinc-500">
                          {(governanceForm.supersession_reason ?? "").length}/1000 characters
                        </span>
                      </label>
                      <label className="block sm:col-span-2">
                        <span className="mb-2 block text-xs uppercase tracking-[0.22em] text-zinc-500">Superseded by item id</span>
                        <input
                          type="text"
                          value={governanceForm.superseded_by_item_id ?? ""}
                          onChange={(event) =>
                            setGovernanceForm((prev) => ({
                              ...prev,
                              superseded_by_item_id: event.target.value,
                            }))
                          }
                          className="sb-input py-2.5 font-mono"
                          placeholder="00000000-0000-0000-0000-000000000000"
                        />
                      </label>
                    </div>
                  </div>
                ) : (
                  <dl className="mt-4 divide-y divide-zinc-800/70">
                    <MetadataRow
                      label="Owner subject"
                      value={
                        governance?.owner_subject ? <span className="capitalize">{governance.owner_subject}</span> : undefined
                      }
                    />
                    <MetadataRow
                      label="Reviewer subject"
                      value={
                        governance?.reviewer_subject ? (
                          <span className="capitalize">{governance.reviewer_subject}</span>
                        ) : undefined
                      }
                    />
                    <MetadataRow
                      label="Verification state"
                      value={
                        verificationState ? <span className="capitalize">{verificationState}</span> : undefined
                      }
                    />
                    <MetadataRow
                      label="Verified at"
                      value={formatDate(governance?.verified_at, { dateStyle: "medium", timeStyle: "short" }) ?? undefined}
                    />
                    <MetadataRow
                      label="Verified by"
                      value={
                        governance?.verified_by_subject ? (
                          <span className="capitalize">{governance.verified_by_subject}</span>
                        ) : undefined
                      }
                    />
                    <MetadataRow
                      label="Verification deadline"
                      value={formatDate(governance?.verification_deadline, {
                        dateStyle: "medium",
                        timeStyle: "short",
                      }) ?? undefined}
                    />
                    <MetadataRow
                      label="Risk class"
                      value={riskClass ? <span className="capitalize">{riskClass}</span> : undefined}
                    />
                    <MetadataRow
                      label="Supersession reason"
                      value={
                        supersessionReason ? (
                          <span className="whitespace-pre-wrap text-left">{supersessionReason}</span>
                        ) : undefined
                      }
                    />
                    <MetadataRow
                      label="Superseded by"
                      value={
                        supersededById ? (
                          <Link
                            to={`/items/${supersededById}`}
                            className="font-mono text-xs text-amber-200 transition hover:text-white"
                          >
                            {supersededById}
                          </Link>
                        ) : undefined
                      }
                    />
                    <MetadataRow
                      label="Superseded at"
                      value={formatDate(supersededAt, { dateStyle: "medium", timeStyle: "short" }) ?? undefined}
                    />
                  </dl>
                )}

                <details className="group mt-4 overflow-hidden rounded-2xl border border-zinc-800/70 bg-zinc-950/40">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm text-zinc-300 transition hover:text-white">
                    <span>Last governance change</span>
                    <span className="text-xs uppercase tracking-[0.22em] text-zinc-500 transition group-open:text-zinc-300">
                      {auditEntry ? "Toggle" : "Unavailable"}
                    </span>
                  </summary>
                  {auditEntry ? (
                    <div className="space-y-2 border-t border-zinc-800/80 px-4 py-4 text-xs leading-6 text-zinc-400">
                      <p>
                        <span className="text-zinc-500">Recorded:</span>{" "}
                        {formatDate(auditEntry.recorded_at, { dateStyle: "medium", timeStyle: "short" }) ?? "Unknown"}
                      </p>
                      {auditEntry.actor_subject ? (
                        <p>
                          <span className="text-zinc-500">Actor:</span>{" "}
                          <span className="font-mono">{auditEntry.actor_subject}</span>
                        </p>
                      ) : null}
                      <p>
                        <span className="text-zinc-500">Changes:</span>{" "}
                        <span className="font-mono">{formatGovernanceAuditChange(auditEntry)}</span>
                      </p>
                    </div>
                  ) : (
                    <p className="border-t border-zinc-800/80 px-4 py-4 text-xs leading-6 text-zinc-500">
                      No governance audit entries recorded yet.
                    </p>
                  )}
                </details>
              </DetailSection>
            );
          })()}

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
                      <SafeExternalLink
                        href={feedUrl}
                        className="inline-flex min-w-0 max-w-full items-center gap-2 text-amber-200 transition hover:text-white"
                      >
                        <span className="truncate">{feedUrl}</span>
                        <ExternalLink className="h-4 w-4 shrink-0" />
                      </SafeExternalLink>
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
