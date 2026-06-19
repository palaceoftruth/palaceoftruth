import { useState } from "react";
import { ExternalLink, FileSearch, Image as ImageIcon } from "lucide-react";

import type { ArtifactCitation as ArtifactCitationType, Item } from "../api/types";

interface ArtifactCitationProps {
  citation: ArtifactCitationType | null | undefined;
  compact?: boolean;
}

function metadataRecord(item: Item): Record<string, unknown> {
  return item.metadata_ ?? item.metadata ?? {};
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0) : [];
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asDimensions(value: unknown): ArtifactCitationType["dimensions"] {
  const record = asRecord(value);
  if (!record) return null;
  return {
    width: asNumber(record.width),
    height: asNumber(record.height),
  };
}

export function artifactCitationFromItem(item: Item): ArtifactCitationType | null {
  const metadata = metadataRecord(item);
  const browserImage = asRecord(metadata.browser_capture_image);
  if (browserImage) {
    const sourcePostUrl = asString(browserImage.source_post_url);
    const candidateUrl = asString(browserImage.candidate_url);
    const finalUrl = asString(browserImage.final_url);
    return {
      kind: "browser_image_candidate",
      thumbnail_url: finalUrl ?? candidateUrl,
      caption: asString(browserImage.alt_text),
      source_url: sourcePostUrl ?? item.source_url,
      source_label: sourcePostUrl ? "Parent social post" : "Source",
      original_artifact_url: finalUrl ?? candidateUrl,
      original_artifact_label: finalUrl ?? candidateUrl,
      media_type: asString(browserImage.media_type),
      dimensions: asDimensions(browserImage.dimensions),
      byte_hash: asString(browserImage.byte_hash),
    };
  }

  const imageAnalysis = asRecord(metadata.image_analysis);
  if (!imageAnalysis) return null;

  const artifact = asRecord(imageAnalysis.artifact);
  const vision = asRecord(imageAnalysis.vision);
  const storagePath = asString(artifact?.storage_path);
  const filename = asString(artifact?.filename);
  return {
    kind: "image_analysis",
    caption: asString(imageAnalysis.caption),
    extracted_text: asStringArray(imageAnalysis.visible_text),
    source_url: item.source_url,
    source_label: item.source_url ? "Source" : null,
    thumbnail_url: `/api/v1/items/${item.id}/artifact`,
    original_artifact_url: `/api/v1/items/${item.id}/artifact`,
    original_artifact_label: storagePath ?? filename,
    filename,
    media_type: asString(artifact?.media_type),
    dimensions: asDimensions(imageAnalysis.dimensions),
    model: asString(vision?.model),
    provider: asString(vision?.provider),
    confidence: asNumber(vision?.confidence),
    byte_hash: asString(imageAnalysis.byte_hash),
  };
}

function Dimensions({ dimensions }: { dimensions: ArtifactCitationType["dimensions"] }) {
  if (!dimensions?.width || !dimensions?.height) return null;
  return <span>{dimensions.width} x {dimensions.height}</span>;
}

function Confidence({ confidence }: { confidence: number | null | undefined }) {
  if (typeof confidence !== "number") return null;
  return <span>{Math.round(confidence * 100)}% confidence</span>;
}

function ArtifactThumbnail({ url }: { url: string | null | undefined }) {
  const [failed, setFailed] = useState(false);
  if (!url || failed) {
    return <ImageIcon className="h-5 w-5 text-zinc-500" />;
  }
  return <img src={url} alt="" className="h-full w-full object-cover" loading="lazy" onError={() => setFailed(true)} />;
}

export default function ArtifactCitation({ citation, compact = false }: ArtifactCitationProps) {
  if (!citation) return null;

  const hasMetadata =
    citation.provider ||
    citation.model ||
    typeof citation.confidence === "number" ||
    citation.media_type ||
    citation.dimensions?.width ||
    citation.dimensions?.height;
  const extractedText = citation.extracted_text?.filter(Boolean) ?? [];

  return (
    <div className={compact ? "border-t border-zinc-800/70 pt-3" : "py-1"}>
      <div className="flex min-w-0 gap-3">
        <div className="flex h-16 w-16 shrink-0 items-center justify-center overflow-hidden rounded-xl border border-zinc-800 bg-zinc-900/80">
          <ArtifactThumbnail url={citation.thumbnail_url} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 text-xs font-medium uppercase tracking-[0.22em] text-sky-300/80">
              <FileSearch className="h-3.5 w-3.5" />
              Visual artifact
            </span>
            {citation.filename ? <span className="truncate text-xs text-zinc-500">{citation.filename}</span> : null}
          </div>
          {citation.caption ? (
            <p className="mt-2 line-clamp-2 text-sm leading-6 text-zinc-200">{citation.caption}</p>
          ) : null}
          {extractedText.length > 0 ? (
            <p className="mt-2 line-clamp-2 border-l border-zinc-700 pl-3 text-xs leading-5 text-zinc-400">
              {extractedText.join(" ")}
            </p>
          ) : null}
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-zinc-500">
            {citation.media_type ? <span>{citation.media_type}</span> : null}
            <Dimensions dimensions={citation.dimensions} />
            <Confidence confidence={citation.confidence} />
            {citation.provider || citation.model ? <span>{[citation.provider, citation.model].filter(Boolean).join(" / ")}</span> : null}
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {citation.source_url ? (
              <a
                href={citation.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="sb-chip sb-chip-inactive px-2.5 py-1"
              >
                {citation.source_label ?? "Open source"}
                <ExternalLink className="h-3 w-3" />
              </a>
            ) : null}
            {citation.original_artifact_url ? (
              <a
                href={citation.original_artifact_url}
                target="_blank"
                rel="noopener noreferrer"
                className="sb-chip sb-chip-inactive px-2.5 py-1"
              >
                Inspect original
                <ExternalLink className="h-3 w-3" />
              </a>
            ) : citation.original_artifact_label ? (
              <span className="max-w-full truncate rounded-full border border-zinc-800 bg-zinc-950 px-2.5 py-1 text-xs text-zinc-500">
                {citation.original_artifact_label}
              </span>
            ) : null}
          </div>
        </div>
      </div>
      {hasMetadata ? null : <p className="mt-2 text-xs text-zinc-600">Additional model metadata is not available for this capture.</p>}
    </div>
  );
}
