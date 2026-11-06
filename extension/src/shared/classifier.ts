export type CaptureKind = "selection_note" | "image" | "media" | "social_post" | "webpage" | "invalid";

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

const IMAGE_EXTENSIONS = new Set([".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"]);

const SOCIAL_HOSTS = [
  /(^|\.)x\.com$/i,
  /(^|\.)twitter\.com$/i,
  /(^|\.)bsky\.app$/i,
  /(^|\.)threads\.net$/i,
  /(^|\.)reddit\.com$/i,
  /(^|\.)linkedin\.com$/i,
];

const MASTODON_STATUS_PATH = /^\/@[^/]+\/\d+(?:\/)?$/;

// Video platforms that publish a page, not a file. yt-dlp resolves these on the
// server, so the extension sends the page URL and Palace downloads the media.
// Only paths that identify one video match: a channel, profile, or video hub
// page is not media.
const FACEBOOK_VIDEO_PATHS = [
  /^\/(?:reel|reels)\/[^/]+/i,
  /^\/[^/]+\/videos\/[^/]+/i,
  /^\/share\/(?:v|r)\/[^/]+/i,
];
const INSTAGRAM_VIDEO_PATH = /^\/(?:reel|reels|tv)\/[^/]+/i;
const TIKTOK_VIDEO_PATH = /^\/(?:@[^/]+\/video\/\d+|t\/[^/]+|v\/\d+)/i;
const TIKTOK_SHORT_HOST_PATH = /^\/[A-Za-z0-9]{5,}\/?$/;
// fb.watch is Facebook's short link. Require a path shape so a bare domain or a
// general-purpose redirect does not classify as media.
const FB_WATCH_PATH = /^\/[A-Za-z0-9_-]{1,64}\/?$/;
const VIMEO_VIDEO_PATH = /^\/(?:\d+|channels\/[^/]+\/\d+|groups\/[^/]+\/videos\/\d+)(?:\/[A-Za-z0-9]+)?\/?$/i;
const VIMEO_PLAYER_PATH = /^\/video\/\d+/i;

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

function isDirectImageUrl(url: URL): boolean {
  const pathname = url.pathname.toLowerCase();
  return [...IMAGE_EXTENSIONS].some((extension) => pathname.endsWith(extension));
}

function isYouTubeUrl(url: URL): boolean {
  if (/^youtu\.be$/i.test(url.hostname)) return true;
  if (!/(^|\.)youtube\.com$/i.test(url.hostname)) return false;
  return url.pathname.startsWith("/watch") || url.pathname.startsWith("/shorts/");
}

function isFacebookVideoUrl(url: URL): boolean {
  const host = url.hostname.toLowerCase();
  if (host === "fb.watch") return FB_WATCH_PATH.test(url.pathname);
  if (!/(^|\.)facebook\.com$/i.test(host)) return false;
  const path = url.pathname;
  if (path === "/watch" || path === "/watch/") return url.searchParams.has("v");
  if (/^\/video\.php$/i.test(path)) {
    return url.searchParams.has("v") || url.searchParams.has("id");
  }
  return FACEBOOK_VIDEO_PATHS.some((pattern) => pattern.test(path));
}

function isInstagramVideoUrl(url: URL): boolean {
  if (!/(^|\.)instagram\.com$/i.test(url.hostname)) return false;
  return INSTAGRAM_VIDEO_PATH.test(url.pathname);
}

function isTikTokVideoUrl(url: URL): boolean {
  const host = url.hostname.toLowerCase();
  if (host === "vm.tiktok.com" || host === "vt.tiktok.com") {
    return TIKTOK_SHORT_HOST_PATH.test(url.pathname);
  }
  if (!/(^|\.)tiktok\.com$/i.test(host)) return false;
  return TIKTOK_VIDEO_PATH.test(url.pathname);
}

function isVimeoVideoUrl(url: URL): boolean {
  const host = url.hostname.toLowerCase();
  if (host === "player.vimeo.com") return VIMEO_PLAYER_PATH.test(url.pathname);
  if (!/(^|\.)vimeo\.com$/i.test(host)) return false;
  return VIMEO_VIDEO_PATH.test(url.pathname);
}

function isVideoPlatformUrl(url: URL): boolean {
  return (
    isFacebookVideoUrl(url) ||
    isInstagramVideoUrl(url) ||
    isTikTokVideoUrl(url) ||
    isVimeoVideoUrl(url)
  );
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

  // Checked before media and social: an image is uploaded as its own bytes,
  // which is the only route that works when Palace cannot fetch the URL.
  if (isDirectImageUrl(parsedUrl)) {
    return {
      kind: "image",
      url: parsedUrl.href,
      reason: "The image file itself is uploaded to Palace, not just its URL.",
    };
  }

  if (isYouTubeUrl(parsedUrl) || isDirectMediaUrl(parsedUrl) || isVideoPlatformUrl(parsedUrl)) {
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
    case "image":
      return "Image";
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
