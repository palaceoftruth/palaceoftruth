import type { CaptureClassification, CaptureKind } from "./classifier.js";
import type { PalaceCredentials } from "./credentials.js";
import type { BrowserImageCandidate } from "./imageCandidates.js";
import type { ImageByteOrigin } from "./imageBytes.js";

export type CaptureRequest = {
  classification: CaptureClassification;
  imageCandidates?: BrowserImageCandidate[];
  pageTitle?: string | null;
  selectionText?: string | null;
  tags?: string[];
};

export type CaptureResult =
  | {
      state: "queued";
      jobId: string;
      routedTo: "media" | "webpage" | "note";
      kind: CaptureKind;
      itemId?: string;
    }
  | { state: "duplicate"; message: string; webSaveId?: string; itemId?: string }
  | { state: "auth_error"; message: string }
  | { state: "error"; message: string };

export type CaptureImageUpload = {
  /** Backed by a plain ArrayBuffer: a Blob part cannot be shared memory. */
  bytes: Uint8Array<ArrayBuffer>;
  mediaType: string;
  byteOrigin: ImageByteOrigin;
  /** Attaches the image to an existing capture. Omit for a standalone one. */
  itemId?: string | null;
  imageUrl?: string | null;
  sourceUrl?: string | null;
  pageTitle?: string | null;
  altText?: string | null;
  width?: number | null;
  height?: number | null;
  role?: string | null;
  order?: number | null;
  tags?: string[];
};

export type CaptureImageResult =
  | { state: "queued"; jobId: string | null; itemId: string; parentItemId: string | null }
  | { state: "duplicate"; itemId: string; message: string }
  | { state: "auth_error"; message: string }
  | { state: "error"; message: string };

export type WebSaveCaptureKind =
  | "webpage"
  | "social_post"
  | "media"
  | "selection_note"
  | "image";

export type WebSave = {
  id: string;
  item_id: string;
  original_url: string;
  normalized_url: string;
  source_title: string | null;
  source_domain: string | null;
  capture_kind: WebSaveCaptureKind;
  user_tags: string[];
  saved_at: string;
  archived_at: string | null;
  item: {
    id: string;
    title: string;
    source_type: string;
    status: string;
    summary: string | null;
    tags: string[];
  };
};

export type WebSaveLookupResult =
  | { state: "ready"; saved: WebSave | null; related: WebSave[] }
  | { state: "auth_error"; message: string }
  | { state: "error"; message: string };

type ImageUploadResponse = {
  job_id?: string | null;
  item_id?: string;
  status?: string;
  parent_item_id?: string | null;
  duplicate_of?: string | null;
};

type IngestResponse = {
  job_id?: string;
  item_id?: string;
  route?: "media" | "webpage" | "note";
  kind?: CaptureKind;
  status?: string;
  duplicate_of?: string;
  web_save_id?: string;
};

type WebSaveListResponse = {
  web_saves?: WebSave[];
  total?: number;
  page?: number;
  per_page?: number;
};

type ExtensionTokenResponse = {
  access_token?: string;
  expires_at?: string;
  expires_in?: number;
};

function cleanTags(tags: string[] | undefined): string[] {
  return [...new Set((tags ?? []).map((tag) => tag.trim()).filter(Boolean))];
}

function detailMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return fallback;
}

async function parseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return response.text().catch(() => "");
  }
  return response.json().catch(() => null);
}

function buildEndpoint(credentials: PalaceCredentials, path: string): string {
  return `${credentials.apiBaseUrl}/api/v1${path}`;
}

function buildBaseEndpoint(apiBaseUrl: string, path: string): string {
  return `${apiBaseUrl.replace(/\/+$/, "")}/api/v1${path}`;
}

export async function issueExtensionToken(
  apiBaseUrl: string,
  pairingKey: string,
  extensionVersion: string,
  fetchImpl: typeof fetch = fetch,
): Promise<PalaceCredentials> {
  const normalizedBaseUrl = apiBaseUrl.replace(/\/+$/, "");
  const response = await fetchImpl(buildBaseEndpoint(normalizedBaseUrl, "/palace/browser-extension-tokens"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Palace-Pairing-Key": pairingKey,
    },
    body: JSON.stringify({
      display_name: "Palace Capture Extension",
      extension_version: extensionVersion,
    }),
  });
  const body = await parseBody(response);
  if (response.status !== 201) {
    throw new Error(detailMessage(body, `Palace returned HTTP ${response.status}.`));
  }
  const token = (body as ExtensionTokenResponse | null)?.access_token;
  if (typeof token !== "string" || !token.trim()) {
    throw new Error("Palace did not return a capture token.");
  }
  return {
    apiBaseUrl: normalizedBaseUrl,
    accessToken: token,
    expiresAt: (body as ExtensionTokenResponse | null)?.expires_at,
  };
}

function routeForKind(kind: CaptureKind): "media" | "webpage" | "note" | null {
  if (kind === "media") return "media";
  // An image only reaches this path as the fallback for a byte upload that
  // could not be read. Palace then fetches the URL itself, as it did before.
  if (kind === "webpage" || kind === "social_post" || kind === "image") return "webpage";
  if (kind === "selection_note") return "note";
  return null;
}

function normalizeComparableUrl(value: string | null | undefined): string | null {
  if (!value?.trim()) return null;
  try {
    const parsed = new URL(value.trim());
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    const path = parsed.pathname === "/" ? "" : parsed.pathname;
    return `${parsed.protocol}//${parsed.hostname.toLowerCase()}${parsed.port ? `:${parsed.port}` : ""}${path}${parsed.search}`;
  } catch {
    return null;
  }
}

function hostnameForUrl(value: string): string | null {
  try {
    return new URL(value).hostname.toLowerCase();
  } catch {
    return null;
  }
}

function webSaveTitle(save: WebSave): string {
  return save.source_title?.trim() || save.item.title?.trim() || save.normalized_url;
}

async function listWebSaves(
  credentials: PalaceCredentials,
  params: Record<string, string | number | boolean>,
  fetchImpl: typeof fetch,
): Promise<WebSaveLookupResult> {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => query.set(key, String(value)));
  try {
    const response = await fetchImpl(buildEndpoint(credentials, `/web-saves?${query}`), {
      method: "GET",
      headers: {
        Authorization: `Bearer ${credentials.accessToken}`,
        "X-Palace-Extension-Version": "0.1.0",
      },
    });
    const body = await parseBody(response);
    if (response.status === 403) {
      return { state: "auth_error", message: detailMessage(body, "Palace rejected the capture token.") };
    }
    if (response.status !== 200) {
      return { state: "error", message: detailMessage(body, `Palace returned HTTP ${response.status}.`) };
    }
    const webSaves = (body as WebSaveListResponse | null)?.web_saves;
    if (!Array.isArray(webSaves)) {
      return { state: "error", message: "Palace returned an invalid Web Saves response." };
    }
    return { state: "ready", saved: null, related: webSaves };
  } catch (error) {
    return {
      state: "error",
      message: error instanceof Error ? error.message : "Network request failed.",
    };
  }
}

export async function lookupWebSavesForUrl(
  credentials: PalaceCredentials,
  url: string,
  fetchImpl: typeof fetch = fetch,
): Promise<WebSaveLookupResult> {
  const normalized = normalizeComparableUrl(url);
  if (!normalized) return { state: "ready", saved: null, related: [] };

  const exactResult = await listWebSaves(
    credentials,
    { active_only: true, q: normalized, per_page: 10, sort: "saved_at", order: "desc" },
    fetchImpl,
  );
  if (exactResult.state !== "ready") return exactResult;

  const saved =
    exactResult.related.find((save) => normalizeComparableUrl(save.normalized_url) === normalized) ??
    exactResult.related.find((save) => normalizeComparableUrl(save.original_url) === normalized) ??
    null;

  const hostname = hostnameForUrl(normalized);
  const relatedResult = hostname
    ? await listWebSaves(
        credentials,
        { active_only: true, q: hostname, per_page: 6, sort: "saved_at", order: "desc" },
        fetchImpl,
      )
    : exactResult;
  if (relatedResult.state !== "ready") return relatedResult;

  const related = relatedResult.related
    .filter((save) => save.id !== saved?.id)
    .slice(0, 5)
    .sort((left, right) => webSaveTitle(left).localeCompare(webSaveTitle(right)));
  return { state: "ready", saved, related };
}

export async function submitCapture(
  credentials: PalaceCredentials,
  request: CaptureRequest,
  fetchImpl: typeof fetch = fetch,
): Promise<CaptureResult> {
  const route = routeForKind(request.classification.kind);
  if (!route) {
    return { state: "error", message: request.classification.reason };
  }

  const payload = {
    url: request.classification.url,
    page_title: request.pageTitle ?? null,
    selection_text: request.selectionText ?? null,
    tags: cleanTags(request.tags),
    detected_kind: request.classification.kind,
    image_candidates: request.imageCandidates ?? [],
    browser_extension_version: "0.1.0",
    extension_metadata: {
      classifier_reason: request.classification.reason,
    },
  };

  try {
    const response = await fetchImpl(buildEndpoint(credentials, "/capture/browser"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${credentials.accessToken}`,
        "X-Palace-Extension-Version": "0.1.0",
      },
      body: JSON.stringify(payload),
    });
    const body = await parseBody(response);

    if (response.status === 202) {
      if ((body as IngestResponse | null)?.status === "duplicate") {
        return {
          state: "duplicate",
          message: "This URL is already saved in Palace.",
          webSaveId: (body as IngestResponse | null)?.web_save_id,
          itemId: (body as IngestResponse | null)?.duplicate_of ?? (body as IngestResponse | null)?.item_id,
        };
      }
      const jobId = (body as IngestResponse | null)?.job_id;
      if (typeof jobId !== "string" || !jobId) {
        return { state: "error", message: "Palace queued the capture without a job id." };
      }
      return {
        state: "queued",
        jobId,
        routedTo: (body as IngestResponse | null)?.route ?? route,
        kind: (body as IngestResponse | null)?.kind ?? request.classification.kind,
        itemId: (body as IngestResponse | null)?.item_id,
      };
    }
    if (response.status === 403) {
      return { state: "auth_error", message: detailMessage(body, "Palace rejected the API key.") };
    }
    if (response.status === 409) {
      return { state: "duplicate", message: detailMessage(body, "This URL is already in Palace.") };
    }
    return { state: "error", message: detailMessage(body, `Palace returned HTTP ${response.status}.`) };
  } catch (error) {
    return {
      state: "error",
      message: error instanceof Error ? error.message : "Network request failed.",
    };
  }
}

const UPLOAD_FILENAMES: Record<string, string> = {
  "image/jpeg": "capture.jpg",
  "image/png": "capture.png",
  "image/gif": "capture.gif",
  "image/webp": "capture.webp",
};

function appendOptional(form: FormData, field: string, value: string | number | null | undefined): void {
  if (value === null || value === undefined) return;
  const text = String(value).trim();
  if (!text) return;
  form.set(field, text);
}

/**
 * Uploads image bytes the browser already holds.
 *
 * Palace types the upload from the bytes, so the file name below is only a
 * label. With `itemId` the image joins an existing capture; without one it
 * becomes an image capture of its own.
 */
export async function uploadCaptureImage(
  credentials: PalaceCredentials,
  upload: CaptureImageUpload,
  fetchImpl: typeof fetch = fetch,
): Promise<CaptureImageResult> {
  const form = new FormData();
  const filename = UPLOAD_FILENAMES[upload.mediaType] ?? "capture.png";
  form.set("file", new Blob([upload.bytes], { type: upload.mediaType }), filename);
  form.set("origin", upload.byteOrigin);
  appendOptional(form, "item_id", upload.itemId);
  appendOptional(form, "image_url", upload.imageUrl);
  appendOptional(form, "source_url", upload.sourceUrl);
  appendOptional(form, "page_title", upload.pageTitle);
  appendOptional(form, "alt_text", upload.altText);
  appendOptional(form, "width", upload.width);
  appendOptional(form, "height", upload.height);
  appendOptional(form, "role", upload.role);
  appendOptional(form, "order", upload.order);
  const tags = cleanTags(upload.tags);
  if (tags.length) form.set("tags", tags.join(","));

  try {
    const response = await fetchImpl(buildEndpoint(credentials, "/capture/browser/images"), {
      method: "POST",
      // Content-Type is left unset on purpose: fetch adds the multipart
      // boundary, and setting it by hand would drop that boundary.
      headers: {
        Authorization: `Bearer ${credentials.accessToken}`,
        "X-Palace-Extension-Version": "0.1.0",
      },
      body: form,
    });
    const body = await parseBody(response);

    if (response.status === 202) {
      const itemId = (body as ImageUploadResponse | null)?.item_id;
      if (typeof itemId !== "string" || !itemId) {
        return { state: "error", message: "Palace stored the image without an item id." };
      }
      if ((body as ImageUploadResponse | null)?.status === "duplicate") {
        return { state: "duplicate", itemId, message: "This image is already in Palace." };
      }
      return {
        state: "queued",
        jobId: (body as ImageUploadResponse | null)?.job_id ?? null,
        itemId,
        parentItemId: (body as ImageUploadResponse | null)?.parent_item_id ?? null,
      };
    }
    if (response.status === 403) {
      return { state: "auth_error", message: detailMessage(body, "Palace rejected the capture token.") };
    }
    if (response.status === 409) {
      return { state: "error", message: detailMessage(body, "This image is already in Palace.") };
    }
    return { state: "error", message: detailMessage(body, `Palace returned HTTP ${response.status}.`) };
  } catch (error) {
    return {
      state: "error",
      message: error instanceof Error ? error.message : "Network request failed.",
    };
  }
}
