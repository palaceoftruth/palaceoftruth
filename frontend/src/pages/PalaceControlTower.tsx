import { FormEvent, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, Clipboard, Loader2, Pencil, RadioTower, RefreshCw, Trash2, X } from "lucide-react";

import { api, ApiError } from "../api/client";
import type {
  PalaceControlTower,
  PalaceMemoryJobScope,
  McpClientConfigSnippets,
  McpOAuthClientRegisterResponse,
  McpOAuthClientSummary,
  McpOperationScope,
  PalaceRunSummary,
  PalaceSyncSource,
} from "../api/types";
import PageHeader from "../components/PageHeader";
import PalaceStateBanner from "../components/PalaceStateBanner";
import StatePanel from "../components/StatePanel";
import { useToast } from "../context/ToastContext";

function parseExtensionsInput(value: string): string[] {
  return value
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

function relative(dateStr: string | null | undefined): string {
  if (!dateStr) return "Never";
  const diffMs = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function errorMessage(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function formatMemoryScope(scope: PalaceMemoryJobScope): string {
  if (scope.type === "tenant_shared") {
    return "tenant shared";
  }
  return `${scope.type}: ${scope.key}`;
}

function formatFact(fact: {
  subject: string;
  predicate: string;
  object_text: string;
}): string {
  return `${fact.subject} ${fact.predicate} ${fact.object_text}`;
}

function formatWakeupBriefScope(scopeType: "tenant" | "wing", scopeKey: string | null | undefined): string {
  if (scopeType === "tenant") {
    return "Tenant shared";
  }
  return scopeKey ? `Wing ${scopeKey}` : "Wing brief";
}

function formatDiaryScope(scopeType: string, scopeKey: string | null | undefined): string {
  return scopeKey ? `${scopeType}: ${scopeKey}` : scopeType;
}

function formatJobType(jobType: string): string {
  return jobType.split("_").join(" ");
}

function formatOperation(value: string): string {
  return value.split("_").join(" ");
}

function formatProgressPhase(value: string): string {
  return value.split("_").join(" ");
}

function formatScopes(scopes: string[]): string {
  return scopes.length ? scopes.join(", ") : "none";
}

function formatDurationSeconds(seconds: number | null | undefined): string {
  if (seconds == null) return "None";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  if (minutes < 60) return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

function formatCandidateScore(score: number): string {
  return `${Math.round(score * 100)}%`;
}

type SourceFormState = {
  name: string;
  root_path: string;
  source_kind: "folder" | "repo" | "s3";
  credential_type: "none" | "github_pat" | "deployment_github_pat" | "ssh_key";
  github_pat: string;
  ssh_private_key: string;
  scan_interval_seconds: number;
  allowed_extensions: string;
  bucket: string;
  prefix: string;
  endpoint_url: string;
  region: string;
  force_path_style: boolean;
};

function emptySourceForm(): SourceFormState {
  return {
    name: "",
    root_path: "",
    source_kind: "folder",
    credential_type: "none",
    github_pat: "",
    ssh_private_key: "",
    scan_interval_seconds: 900,
    allowed_extensions: "",
    bucket: "",
    prefix: "",
    endpoint_url: "",
    region: "",
    force_path_style: false,
  };
}

function sourceFormFromSource(source: PalaceSyncSource): SourceFormState {
  return {
    name: source.name,
    root_path: source.source_kind === "s3" ? "" : source.root_path,
    source_kind: source.source_kind,
    credential_type: source.credential_type,
    github_pat: "",
    ssh_private_key: "",
    scan_interval_seconds: source.scan_interval_seconds,
    allowed_extensions: source.allowed_extensions?.join(",") ?? "",
    bucket: source.bucket ?? "",
    prefix: source.prefix ?? "",
    endpoint_url: source.endpoint_url ?? "",
    region: source.region ?? "",
    force_path_style: Boolean(source.force_path_style),
  };
}

const MCP_SCOPE_OPTIONS: Array<{ value: McpOperationScope; label: string }> = [
  { value: "read", label: "Read" },
  { value: "write", label: "Write" },
  { value: "local_only", label: "Local only" },
  { value: "destructive_prohibited", label: "No destructive tools" },
  { value: "admin", label: "Admin" },
];

const DEFAULT_MCP_FORM = {
  client_key: "codex-remote",
  display_name: "Codex remote MCP",
  allowed_scopes: ["read", "write", "destructive_prohibited"] as McpOperationScope[],
  token_ttl_seconds: 3600,
};

function SecretSafeSnippet({
  title,
  value,
  onCopy,
}: {
  title: string;
  value: string;
  onCopy: (title: string, value: string) => void;
}) {
  return (
    <div className="min-w-0 rounded-2xl border border-zinc-800 bg-zinc-950/80 p-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs font-medium uppercase tracking-[0.2em] text-zinc-500">{title}</p>
        <button type="button" onClick={() => onCopy(title, value)} className="sb-button-ghost px-2 py-1 text-xs">
          <Clipboard className="h-3.5 w-3.5" />
          Copy
        </button>
      </div>
      <pre className="mt-3 max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-xl bg-black/30 p-3 text-xs leading-6 text-zinc-300">
        {value}
      </pre>
    </div>
  );
}

export default function PalaceControlTowerPage() {
  const [tower, setTower] = useState<PalaceControlTower | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [editingSourceId, setEditingSourceId] = useState<string | null>(null);
  const [deletingSourceId, setDeletingSourceId] = useState<string | null>(null);
  const [retryingMemoryJobId, setRetryingMemoryJobId] = useState<string | null>(null);
  const [form, setForm] = useState<SourceFormState>(emptySourceForm);
  const [mcpClients, setMcpClients] = useState<McpOAuthClientSummary[]>([]);
  const [mcpSnippets, setMcpSnippets] = useState<McpClientConfigSnippets | null>(null);
  const [mcpForm, setMcpForm] = useState(DEFAULT_MCP_FORM);
  const [mcpSubmitting, setMcpSubmitting] = useState(false);
  const [mcpRevokingId, setMcpRevokingId] = useState<string | null>(null);
  const [mcpRegistration, setMcpRegistration] = useState<McpOAuthClientRegisterResponse | null>(null);
  const navigate = useNavigate();
  const toast = useToast();
  const sourceFormRef = useRef<HTMLFormElement>(null);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [controlTower, clients] = await Promise.all([
        api.getPalaceControlTower(),
        api.listPalaceMcpClients(),
      ]);
      setTower(controlTower);
      setMcpClients(clients.clients);
      setMcpSnippets(clients.config_snippets);
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    const id = setInterval(() => {
      void load();
    }, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (editingSourceId && tower && !tower.sync_sources.some((source) => source.id === editingSourceId)) {
      setEditingSourceId(null);
      setForm(emptySourceForm());
    }
  }, [editingSourceId, tower]);

  const resetSourceForm = () => {
    setEditingSourceId(null);
    setForm(emptySourceForm());
  };

  const editingSource = tower?.sync_sources.find((source) => source.id === editingSourceId) ?? null;
  const needsPatEntry = form.source_kind === "repo"
    && form.credential_type === "github_pat"
    && (!editingSource || editingSource.credential_type !== "github_pat" || !editingSource.has_stored_credential);
  const needsSshKeyEntry = form.source_kind === "repo"
    && form.credential_type === "ssh_key"
    && (!editingSource || editingSource.credential_type !== "ssh_key" || !editingSource.has_stored_credential);

  const handleSourceSubmit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    try {
      if (editingSourceId) {
        await api.updatePalaceSyncSource(editingSourceId, sourcePayload);
        toast.success("Sync source updated");
      } else {
        await api.createPalaceSyncSource(sourcePayload);
        toast.success("Sync source added");
      }
      resetSourceForm();
      await load();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const handleSync = async (sourceId: string) => {
    try {
      await api.startPalaceSync(sourceId);
      await load();
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  const handleStartRun = async () => {
    try {
      await api.startPalaceRun();
      await load();
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  const handleRetryRun = async (run: PalaceRunSummary) => {
    try {
      await api.retryPalaceRun(run.id);
      await load();
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  const handleRetryMemoryJob = async (jobId: string) => {
    setRetryingMemoryJobId(jobId);
    try {
      await api.retryMemoryJob(jobId);
      toast.success("Memory write requeued");
      await load();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setRetryingMemoryJobId(null);
    }
  };

  const handleCopySnippet = async (title: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`${title} copied`);
    } catch (err) {
      toast.error(errorMessage(err));
    }
  };

  const handleMcpScopeToggle = (scope: McpOperationScope, checked: boolean) => {
    setMcpForm((prev) => ({
      ...prev,
      allowed_scopes: checked
        ? Array.from(new Set([...prev.allowed_scopes, scope]))
        : prev.allowed_scopes.filter((candidate) => candidate !== scope),
    }));
  };

  const handleMcpRegister = async (event: FormEvent) => {
    event.preventDefault();
    setMcpSubmitting(true);
    setMcpRegistration(null);
    try {
      const result = await api.registerPalaceMcpClient({
        client_key: mcpForm.client_key.trim(),
        display_name: mcpForm.display_name.trim(),
        allowed_scopes: mcpForm.allowed_scopes,
        token_ttl_seconds: mcpForm.token_ttl_seconds,
      });
      setMcpRegistration(result);
      toast.success("MCP agent registered");
      const clients = await api.listPalaceMcpClients();
      setMcpClients(clients.clients);
      setMcpSnippets(clients.config_snippets);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setMcpSubmitting(false);
    }
  };

  const handleRevokeMcpClient = async (client: McpOAuthClientSummary) => {
    const confirmed = window.confirm(
      `Revoke MCP client "${client.display_name}"?\n\nExisting OAuth tokens for this client will stop working.`,
    );
    if (!confirmed) return;
    setMcpRevokingId(client.id);
    try {
      await api.revokePalaceMcpClient(client.id);
      toast.success("MCP client revoked");
      const clients = await api.listPalaceMcpClients();
      setMcpClients(clients.clients);
      setMcpSnippets(clients.config_snippets);
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setMcpRevokingId(null);
    }
  };

  const handleEditSource = (source: PalaceSyncSource) => {
    setEditingSourceId(source.id);
    setForm(sourceFormFromSource(source));
    sourceFormRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const handleDeleteSource = async (source: PalaceSyncSource) => {
    const confirmed = window.confirm(
      `Disable sync source "${source.name}"?\n\nThis keeps the source metadata for audit and deactivates its indexed items in Palace and search.`,
    );
    if (!confirmed) {
      return;
    }

    setDeletingSourceId(source.id);
    try {
      const result = await api.deletePalaceSyncSource(source.id);
      if (editingSourceId === source.id) {
        resetSourceForm();
      }
      toast.success(
        result.items_deactivated > 0
          ? `Source disabled and ${result.items_deactivated} items deactivated`
          : "Sync source disabled",
      );
      await load();
    } catch (err) {
      toast.error(errorMessage(err));
    } finally {
      setDeletingSourceId(null);
    }
  };

  const isS3 = form.source_kind === "s3";
  const isRepo = form.source_kind === "repo";
  const canSubmit = Boolean(
    form.name.trim()
    && (isS3 ? form.bucket.trim() : form.root_path.trim())
    && (!needsPatEntry || form.github_pat.trim())
    && (!needsSshKeyEntry || form.ssh_private_key.trim()),
  );
  const canRegisterMcp = Boolean(
    mcpForm.client_key.trim()
    && mcpForm.display_name.trim()
    && mcpForm.allowed_scopes.length
    && mcpForm.token_ttl_seconds >= 60
    && mcpForm.token_ttl_seconds <= 86400,
  );

  const sourcePayload = {
    name: form.name.trim(),
    source_kind: form.source_kind,
    credential_type: isRepo ? form.credential_type : "none",
    scan_interval_seconds: form.scan_interval_seconds,
    allowed_extensions: parseExtensionsInput(form.allowed_extensions),
    ...(isS3 ? {
      bucket: form.bucket.trim(),
      prefix: form.prefix.trim() || (editingSourceId ? null : undefined),
      endpoint_url: form.endpoint_url.trim() || (editingSourceId ? null : undefined),
      region: form.region.trim() || (editingSourceId ? null : undefined),
      force_path_style: form.force_path_style,
    } : {
      root_path: form.root_path.trim(),
      ...(isRepo && form.credential_type === "github_pat" ? {
        ...(form.github_pat.trim() ? { github_pat: form.github_pat.trim() } : {}),
      } : {}),
      ...(isRepo && form.credential_type === "ssh_key" ? {
        ...(form.ssh_private_key.trim() ? { ssh_private_key: form.ssh_private_key.trim() } : {}),
      } : {}),
    }),
  };

  if (loading && !tower && !loadError) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-sm text-zinc-400">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        Loading control tower…
      </div>
    );
  }

  if (loadError && !tower) {
    return (
      <StatePanel
        icon={RefreshCw}
        variant="error"
        title="Control Tower could not load."
        description={loadError}
        action={
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-full border border-rose-700/40 px-4 py-2 text-sm font-medium text-rose-50 transition hover:border-rose-500/60 hover:bg-rose-950/40"
          >
            Try again
          </button>
        }
      />
    );
  }

  const hasSources = Boolean(tower?.sync_sources.length);
  const recentWakeupBriefs = tower?.wakeup_briefs.recent_briefs ?? [];
  const totalWakeupDiaryRollups = recentWakeupBriefs.reduce((sum, brief) => sum + brief.diary_count, 0);
  const artifactHealth = tower?.room_artifacts;
  const consolidation = tower?.consolidation;
  const consolidationCandidates = consolidation?.candidates ?? [];
  const workerBackpressure = tower?.worker_backpressure;

  return (
    <div className="sb-page">
      <button
        onClick={() => navigate("/palace")}
        className="sb-button-ghost self-start"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to Palace
      </button>

      <PageHeader
        eyebrow="Operations"
        title="Palace Control Tower"
        description="Operate sync sources, watch backlog, retry failed runs, and keep Palace honest about what is current."
        actions={
          <button onClick={handleStartRun} disabled={!hasSources} className="sb-button-primary">
            Start Palace run
          </button>
        }
      />

      {tower?.active_palace_run ? (
        <PalaceStateBanner
          banner={{
            kind: "indexing",
            message: `Active Palace run is ${tower.active_palace_run.status}.`,
            detail: `Working generation ${tower.active_palace_run.requested_generation}.`,
          }}
        />
      ) : null}
      {loadError && tower ? (
        <StatePanel
          icon={RefreshCw}
          compact
          variant="error"
          title="Control Tower refresh failed."
          description={loadError}
          action={
            <button
              type="button"
              onClick={() => void load()}
              className="rounded-full border border-rose-700/40 px-4 py-2 text-sm font-medium text-rose-50 transition hover:border-rose-500/60 hover:bg-rose-950/40"
            >
              Reload Control Tower
            </button>
          }
        />
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,0.9fr),minmax(0,1.1fr)]">
        <section className="min-w-0 space-y-4 rounded-3xl border border-zinc-800 bg-zinc-950 p-5 [overflow-wrap:anywhere]">
          <div className="grid gap-3 md:grid-cols-3">
            <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Indexed</p>
              <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.indexed_generation ?? 0}</p>
            </div>
            <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Dirty</p>
              <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.dirty_generation ?? 0}</p>
            </div>
            <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Backlog</p>
              <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.backlog_generation ?? 0}</p>
            </div>
          </div>

          <div className="border-t border-zinc-800 pt-4">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Room artifact health</p>
              <p className="text-xs text-zinc-500">
                Target generation {artifactHealth?.target_generation ?? 0}
              </p>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-2xl border border-zinc-800 bg-zinc-950/70 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Active rooms</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{artifactHealth?.active_rooms ?? 0}</p>
              </div>
              <div className={`rounded-2xl border px-4 py-3 ${artifactHealth?.closets.stale ? "border-amber-900/60 bg-amber-950/20" : "border-emerald-900/50 bg-emerald-950/20"}`}>
                <p className={`text-xs uppercase tracking-[0.2em] ${artifactHealth?.closets.stale ? "text-amber-300/80" : "text-emerald-300/80"}`}>
                  Stale closets
                </p>
                <p className={`mt-2 text-2xl font-semibold ${artifactHealth?.closets.stale ? "text-amber-100" : "text-emerald-100"}`}>
                  {artifactHealth?.closets.stale ?? 0}
                </p>
                <p className="mt-1 text-xs text-zinc-500">{artifactHealth?.closets.fresh ?? 0} fresh</p>
              </div>
              <div className={`rounded-2xl border px-4 py-3 ${artifactHealth?.snapshots.stale ? "border-amber-900/60 bg-amber-950/20" : "border-emerald-900/50 bg-emerald-950/20"}`}>
                <p className={`text-xs uppercase tracking-[0.2em] ${artifactHealth?.snapshots.stale ? "text-amber-300/80" : "text-emerald-300/80"}`}>
                  Stale snapshots
                </p>
                <p className={`mt-2 text-2xl font-semibold ${artifactHealth?.snapshots.stale ? "text-amber-100" : "text-emerald-100"}`}>
                  {artifactHealth?.snapshots.stale ?? 0}
                </p>
                <p className="mt-1 text-xs text-zinc-500">{artifactHealth?.snapshots.fresh ?? 0} fresh</p>
              </div>
              <div className={`rounded-2xl border px-4 py-3 ${artifactHealth?.tunnels.stale ? "border-amber-900/60 bg-amber-950/20" : "border-emerald-900/50 bg-emerald-950/20"}`}>
                <p className={`text-xs uppercase tracking-[0.2em] ${artifactHealth?.tunnels.stale ? "text-amber-300/80" : "text-emerald-300/80"}`}>
                  Stale tunnels
                </p>
                <p className={`mt-2 text-2xl font-semibold ${artifactHealth?.tunnels.stale ? "text-amber-100" : "text-emerald-100"}`}>
                  {artifactHealth?.tunnels.stale ?? 0}
                </p>
                <p className="mt-1 text-xs text-zinc-500">{artifactHealth?.tunnels.fresh ?? 0} fresh</p>
              </div>
            </div>
            {artifactHealth?.blocked_rooms ? (
              <p className="mt-3 text-xs text-amber-200">
                {artifactHealth.blocked_rooms} room{artifactHealth.blocked_rooms === 1 ? "" : "s"} waiting on membership repair before artifacts can refresh.
              </p>
            ) : (
              <p className="mt-3 text-xs text-zinc-500">
                Closet, snapshot, and tunnel repair can run without waiting on membership repair.
              </p>
            )}
          </div>

          <div className="border-t border-zinc-800 pt-4">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Worker backpressure</p>
              <p className="text-xs text-zinc-500">
                {workerBackpressure?.generated_at ? `Sampled ${relative(workerBackpressure.generated_at)}` : "Redis telemetry unavailable"}
              </p>
            </div>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              {workerBackpressure?.queues.length ? workerBackpressure.queues.map((queue) => {
                const hasMediaPressure = queue.key === "media_ingest" && (
                  (queue.db_queued_depth ?? 0) > 0 ||
                  (queue.db_processing_depth ?? 0) > 0 ||
                  (queue.recent_timeout_count ?? 0) > 0 ||
                  (queue.unexpected_function_count ?? 0) > 0
                );
                const blocked = queue.queued_depth > 0 || queue.deferred_depth > 0 || hasMediaPressure || Boolean(queue.telemetry_error);
                return (
                  <div
                    key={queue.key}
                    className={`rounded-2xl border px-4 py-3 ${blocked ? "border-amber-900/60 bg-amber-950/20" : "border-zinc-800 bg-zinc-950/70"}`}
                  >
                    <div className="min-w-0 space-y-2">
                      <div className="min-w-0">
                        <p className={`break-words text-xs uppercase tracking-[0.2em] ${blocked ? "text-amber-300/80" : "text-zinc-500"}`}>
                          {queue.label}
                        </p>
                        <p className="mt-1 break-all text-xs text-zinc-500">{queue.queue_name}</p>
                      </div>
                      <span className="inline-block max-w-full rounded-lg border border-zinc-700 px-2 py-1 text-[11px] leading-5 text-zinc-400 break-words">
                        {queue.functions.join(", ")}
                      </span>
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
                      <div>
                        <p className="text-zinc-500">Ready depth</p>
                        <p className="mt-1 text-lg font-semibold text-zinc-100">{queue.queued_depth}</p>
                      </div>
                      <div>
                        <p className="text-zinc-500">Oldest ready</p>
                        <p className="mt-1 text-lg font-semibold text-zinc-100">
                          {formatDurationSeconds(queue.oldest_queued_age_seconds)}
                        </p>
                      </div>
                      <div>
                        <p className="text-zinc-500">Worker busy</p>
                        <p className="mt-1 text-lg font-semibold text-zinc-100">
                          {queue.worker_concurrency ?? "Unknown"}
                        </p>
                      </div>
                      <div>
                        <p className="text-zinc-500">Recent latency</p>
                        <p className="mt-1 text-lg font-semibold text-zinc-100">
                          {formatDurationSeconds(queue.recent_avg_latency_seconds)}
                        </p>
                      </div>
                    </div>
                    <p className="mt-3 text-xs text-zinc-500">
                      Deferred {queue.deferred_depth} • worker queue {queue.worker_queue_depth ?? "unknown"} • recent failures {queue.recent_failed}
                      {queue.recent_timeout_count ? ` • timeouts ${queue.recent_timeout_count}` : ""}
                    </p>
                    {queue.key === "media_ingest" ? (
                      <div className="mt-3 rounded-xl border border-zinc-800 bg-zinc-950/70 p-3">
                        <div className="grid grid-cols-2 gap-3 text-xs">
                          <div>
                            <p className="text-zinc-500">DB queued</p>
                            <p className="mt-1 text-base font-semibold text-zinc-100">{queue.db_queued_depth ?? 0}</p>
                          </div>
                          <div>
                            <p className="text-zinc-500">DB processing</p>
                            <p className="mt-1 text-base font-semibold text-zinc-100">{queue.db_processing_depth ?? 0}</p>
                          </div>
                          <div>
                            <p className="text-zinc-500">Tenant pressure</p>
                            <p className="mt-1 text-base font-semibold text-zinc-100">{queue.queued_tenant_count ?? 0}</p>
                          </div>
                          <div>
                            <p className="text-zinc-500">Oldest DB queued</p>
                            <p className="mt-1 text-base font-semibold text-zinc-100">
                              {formatDurationSeconds(queue.oldest_db_queued_age_seconds)}
                            </p>
                          </div>
                        </div>
                        <p className="mt-3 text-xs text-zinc-500">
                          Max queued per tenant {queue.max_queued_per_tenant ?? 0} • max processing per tenant {queue.max_processing_per_tenant ?? 0}
                        </p>
                        {queue.tenant_pressure.length ? (
                          <div className="mt-3 space-y-2">
                            {queue.tenant_pressure.map((tenant) => (
                              <div
                                key={tenant.rank}
                                className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-800 px-3 py-2 text-xs text-zinc-400"
                              >
                                <span className="text-zinc-500">Tenant pressure #{tenant.rank}</span>
                                <span className="text-zinc-300">
                                  queued {tenant.queued_depth} • processing {tenant.processing_depth} • oldest {formatDurationSeconds(tenant.oldest_queued_age_seconds)}
                                </span>
                              </div>
                            ))}
                          </div>
                        ) : null}
                        {queue.unexpected_function_count ? (
                          <p className="mt-3 break-words text-xs text-rose-300">
                            Unexpected media queue functions: {queue.unexpected_functions.join(", ")}
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                    {queue.telemetry_error ? (
                      <p className="mt-2 text-xs text-rose-300">{queue.telemetry_error}</p>
                    ) : null}
                  </div>
                );
              }) : (
                <p className="text-xs text-zinc-500">
                  No ARQ queue telemetry has been reported yet.
                </p>
              )}
            </div>
          </div>

          <div className="border-t border-zinc-800 pt-4">
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Consolidation candidates</p>
              <p className="text-xs text-zinc-500">{consolidation?.candidate_count ?? 0} detected</p>
            </div>
            {consolidationCandidates.length ? (
              <div className="mt-3 space-y-3">
                {consolidationCandidates.slice(0, 3).map((candidate) => (
                  <div
                    key={`${candidate.room_id}-${candidate.candidate_room_id}`}
                    className="rounded-2xl border border-amber-900/50 bg-amber-950/10 px-4 py-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-zinc-100">
                          {candidate.room_name} / {candidate.candidate_room_name}
                        </p>
                        <p className="mt-1 truncate text-xs text-zinc-500">{candidate.wing_name}</p>
                      </div>
                      <span className="shrink-0 rounded-full border border-amber-700/50 bg-amber-950/40 px-2.5 py-1 text-xs font-medium text-amber-100">
                        {formatCandidateScore(candidate.score)}
                      </span>
                    </div>
                    <p className="mt-2 text-xs text-zinc-400">{candidate.reasons.join(", ")}</p>
                    {candidate.shared_tags.length ? (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {candidate.shared_tags.slice(0, 4).map((tag) => (
                          <span key={tag} className="rounded-full border border-zinc-700 px-2 py-0.5 text-xs text-zinc-400">
                            {tag}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : (
              <p className="mt-3 text-xs text-zinc-500">
                No likely duplicate rooms are being surfaced for human review.
              </p>
            )}
          </div>

          <form ref={sourceFormRef} onSubmit={handleSourceSubmit} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">
                  {editingSource ? "Edit sync source" : "Add sync source"}
                </p>
                {editingSource ? (
                  <p className="mt-1 text-xs text-zinc-500">
                    Update the source path, rotate credentials, or switch auth modes without recreating the source.
                  </p>
                ) : null}
              </div>
              {editingSource ? (
                <button
                  type="button"
                  onClick={resetSourceForm}
                  className="inline-flex items-center gap-2 rounded-full border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-200 transition hover:border-zinc-500 hover:text-white"
                >
                  <X className="h-3.5 w-3.5" />
                  Cancel
                </button>
              ) : null}
            </div>
            <div className="mt-3 grid gap-3">
              <input
                aria-label="Sync source name"
                value={form.name}
                onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
                placeholder="Source name"
                className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
              />
              <div className="grid gap-3 sm:grid-cols-2">
                <select
                  aria-label="Sync source kind"
                  value={form.source_kind}
                  onChange={(event) => {
                    const sourceKind = event.target.value as "folder" | "repo" | "s3";
                    setForm((prev) => ({
                      ...prev,
                      source_kind: sourceKind,
                      allowed_extensions: sourceKind === "s3" && !prev.allowed_extensions ? ".md" : prev.allowed_extensions,
                      force_path_style: sourceKind === "s3" ? prev.force_path_style || true : false,
                      credential_type: sourceKind === "repo" ? prev.credential_type : "none",
                      github_pat: sourceKind === "repo" ? prev.github_pat : "",
                      ssh_private_key: sourceKind === "repo" ? prev.ssh_private_key : "",
                    }));
                  }}
                  className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                >
                  <option value="folder">Folder</option>
                  <option value="repo">Repo</option>
                  <option value="s3">S3 / MinIO</option>
                </select>
                <select
                  aria-label="Sync scan interval"
                  value={form.scan_interval_seconds}
                  onChange={(event) => setForm((prev) => ({ ...prev, scan_interval_seconds: Number(event.target.value) }))}
                  className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                >
                  <option value={900}>15 minutes</option>
                  <option value={1800}>30 minutes</option>
                  <option value={3600}>1 hour</option>
                  <option value={21600}>6 hours</option>
                  <option value={86400}>24 hours</option>
                </select>
              </div>
              {isS3 ? (
                <div className="grid gap-3">
                  <input
                    aria-label="S3 bucket"
                    value={form.bucket}
                    onChange={(event) => setForm((prev) => ({ ...prev, bucket: event.target.value }))}
                    placeholder="palaceoftruth-corpus"
                    className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                  />
                  <input
                    aria-label="S3 prefix"
                    value={form.prefix}
                    onChange={(event) => setForm((prev) => ({ ...prev, prefix: event.target.value }))}
                    placeholder="notes/exampleos"
                    className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                  />
                  <div className="grid gap-3 sm:grid-cols-2">
                    <input
                      aria-label="S3 endpoint URL"
                      value={form.endpoint_url}
                      onChange={(event) => setForm((prev) => ({ ...prev, endpoint_url: event.target.value }))}
                      placeholder="http://minio.minio.svc.cluster.local:9000"
                      className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                    />
                    <input
                      aria-label="S3 region"
                      value={form.region}
                      onChange={(event) => setForm((prev) => ({ ...prev, region: event.target.value }))}
                      placeholder="us-east-1"
                      className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                    />
                  </div>
                  <label className="flex items-center gap-2 text-xs text-zinc-400">
                    <input
                      type="checkbox"
                      checked={form.force_path_style}
                      onChange={(event) => setForm((prev) => ({ ...prev, force_path_style: event.target.checked }))}
                      className="h-4 w-4 rounded border-zinc-700 bg-zinc-950 text-emerald-500 focus:ring-emerald-500"
                    />
                    Force path-style addressing. Keep this on for MinIO. Turn it off for R2 unless needed.
                  </label>
                </div>
              ) : (
                <div className="grid gap-3">
                  <input
                    aria-label={isRepo ? "Repo or local path" : "Folder path"}
                    value={form.root_path}
                    onChange={(event) => setForm((prev) => ({ ...prev, root_path: event.target.value }))}
                    placeholder={isRepo ? "https://github.com/org/repo or /absolute/path/to/local/repo" : "/absolute/path/to/folder"}
                    className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                  />
                  {isRepo ? (
                    <>
                      <select
                        aria-label="Repo credential type"
                        value={form.credential_type}
                        onChange={(event) => {
                          const credentialType = event.target.value as "none" | "github_pat" | "deployment_github_pat" | "ssh_key";
                          setForm((prev) => ({
                            ...prev,
                            credential_type: credentialType,
                            github_pat: credentialType === "github_pat" ? prev.github_pat : "",
                            ssh_private_key: credentialType === "ssh_key" ? prev.ssh_private_key : "",
                          }));
                        }}
                        className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                      >
                        <option value="none">Public repo / no credential</option>
                        <option value="github_pat">Store GitHub PAT</option>
                        <option value="deployment_github_pat">Use deployment GitHub PAT</option>
                        <option value="ssh_key">Store SSH private key</option>
                      </select>
                      <p className="text-xs text-zinc-500">
                        Stored PATs and SSH keys are encrypted at rest. Deployment PAT mode uses the backend&apos;s
                        `GITHUB_PAT` secret and keeps the token out of the database.
                      </p>
                      {editingSource?.source_kind === "repo" && editingSource.has_stored_credential && editingSource.credential_type === form.credential_type ? (
                        <p className="text-xs text-zinc-500">
                          Leave the secret field blank to keep the current stored credential. Enter a new value to rotate it.
                        </p>
                      ) : null}
                      {form.credential_type === "github_pat" ? (
                        <input
                          aria-label="GitHub PAT"
                          value={form.github_pat}
                          onChange={(event) => setForm((prev) => ({ ...prev, github_pat: event.target.value }))}
                          placeholder="github_pat_..."
                          type="password"
                          autoComplete="off"
                          className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                        />
                      ) : null}
                      {form.credential_type === "ssh_key" ? (
                        <textarea
                          aria-label="SSH private key"
                          value={form.ssh_private_key}
                          onChange={(event) => setForm((prev) => ({ ...prev, ssh_private_key: event.target.value }))}
                          placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----"}
                          rows={6}
                          className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
                        />
                      ) : null}
                    </>
                  ) : null}
                </div>
              )}
              <input
                aria-label="Allowed extensions"
                value={form.allowed_extensions}
                onChange={(event) => setForm((prev) => ({ ...prev, allowed_extensions: event.target.value }))}
                placeholder=".md,.markdown"
                className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-100 outline-none transition focus:border-emerald-500"
              />
              <button
                type="submit"
                disabled={submitting || !canSubmit}
                className="rounded-2xl border border-zinc-700 bg-zinc-950 px-4 py-3 text-sm text-zinc-200 transition hover:border-zinc-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting
                  ? editingSource ? "Saving source…" : "Adding source…"
                  : editingSource ? "Save source" : "Add source"}
              </button>
            </div>
          </form>
        </section>

        <section className="min-w-0 space-y-4 rounded-3xl border border-zinc-800 bg-zinc-950 p-5 [overflow-wrap:anywhere]">
          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Sync sources</p>
            <div className="mt-3 space-y-2">
              {tower?.sync_sources.length ? tower.sync_sources.map((source) => (
                <article
                  key={source.id}
                  aria-label={`Sync source ${source.name}`}
                  className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3"
                >
                  <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_9.5rem] xl:items-start">
                    <div className="min-w-0">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <p className="min-w-0 truncate text-sm font-medium text-zinc-100" title={source.name}>
                          {source.name}
                        </p>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] uppercase tracking-[0.2em] text-zinc-400">
                          {source.source_kind}
                        </span>
                      </div>
                      <p className="mt-1 truncate text-xs text-zinc-500" title={source.root_path}>{source.root_path}</p>
                      {source.endpoint_url ? (
                        <p className="mt-1 truncate text-xs text-zinc-500" title={source.endpoint_url}>{source.endpoint_url}</p>
                      ) : null}
                      {source.source_kind === "repo" && source.credential_type !== "none" ? (
                        <p className="mt-1 text-xs text-zinc-500">
                          Credential: {source.credential_type === "deployment_github_pat"
                            ? "deployment GitHub PAT"
                            : source.credential_type === "github_pat"
                              ? `stored GitHub PAT${source.has_stored_credential ? "" : " (missing)"}`
                              : `stored SSH private key${source.has_stored_credential ? "" : " (missing)"}`}
                        </p>
                      ) : null}
                      <p className="mt-2 text-xs text-zinc-400">
                        Last synced {relative(source.last_synced_at)} • every {Math.round(source.scan_interval_seconds / 60)} minutes
                      </p>
                      {source.allowed_extensions?.length ? (
                        <p className="mt-1 text-xs text-zinc-500">
                          Extensions: {source.allowed_extensions.join(", ")}
                        </p>
                      ) : null}
                      {source.last_error ? (
                        <p className="mt-2 break-words text-xs text-rose-300">{source.last_error}</p>
                      ) : null}
                    </div>
                    <div
                      role="group"
                      aria-label={`Actions for ${source.name}`}
                      className="grid grid-cols-1 gap-2 min-[480px]:grid-cols-3 xl:w-36 xl:grid-cols-1"
                    >
                      <button
                        type="button"
                        onClick={() => handleEditSource(source)}
                        className="inline-flex w-full cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-full border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-200 transition hover:border-zinc-500 hover:text-white"
                      >
                        <Pencil className="h-3.5 w-3.5" />
                        Edit
                      </button>
                      <button
                        type="button"
                        onClick={() => void handleDeleteSource(source)}
                        disabled={deletingSourceId === source.id}
                        className="inline-flex w-full cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-full border border-rose-700/40 bg-rose-950/30 px-3 py-1.5 text-xs text-rose-200 transition hover:border-rose-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        {deletingSourceId === source.id ? "Disabling…" : "Disable"}
                      </button>
                      <button
                        type="button"
                        onClick={() => handleSync(source.id)}
                        className="inline-flex w-full cursor-pointer items-center justify-center gap-2 whitespace-nowrap rounded-full border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-xs text-zinc-200 transition hover:border-zinc-500 hover:text-white"
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                        Sync now
                      </button>
                    </div>
                  </div>
                </article>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No sync sources are connected yet."
                  description="Add a folder, repo, or bucket first. Until then, Palace has nothing real to index and every run button is just chrome."
                  action={
                    <button
                      type="button"
                      onClick={() => sourceFormRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })}
                      className="rounded-full border border-emerald-700/40 bg-emerald-950/30 px-4 py-2 text-sm font-medium text-emerald-100 transition hover:border-emerald-500/60 hover:bg-emerald-950/50"
                    >
                      Add the first source
                    </button>
                  }
                />
              )}
            </div>
          </div>

          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Recent sync runs</p>
            <div className="mt-3 space-y-2">
              {tower?.sync_runs.length ? tower.sync_runs.map((run) => (
                <div key={run.id} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium text-zinc-100">{run.sync_source_name}</span>
                    <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                      {run.status}
                    </span>
                  </div>
                  <p className="mt-2 text-xs text-zinc-400">
                    changed {run.files_changed} • skipped {run.files_skipped} • created {run.items_created} • updated {run.items_updated}
                  </p>
                  {run.error_message ? <p className="mt-2 text-xs text-rose-300">{run.error_message}</p> : null}
                </div>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No sync runs yet."
                  description="Once a source is connected and scanned, this rail will show what changed, what was skipped, and where ingest is failing."
                />
              )}
            </div>
          </div>

          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">MCP client activity</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Clients</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.mcp_activity?.registered_clients ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-emerald-900/60 bg-emerald-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-emerald-300/80">Successful</p>
                <p className="mt-2 text-2xl font-semibold text-emerald-100">{tower?.mcp_activity?.recent_success ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-amber-900/60 bg-amber-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-amber-300/80">Denied</p>
                <p className="mt-2 text-2xl font-semibold text-amber-100">{tower?.mcp_activity?.recent_denied ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-rose-900/60 bg-rose-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-rose-300/80">Errors</p>
                <p className="mt-2 text-2xl font-semibold text-rose-100">{tower?.mcp_activity?.recent_error ?? 0}</p>
              </div>
            </div>

            <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
              <form onSubmit={handleMcpRegister} className="min-w-0 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Register MCP agent</p>
                  <p className="mt-1 text-xs leading-6 text-zinc-500">
                    OAuth secrets are shown once after registration. Store them outside Palace config.
                  </p>
                </div>
                <div className="mt-3 grid gap-3">
                  <input
                    aria-label="MCP client key"
                    value={mcpForm.client_key}
                    onChange={(event) => setMcpForm((prev) => ({ ...prev, client_key: event.target.value }))}
                    placeholder="codex-remote"
                    className="sb-input"
                  />
                  <input
                    aria-label="MCP display name"
                    value={mcpForm.display_name}
                    onChange={(event) => setMcpForm((prev) => ({ ...prev, display_name: event.target.value }))}
                    placeholder="Codex remote MCP"
                    className="sb-input"
                  />
                  <input
                    aria-label="MCP token TTL seconds"
                    value={mcpForm.token_ttl_seconds}
                    onChange={(event) => setMcpForm((prev) => ({ ...prev, token_ttl_seconds: Number(event.target.value) }))}
                    min={60}
                    max={86400}
                    type="number"
                    className="sb-input"
                  />
                  <div className="grid gap-2 sm:grid-cols-2">
                    {MCP_SCOPE_OPTIONS.map((scope) => (
                      <label key={scope.value} className="flex min-w-0 items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-xs text-zinc-300">
                        <input
                          type="checkbox"
                          checked={mcpForm.allowed_scopes.includes(scope.value)}
                          onChange={(event) => handleMcpScopeToggle(scope.value, event.target.checked)}
                          className="h-4 w-4 rounded border-zinc-700 bg-zinc-950 text-sky-500 focus:ring-sky-500"
                        />
                        <span>{scope.label}</span>
                      </label>
                    ))}
                  </div>
                  <button type="submit" disabled={!canRegisterMcp || mcpSubmitting} className="sb-button-primary">
                    {mcpSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <RadioTower className="h-4 w-4" />}
                    Register agent
                  </button>
                </div>
              </form>

              <div className="min-w-0 rounded-2xl border border-zinc-800 bg-zinc-900/40 p-4">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Registered agents</p>
                <div className="mt-3 space-y-2">
                  {mcpClients.length ? mcpClients.map((client) => (
                    <div key={client.id} className="min-w-0 rounded-2xl border border-zinc-800 bg-zinc-950/60 px-4 py-3 text-sm">
                      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="break-words font-medium text-zinc-100">{client.display_name}</span>
                            <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                              {client.revoked_at ? "revoked" : "active"}
                            </span>
                          </div>
                          <p className="mt-2 break-all text-xs text-zinc-500">{client.client_key}</p>
                          <p className="mt-2 text-xs text-zinc-400">
                            {client.request_count} requests • {client.success_count} ok • {client.denied_count} denied • {client.error_count} errors
                          </p>
                          <p className="mt-1 text-xs text-zinc-500">
                            scopes {formatScopes(client.allowed_scopes)} • last seen {relative(client.last_seen_at ?? client.last_request_at)}
                          </p>
                        </div>
                        <button
                          type="button"
                          onClick={() => void handleRevokeMcpClient(client)}
                          disabled={Boolean(client.revoked_at) || mcpRevokingId === client.id}
                          className="sb-button-ghost shrink-0 text-rose-200 hover:bg-rose-950/30 hover:text-rose-100"
                        >
                          {mcpRevokingId === client.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                          Revoke
                        </button>
                      </div>
                    </div>
                  )) : (
                    <StatePanel
                      icon={RadioTower}
                      compact
                      variant="empty"
                      title="No registered MCP agents."
                      description="Register Codex or another MCP-capable client here, then copy the OAuth or local stdio config into the client runtime."
                    />
                  )}
                </div>
              </div>
            </div>

            {mcpRegistration ? (
              <div className="mt-4 rounded-2xl border border-sky-800/70 bg-sky-950/20 p-4">
                <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                  <div>
                    <p className="text-xs uppercase tracking-[0.2em] text-sky-300/80">Copy once secret</p>
                    <p className="mt-1 text-sm text-sky-100">{mcpRegistration.client.display_name}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => void handleCopySnippet("Client secret", mcpRegistration.client_secret)}
                    className="sb-button-secondary shrink-0"
                  >
                    <Clipboard className="h-4 w-4" />
                    Copy client secret
                  </button>
                </div>
                <p className="mt-3 text-xs leading-6 text-sky-100/75">
                  {mcpRegistration.config_snippets?.secret_handling_note}
                </p>
                <div className="mt-4 grid gap-3 lg:grid-cols-2">
                  <SecretSafeSnippet
                    title="OAuth HTTP config"
                    value={mcpRegistration.config_snippets?.http_oauth_toml ?? ""}
                    onCopy={handleCopySnippet}
                  />
                  <SecretSafeSnippet
                    title="Token command"
                    value={mcpRegistration.config_snippets?.oauth_token_command ?? ""}
                    onCopy={handleCopySnippet}
                  />
                </div>
              </div>
            ) : mcpSnippets ? (
              <div className="mt-4 grid gap-3 lg:grid-cols-2">
                <SecretSafeSnippet title="Codex stdio config" value={mcpSnippets.codex_stdio_toml} onCopy={handleCopySnippet} />
                <SecretSafeSnippet title="Legacy API-key config" value={mcpSnippets.legacy_api_key_toml} onCopy={handleCopySnippet} />
              </div>
            ) : null}

            <div className="mt-3 space-y-2">
              {tower?.mcp_activity?.recent_events.length ? tower.mcp_activity.recent_events.map((event) => (
                <div key={event.id} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">{event.client_name}</span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {formatOperation(event.operation)}
                        </span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {event.status}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        {event.required_scope ? `scope ${event.required_scope}` : "scope none"} • {event.latency_ms ?? 0}ms • {relative(event.created_at)}
                      </p>
                      {event.error_class ? <p className="mt-2 text-xs text-rose-300">{event.error_class}</p> : null}
                    </div>
                    <span className="text-xs text-zinc-500">{event.client_key}</span>
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RadioTower}
                  compact
                  variant="empty"
                  title="No MCP client calls yet."
                  description="Once Codex or another agent calls the Palace MCP adapter, this rail shows client identity, scope decisions, and redacted request outcomes."
                />
              )}
            </div>
          </div>

          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Memory reliability</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Queued</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.memory_health.queued ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Processing</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.memory_health.processing ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-rose-900/60 bg-rose-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-rose-300/80">Failed</p>
                <p className="mt-2 text-2xl font-semibold text-rose-100">{tower?.memory_health.failed ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-amber-900/60 bg-amber-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-amber-300/80">Retryable</p>
                <p className="mt-2 text-2xl font-semibold text-amber-100">{tower?.memory_health.retryable ?? 0}</p>
              </div>
            </div>

            <div className="mt-3 space-y-2">
              {tower?.memory_health.recent_jobs.length ? tower.memory_health.recent_jobs.map((job) => (
                <div key={job.job_id} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">{job.title}</span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {job.status}
                        </span>
                        {job.accepted_as ? (
                          <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                            {job.accepted_as === "canonical" ? "canonical" : "legacy adapter"}
                          </span>
                        ) : null}
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        {formatMemoryScope(job.scope)} • created {relative(job.created_at)}
                        {job.source ? ` • source ${job.source}` : ""}
                      </p>
                      {job.error_message ? <p className="mt-2 text-xs text-rose-300">{job.error_message}</p> : null}
                      {job.recent_progress_events.length ? (
                        <div className="mt-3 flex flex-wrap gap-2">
                          {job.recent_progress_events.slice(0, 3).map((event) => (
                            <span
                              key={`${event.phase}-${event.status}-${event.created_at}`}
                              className="rounded-full border border-zinc-800 bg-black/20 px-2 py-1 text-[11px] text-zinc-400"
                            >
                              {formatProgressPhase(event.phase)} {event.progress != null ? `${event.progress}%` : event.status} • {relative(event.created_at)}
                            </span>
                          ))}
                        </div>
                      ) : null}
                    </div>
                    {job.retriable ? (
                      <button
                        type="button"
                        onClick={() => void handleRetryMemoryJob(job.job_id)}
                        disabled={retryingMemoryJobId === job.job_id}
                        className="rounded-full border border-amber-700/40 bg-amber-950/30 px-3 py-1.5 text-xs text-amber-200 transition hover:border-amber-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {retryingMemoryJobId === job.job_id ? "Retrying…" : "Retry memory write"}
                      </button>
                    ) : null}
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No memory jobs yet."
                  description="Once Hermes or the UI writes durable memory, this rail shows the latest jobs and gives you a retry button for failed writes."
                />
              )}
            </div>
          </div>

          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Webhook delivery health</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Configured</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.webhook_health?.configured ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Pending</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.webhook_health?.pending ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-emerald-900/60 bg-emerald-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-emerald-300/80">Triggered</p>
                <p className="mt-2 text-2xl font-semibold text-emerald-100">{tower?.webhook_health?.terminal ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-rose-900/60 bg-rose-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-rose-300/80">Failed jobs</p>
                <p className="mt-2 text-2xl font-semibold text-rose-100">{tower?.webhook_health?.failed_jobs ?? 0}</p>
              </div>
            </div>

            <div className="mt-3 space-y-2">
              {tower?.webhook_health?.recent_jobs?.length ? tower.webhook_health.recent_jobs.map((job) => (
                <div key={job.job_id} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">{job.title}</span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {job.status}
                        </span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {formatJobType(job.job_type)}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        completion webhook {job.terminal ? "triggered" : "waiting for terminal status"} • created {relative(job.created_at)}
                      </p>
                      {job.error_message ? <p className="mt-2 text-xs text-rose-300">{job.error_message}</p> : null}
                    </div>
                    {job.terminal ? (
                      <span className="inline-flex items-center gap-2 rounded-full border border-emerald-700/40 bg-emerald-950/30 px-3 py-1.5 text-xs text-emerald-200">
                        <RadioTower className="h-3.5 w-3.5" aria-hidden="true" />
                        Delivery requested
                      </span>
                    ) : null}
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RadioTower}
                  compact
                  variant="empty"
                  title="No webhook-enabled jobs yet."
                  description="Jobs created with a webhook URL will appear here so operators can see pending and terminal delivery triggers."
                />
              )}
            </div>
          </div>

          <div>
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Wake-up brief freshness</p>
              <p className="text-xs text-zinc-500">
                {tower?.wakeup_briefs.last_refreshed_at
                  ? `Last refreshed ${relative(tower.wakeup_briefs.last_refreshed_at)}`
                  : "No wake-up briefs have landed yet."}
              </p>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl border border-emerald-900/50 bg-emerald-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-emerald-300/80">Fresh briefs</p>
                <p className="mt-2 text-2xl font-semibold text-emerald-100">{tower?.wakeup_briefs.fresh ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-amber-900/60 bg-amber-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-amber-300/80">Stale briefs</p>
                <p className="mt-2 text-2xl font-semibold text-amber-100">{tower?.wakeup_briefs.stale ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Diary rollups covered</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{totalWakeupDiaryRollups}</p>
              </div>
            </div>

            <div className="mt-3 space-y-2">
              {recentWakeupBriefs.length ? recentWakeupBriefs.map((brief) => (
                <div key={`${brief.scope_type}-${brief.scope_key ?? "shared"}-${brief.title}`} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">{brief.title}</span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {formatWakeupBriefScope(brief.scope_type, brief.scope_key)}
                        </span>
                        <span className={`rounded-full border px-2 py-1 text-[11px] ${brief.stale ? "border-amber-700/40 text-amber-200" : "border-emerald-700/40 text-emerald-200"}`}>
                          {brief.stale ? "stale" : "fresh"}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        Refreshed {relative(brief.updated_at)}
                        {tower?.wakeup_briefs.generated_for_day ? ` • day ${tower.wakeup_briefs.generated_for_day}` : ""}
                      </p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                        {brief.room_count} room{brief.room_count === 1 ? "" : "s"}
                      </span>
                      <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                        {brief.diary_count} diary rollup{brief.diary_count === 1 ? "" : "s"}
                      </span>
                      <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                        {brief.fact_count} fact{brief.fact_count === 1 ? "" : "s"}
                      </span>
                    </div>
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No wake-up briefs yet."
                  description="Once the wake-up brief job runs, this rail will show whether diary-backed startup context is fresh or lagging behind the indexed Palace generation."
                />
              )}
            </div>
          </div>

          <div>
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Diary rollup freshness</p>
              <p className="text-xs text-zinc-500">
                {tower?.diary_rollups.expected_through_day
                  ? `Expected through ${tower.diary_rollups.expected_through_day}`
                  : "No completed day is expected yet."}
              </p>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl border border-emerald-900/50 bg-emerald-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-emerald-300/80">Fresh scopes</p>
                <p className="mt-2 text-2xl font-semibold text-emerald-100">{tower?.diary_rollups.fresh ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-amber-900/50 bg-amber-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-amber-300/80">Stale scopes</p>
                <p className="mt-2 text-2xl font-semibold text-amber-100">{tower?.diary_rollups.stale ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Last refreshed</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">
                  {tower?.diary_rollups.last_refreshed_at ? relative(tower.diary_rollups.last_refreshed_at) : "Never"}
                </p>
              </div>
            </div>

            <div className="mt-3 space-y-2">
              {tower?.diary_rollups.recent_rollups.length ? tower.diary_rollups.recent_rollups.map((rollup) => (
                <div key={`${rollup.scope_type}:${rollup.scope_key ?? "shared"}`} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">{rollup.title}</span>
                        <span className={`rounded-full border px-2 py-1 text-[11px] ${rollup.stale ? "border-amber-700/40 text-amber-200" : "border-emerald-700/40 text-emerald-200"}`}>
                          {rollup.stale ? "stale" : "fresh"}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        {formatDiaryScope(rollup.scope_type, rollup.scope_key)} • day {rollup.day} • refreshed {relative(rollup.updated_at)}
                      </p>
                    </div>
                    <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                      {rollup.source_count} source {rollup.source_count === 1 ? "item" : "items"}
                    </span>
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No diary rollups yet."
                  description="Once the daily maintenance job lands, this rail will show whether each workspace, agent, or session scope is covered through the latest completed day."
                />
              )}
            </div>
          </div>

          <div>
            <div className="flex flex-wrap items-baseline justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Fact registry freshness</p>
              <p className="text-xs text-zinc-500">
                {tower?.fact_registry.last_extracted_at
                  ? `Last extracted ${relative(tower.fact_registry.last_extracted_at)}`
                  : "No fact extraction has landed yet."}
              </p>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-3">
              <div className="rounded-2xl border border-emerald-900/50 bg-emerald-950/20 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-emerald-300/80">Active facts</p>
                <p className="mt-2 text-2xl font-semibold text-emerald-100">{tower?.fact_registry.active ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Superseded</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.fact_registry.superseded ?? 0}</p>
              </div>
              <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 px-4 py-3">
                <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Source items</p>
                <p className="mt-2 text-2xl font-semibold text-zinc-100">{tower?.fact_registry.distinct_sources ?? 0}</p>
              </div>
            </div>

            <div className="mt-3 space-y-2">
              {tower?.fact_registry.recent_facts.length ? tower.fact_registry.recent_facts.map((fact) => (
                <div key={fact.id} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">{formatFact(fact)}</span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {fact.status}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        from {fact.source_item_title} • extracted {relative(fact.extracted_at)}
                      </p>
                    </div>
                    <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                      confidence {Math.round(fact.confidence * 100)}%
                    </span>
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No fact registry entries yet."
                  description="Once the extraction sweep runs, this rail will show whether temporal facts are fresh, superseded, or missing entirely."
                />
              )}
            </div>
          </div>

          <div>
            <p className="text-xs uppercase tracking-[0.2em] text-zinc-500">Palace runs</p>
            <div className="mt-3 space-y-2">
              {tower?.palace_runs.length ? tower.palace_runs.map((run) => (
                <div key={run.id} className="rounded-2xl border border-zinc-800 bg-zinc-900/40 px-4 py-3 text-sm">
                  <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium text-zinc-100">Generation {run.requested_generation}</span>
                        <span className="rounded-full border border-zinc-700 px-2 py-1 text-[11px] text-zinc-400">
                          {run.status}
                        </span>
                      </div>
                      <p className="mt-2 text-xs text-zinc-400">
                        {run.triggered_by} • started {relative(run.started_at)} • applied {run.applied_generation}
                      </p>
                      {run.error_message ? <p className="mt-2 text-xs text-rose-300">{run.error_message}</p> : null}
                    </div>
                    {run.status === "failed" ? (
                      <button
                        onClick={() => handleRetryRun(run)}
                        className="rounded-full border border-rose-700/40 bg-rose-950/30 px-3 py-1.5 text-xs text-rose-200 transition hover:border-rose-500 hover:text-white"
                      >
                        Retry run
                      </button>
                    ) : null}
                  </div>
                </div>
              )) : (
                <StatePanel
                  icon={RefreshCw}
                  compact
                  variant="empty"
                  title="No Palace runs yet."
                  description="After the first sync, start a Palace run here to build rooms, summaries, and tunnels from the shared corpus."
                />
              )}
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
