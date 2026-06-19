import type {
  Item,
  ItemListResponse,
  JobStatus,
  WebSave,
  WebSaveCaptureKind,
  WebSaveListResponse,
  SearchResponse,
  ChatMessage,
  ChatResponse,
  GraphResponse,
  RelatedItemsResponse,
  StatsResponse,
  Feed,
  FeedListResponse,
  FeedItemsResponse,
  MemoryJobResponse,
  McpOAuthClientListResponse,
  McpOAuthClientRegisterResponse,
  McpOAuthClientRevokeResponse,
  McpOperationScope,
  OPMLImportResponse,
  ConversationDetail,
  ConversationSummary,
  PalaceControlTower,
  PalaceOverview,
  PalaceRetrieveResponse,
  PalaceRoomDetail,
  PalaceRunSummary,
  PalaceSyncRun,
  PalaceSyncSource,
  PalaceSyncSourceDeleteResponse,
  SourceSubscription,
  SourceSubscriptionEntryListResponse,
  SourceSubscriptionListResponse,
  SourceSubscriptionPreview,
} from "./types";

const API_KEY = import.meta.env.VITE_API_KEY as string | undefined;
const BASE = "/api/v1";

function buildHeaders(init?: RequestInit): Record<string, string> {
  const headers: Record<string, string> = {
    ...((init?.headers as Record<string, string> | undefined) ?? {}),
  };
  if (API_KEY) {
    headers["X-API-Key"] = API_KEY;
  }
  if (!(init?.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = buildHeaders(init);
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  const contentType = res.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

export const api = {
  getStats: () => req<StatsResponse>("/stats"),

  listItems: (params: Record<string, string | number>) =>
    req<ItemListResponse>(`/items?${new URLSearchParams(params as Record<string, string>)}`),

  listWebSaves: (params: {
    page?: number;
    per_page?: number;
    active_only?: boolean;
    q?: string;
    capture_kind?: WebSaveCaptureKind | "";
    tag?: string;
    sort?: "saved_at" | "title";
    order?: "asc" | "desc";
  }) => {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== "") query.set(key, String(value));
    });
    return req<WebSaveListResponse>(`/web-saves?${query}`);
  },

  archiveWebSave: (id: string, archived = true) =>
    req<WebSave>(`/web-saves/${id}`, { method: "PATCH", body: JSON.stringify({ archived }) }),

  getItem: (id: string) => req<Item>(`/items/${id}`),

  updateItem: (id: string, body: { tags?: string[]; title?: string }) =>
    req<Item>(`/items/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteItem: (id: string) =>
    req<{ deleted: boolean; item_id: string; status: string; deleted_at: string }>(`/items/${id}`, { method: "DELETE" }),

  restoreItem: (id: string) =>
    req<{ restored: boolean; item: Item }>(`/items/${id}/restore`, { method: "POST" }),

  getRelated: (id: string) =>
    req<RelatedItemsResponse>(`/items/${id}/related`),

  search: (body: { query: string; source_type?: string; limit?: number }) =>
    req<SearchResponse>("/search", { method: "POST", body: JSON.stringify(body) }),

  chat: (body: { messages: ChatMessage[]; conversation_id?: string }) =>
    req<ChatResponse>("/chat", { method: "POST", body: JSON.stringify(body) }),

  listConversations: () => req<ConversationSummary[]>("/conversations"),

  createConversation: (body?: { title?: string }) =>
    req<ConversationSummary>("/conversations", {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    }),

  getConversation: (id: string) =>
    req<ConversationDetail>(`/conversations/${id}`),

  deleteConversation: (id: string) =>
    req<void>(`/conversations/${id}`, { method: "DELETE" }),

  updateConversation: (id: string, body: { title: string }) =>
    req<ConversationSummary>(`/conversations/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  ingestMedia: (url: string) =>
    req<JobStatus>("/ingest/media", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  /** @deprecated use ingestMedia */
  ingestYoutube: (url: string) =>
    req<JobStatus>("/ingest/media", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  ingestWebpage: (url: string) =>
    req<JobStatus>("/ingest/webpage", {
      method: "POST",
      body: JSON.stringify({ url }),
    }),

  ingestDoc: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<JobStatus>("/ingest/doc", { method: "POST", body: fd });
  },

  ingestImage: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<JobStatus>("/ingest/image", { method: "POST", body: fd });
  },

  /** @deprecated use ingestDoc */
  ingestPdf: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<JobStatus>("/ingest/pdf", { method: "POST", body: fd });
  },

  ingestNote: (body: { title: string; content: string; tags?: string[] }) =>
    req<JobStatus>("/ingest/note", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getJob: (id: string) => req<JobStatus>(`/jobs/${id}`),

  retryMemoryJob: (id: string) =>
    req<MemoryJobResponse>(`/memory/jobs/${id}/retry`, { method: "POST" }),

  getGraph: (params?: { node_limit?: number; edge_limit?: number; include_orphans?: boolean }) => {
    const query = params
      ? `?${new URLSearchParams(
          Object.entries(params).reduce<Record<string, string>>((acc, [key, value]) => {
            if (value !== undefined) acc[key] = String(value);
            return acc;
          }, {}),
        )}`
      : "";
    return req<GraphResponse>(`/graph${query}`);
  },

  listFeeds: () => req<FeedListResponse>("/feeds"),

  createFeed: (body: {
    url: string;
    name?: string;
    auto_tags?: string[];
    poll_interval?: number;
  }) => req<Feed>("/feeds", { method: "POST", body: JSON.stringify(body) }),

  updateFeed: (
    id: string,
    body: { name?: string; auto_tags?: string[]; poll_interval?: number; enabled?: boolean },
  ) => req<Feed>(`/feeds/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  deleteFeed: (id: string) =>
    req<void>(`/feeds/${id}`, { method: "DELETE" }),

  restoreFeed: (id: string) =>
    req<Feed>(`/feeds/${id}/restore`, { method: "POST" }),

  pollFeed: (id: string) =>
    req<void>(`/feeds/${id}/poll`, { method: "POST" }),

  enableFeed: (id: string) =>
    req<Feed>(`/feeds/${id}/enable`, { method: "POST" }),

  disableFeed: (id: string) =>
    req<Feed>(`/feeds/${id}/disable`, { method: "POST" }),

  getFeedItems: (id: string, page = 1, perPage = 10) => {
    const params = new URLSearchParams({
      limit: String(perPage),
      offset: String(Math.max(page - 1, 0) * perPage),
    });
    return req<FeedItemsResponse>(`/feeds/${id}/items?${params}`);
  },

  listSourceSubscriptions: () =>
    req<SourceSubscriptionListResponse>("/source-subscriptions"),

  previewSourceSubscription: (body: {
    source_url: string;
    display_name?: string;
    auto_tags?: string[];
    poll_interval_seconds?: number;
    backfill_enabled?: boolean;
    backfill_limit?: number;
    backfill_published_after?: string;
    provider_type?: "youtube_channel";
  }) =>
    req<SourceSubscriptionPreview>("/source-subscriptions/preview", {
      method: "POST",
      body: JSON.stringify({ provider_type: "youtube_channel", ...body }),
    }),

  createSourceSubscription: (body: {
    source_url: string;
    display_name?: string;
    auto_tags?: string[];
    poll_interval_seconds?: number;
    backfill_enabled?: boolean;
    backfill_limit?: number;
    backfill_published_after?: string;
    provider_type?: "youtube_channel";
  }) =>
    req<SourceSubscription>("/source-subscriptions", {
      method: "POST",
      body: JSON.stringify({ provider_type: "youtube_channel", ...body }),
    }),

  updateSourceSubscription: (
    id: string,
    body: { display_name?: string; auto_tags?: string[]; poll_interval_seconds?: number; paused_reason?: string },
  ) => req<SourceSubscription>(`/source-subscriptions/${id}`, { method: "PATCH", body: JSON.stringify(body) }),

  pauseSourceSubscription: (id: string) =>
    req<SourceSubscription>(`/source-subscriptions/${id}/pause`, { method: "POST" }),

  resumeSourceSubscription: (id: string) =>
    req<SourceSubscription>(`/source-subscriptions/${id}/resume`, { method: "POST" }),

  deleteSourceSubscription: (id: string) =>
    req<void>(`/source-subscriptions/${id}`, { method: "DELETE" }),

  syncSourceSubscription: (id: string) =>
    req<{ status: "queued"; subscription_id: string }>(`/source-subscriptions/${id}/sync`, { method: "POST" }),

  retrySourceSubscriptionEntry: (entryId: string) =>
    req<{ status: "queued"; subscription_id: string; entry_id: string }>(
      `/source-subscriptions/entries/${entryId}/retry`,
      { method: "POST" },
    ),

  listSourceSubscriptionEntries: (id: string, limit = 50) =>
    req<SourceSubscriptionEntryListResponse>(`/source-subscriptions/${id}/entries?${new URLSearchParams({ limit: String(limit) })}`),

  listRecentSourceSubscriptionEntries: (limit = 50) =>
    req<SourceSubscriptionEntryListResponse>(`/source-subscriptions/entries?${new URLSearchParams({ limit: String(limit) })}`),

  getPalaceOverview: () => req<PalaceOverview>("/palace"),

  getPalaceControlTower: () => req<PalaceControlTower>("/palace/control-tower"),

  listPalaceMcpClients: () => req<McpOAuthClientListResponse>("/palace/mcp-clients"),

  registerPalaceMcpClient: (body: {
    client_key: string;
    display_name: string;
    allowed_scopes: McpOperationScope[];
    metadata?: Record<string, unknown>;
    token_ttl_seconds: number;
  }) => req<McpOAuthClientRegisterResponse>("/palace/mcp-clients/register", {
    method: "POST",
    body: JSON.stringify(body),
  }),

  revokePalaceMcpClient: (clientId: string) =>
    req<McpOAuthClientRevokeResponse>(`/palace/mcp-clients/${clientId}/revoke`, { method: "POST" }),

  listPalaceSyncSources: () => req<PalaceSyncSource[]>("/palace/sync-sources"),

  createPalaceSyncSource: (body: {
    name: string;
    root_path?: string;
    source_kind: "folder" | "repo" | "s3";
    credential_type?: "none" | "github_pat" | "deployment_github_pat" | "ssh_key";
    github_pat?: string;
    ssh_private_key?: string;
    scan_interval_seconds: number;
    allowed_extensions?: string[];
    bucket?: string;
    prefix?: string | null;
    endpoint_url?: string | null;
    region?: string | null;
    force_path_style?: boolean;
  }) => req<PalaceSyncSource>("/palace/sync-sources", { method: "POST", body: JSON.stringify(body) }),

  updatePalaceSyncSource: (sourceId: string, body: {
    name?: string;
    root_path?: string;
    source_kind?: "folder" | "repo" | "s3";
    credential_type?: "none" | "github_pat" | "deployment_github_pat" | "ssh_key";
    github_pat?: string;
    ssh_private_key?: string;
    clear_stored_credential?: boolean;
    scan_interval_seconds?: number;
    allowed_extensions?: string[];
    bucket?: string | null;
    prefix?: string | null;
    endpoint_url?: string | null;
    region?: string | null;
    force_path_style?: boolean;
  }) => req<PalaceSyncSource>(`/palace/sync-sources/${sourceId}`, { method: "PATCH", body: JSON.stringify(body) }),

  deletePalaceSyncSource: (sourceId: string) =>
    req<PalaceSyncSourceDeleteResponse>(`/palace/sync-sources/${sourceId}`, { method: "DELETE" }),

  startPalaceSync: (sourceId: string) =>
    req<PalaceSyncRun>(`/palace/sync-sources/${sourceId}/sync`, { method: "POST" }),

  listPalaceSyncRuns: () => req<PalaceSyncRun[]>("/palace/sync-runs"),

  listPalaceRuns: () => req<PalaceRunSummary[]>("/palace/runs"),

  startPalaceRun: () => req<PalaceRunSummary>("/palace/runs", { method: "POST" }),

  retryPalaceRun: (runId: string) =>
    req<PalaceRunSummary>(`/palace/runs/${runId}/retry`, { method: "POST" }),

  getPalaceRoom: (roomId: string) => req<PalaceRoomDetail>(`/palace/rooms/${roomId}`),

  updatePalaceRoom: (roomId: string, body: { name: string }) =>
    req<PalaceRoomDetail>(`/palace/rooms/${roomId}`, { method: "PATCH", body: JSON.stringify(body) }),

  retrievePalace: (body: {
    query: string;
    room_id?: string;
    limit?: number;
    scope_type?: "session" | "agent" | "workspace" | "tenant_shared";
    scope_key?: string;
  }) =>
    req<PalaceRetrieveResponse>("/palace/retrieve", { method: "POST", body: JSON.stringify(body) }),

  pinPalaceItem: (roomId: string, itemId: string) =>
    req<void>(`/palace/rooms/${roomId}/pins`, { method: "POST", body: JSON.stringify({ item_id: itemId }) }),

  unpinPalaceItem: (roomId: string, itemId: string) =>
    req<void>(`/palace/rooms/${roomId}/pins/${itemId}`, { method: "DELETE" }),

  exportLibrary: async (params: {
    format: "json" | "markdown";
    source_type?: string;
    tags?: string;
  }): Promise<void> => {
    const qs = new URLSearchParams({ format: params.format });
    if (params.source_type) qs.set("source_type", params.source_type);
    if (params.tags) qs.set("tags", params.tags);
    const res = await fetch(`${BASE}/export?${qs}`, {
      headers: API_KEY ? { "X-API-Key": API_KEY } : undefined,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      throw new ApiError(res.status, text);
    }
    const blob = await res.blob();
    const today = new Date().toISOString().split("T")[0];
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `palaceoftruth-export-${today}.zip`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  },

  importOPML: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<OPMLImportResponse>("/feeds/import_opml", {
      method: "POST",
      body: fd,
    });
  },
};

export async function streamChat(
  messages: ChatMessage[],
  options: { conversationId?: string } | undefined,
  onToken: (token: string) => void,
  onDone: () => void,
  onError: (err: Error) => void,
  onSources?: (sources: ChatResponse["sources"]) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/chat/stream`, {
    method: "POST",
    headers: {
      ...(API_KEY ? { "X-API-Key": API_KEY } : {}),
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messages,
      conversation_id: options?.conversationId,
    }),
  });
  if (!res.ok) {
    onError(new ApiError(res.status, await res.text()));
    return;
  }
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = line.slice(6);
        if (data === "[DONE]") {
          onDone();
          return;
        }
        try {
          const event = JSON.parse(data) as { type?: string; sources?: ChatResponse["sources"] };
          if (event.type === "usage") {
            continue;
          }
          if (event.type === "sources") {
            onSources?.(event.sources ?? []);
            continue;
          }
        } catch {
          // Token payloads are plain text. Only structured usage events are JSON.
        }
        onToken(data.replace(/\\n/g, "\n"));
      }
    }
  }
  onDone();
}
