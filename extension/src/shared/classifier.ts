export type CaptureKind = "selection_note" | "media" | "social_post" | "webpage" | "invalid";

export type CaptureInput = {
  url?: string | null;
  selectionText?: string | null;
};

export type CaptureClassification = {
  kind: CaptureKind;
  url: string | null;
  reason: string;
};

const MEDIA_EXTENSIONS = new Set([
  ".aac",
  ".aiff",
  ".flac",
  ".m4a",
  ".m4v",
  ".mov",
  ".mp3",
  ".mp4",
  ".mpeg",
  ".mpg",
  ".oga",
  ".ogg",
  ".ogv",
  ".wav",
  ".webm",
]);

const SOCIAL_HOSTS = [
  /(^|\.)x\.com$/i,
  /(^|\.)twitter\.com$/i,
  /(^|\.)bsky\.app$/i,
  /(^|\.)threads\.net$/i,
  /(^|\.)reddit\.com$/i,
  /(^|\.)linkedin\.com$/i,
];

const MASTODON_STATUS_PATH = /^\/@[^/]+\/\d+(?:\/)?$/;

function normalizeUrl(rawUrl: string | null | undefined): URL | null {
  const value = rawUrl?.trim();
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
    return parsed;
  } catch {
    return null;
  }
}

function hasSelection(selectionText: string | null | undefined): boolean {
  return Boolean(selectionText?.trim());
}

function isDirectMediaUrl(url: URL): boolean {
  const pathname = url.pathname.toLowerCase();
  return [...MEDIA_EXTENSIONS].some((extension) => pathname.endsWith(extension));
}

function isYouTubeUrl(url: URL): boolean {
  if (/^youtu\.be$/i.test(url.hostname)) return true;
  if (!/(^|\.)youtube\.com$/i.test(url.hostname)) return false;
  return url.pathname.startsWith("/watch") || url.pathname.startsWith("/shorts/");
}

function isSocialUrl(url: URL): boolean {
  if (SOCIAL_HOSTS.some((pattern) => pattern.test(url.hostname))) return true;
  return MASTODON_STATUS_PATH.test(url.pathname) && url.hostname.includes(".");
}

export function classifyCapture(input: CaptureInput): CaptureClassification {
  if (hasSelection(input.selectionText)) {
    return {
      kind: "selection_note",
      url: normalizeUrl(input.url)?.href ?? null,
      reason: "Selected text is saved as a note with source provenance.",
    };
  }

  const parsedUrl = normalizeUrl(input.url);
  if (!parsedUrl) {
    return {
      kind: "invalid",
      url: null,
      reason: "Open an http or https page before saving to Palace.",
    };
  }

  if (isYouTubeUrl(parsedUrl) || isDirectMediaUrl(parsedUrl)) {
    return {
      kind: "media",
      url: parsedUrl.href,
      reason: "Media URLs are queued through the audio/video ingest path.",
    };
  }

  if (isSocialUrl(parsedUrl)) {
    return {
      kind: "social_post",
      url: parsedUrl.href,
      reason: "Social posts are captured through webpage ingest for the MVP.",
    };
  }

  return {
    kind: "webpage",
    url: parsedUrl.href,
    reason: "Ordinary web pages are queued through webpage ingest.",
  };
}

export function labelForCaptureKind(kind: CaptureKind): string {
  switch (kind) {
    case "selection_note":
      return "Selected text";
    case "media":
      return "Media URL";
    case "social_post":
      return "Social post";
    case "webpage":
      return "Webpage";
    case "invalid":
      return "Unsupported page";
  }
}
