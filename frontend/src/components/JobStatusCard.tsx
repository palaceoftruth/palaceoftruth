import { Link } from "react-router-dom";
import { CheckCircle, XCircle, Loader } from "lucide-react";
import { useJobPoller } from "../hooks/useJobPoller";
import type { JobProgressEvent } from "../api/types";

interface JobStatusCardProps {
  jobId: string;
}

function formatProgressPhase(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function chunkLabel(event: JobProgressEvent): string | null {
  const metadata = event.metadata_;
  const chunkIndex = metadata?.chunk_index;
  const chunkCount = metadata?.chunk_count;
  if (typeof chunkIndex === "number" && typeof chunkCount === "number" && chunkCount > 1) {
    return `${chunkIndex}/${chunkCount}`;
  }
  return null;
}

export default function JobStatusCard({ jobId }: JobStatusCardProps) {
  const job = useJobPoller(jobId);

  if (!job) {
    return (
      <div className="sb-panel-muted flex items-center gap-3 p-4">
        <Loader className="w-4 h-4 text-indigo-400 animate-spin" />
        <span className="text-gray-400 text-sm">Initializing job…</span>
      </div>
    );
  }

  const isCompleted = job.status === "completed";
  const isFailed = job.status === "failed";
  const isDuplicate = job.status === "duplicate";
  const isRunning = !isCompleted && !isFailed && !isDuplicate;
  const latestEvent = job.recent_progress_events?.[0] ?? null;
  const latestChunkLabel = latestEvent ? chunkLabel(latestEvent) : null;
  const errorText = job.error ?? job.error_message;

  return (
    <div className="sb-panel sb-panel-padding space-y-4">
      <div className="flex items-center gap-3">
        {isCompleted && <CheckCircle className="w-5 h-5 text-green-400 shrink-0" />}
        {isFailed && <XCircle className="w-5 h-5 text-red-400 shrink-0" />}
        {isDuplicate && <CheckCircle className="w-5 h-5 text-amber-400 shrink-0" />}
        {isRunning && <Loader className="w-5 h-5 text-indigo-400 shrink-0 animate-spin" />}
        <div className="flex-1">
          <p className="text-sm font-medium text-zinc-100">
            {isDuplicate ? "Already in your library" : isRunning ? "Processing…" : job.status}
          </p>
          {latestEvent ? (
            <p className="mt-1 text-xs text-zinc-400">
              {latestEvent.message ?? formatProgressPhase(latestEvent.phase)}
              {latestChunkLabel ? ` (${latestChunkLabel})` : ""}
            </p>
          ) : null}
          {errorText && !isDuplicate ? <p className="mt-1 text-xs text-red-400">{errorText}</p> : null}
        </div>
        {isCompleted && job.item_id && (
          <Link
            to={`/items/${job.item_id}`}
            className="shrink-0 text-sm text-sky-300 transition hover:text-white hover:underline"
          >
            View Item →
          </Link>
        )}
        {isDuplicate && job.duplicate_of && (
          <Link
            to={`/items/${job.duplicate_of}`}
            className="shrink-0 text-sm text-amber-300 transition hover:text-white hover:underline"
          >
            View Existing →
          </Link>
        )}
      </div>

      {isRunning && (
        <div className="space-y-1">
          <div className="h-1.5 overflow-hidden rounded-full bg-zinc-900">
            <div
              className="h-full rounded-full bg-sky-500 transition-all duration-500"
              style={{ width: `${Math.max(5, job.progress)}%` }}
            />
          </div>
          <p className="text-right text-xs text-zinc-500">{job.progress}%</p>
          {job.recent_progress_events?.length ? (
            <div className="mt-3 flex flex-wrap gap-2">
              {job.recent_progress_events.slice(0, 3).map((event) => {
                const label = chunkLabel(event);
                return (
                  <span
                    key={`${event.phase}-${event.status}-${event.created_at}`}
                    className="rounded border border-zinc-800 bg-black/20 px-2 py-1 text-[11px] text-zinc-400"
                  >
                    {formatProgressPhase(event.phase)}
                    {label ? ` ${label}` : event.progress != null ? ` ${event.progress}%` : ` ${event.status}`}
                  </span>
                );
              })}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
