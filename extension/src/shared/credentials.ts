export type PalaceCredentials = {
  apiBaseUrl: string;
  accessToken: string;
  expiresAt?: string;
};

const API_BASE_URL_KEY = "palaceApiBaseUrl";
const CAPTURE_TOKEN_KEY = "palaceCaptureToken";
const CAPTURE_TOKEN_EXPIRES_AT_KEY = "palaceCaptureTokenExpiresAt";
const LEGACY_API_KEY_KEY = "palaceApiKey";

function storageArea(): ChromeStorageArea | null {
  if (typeof chrome === "undefined" || !chrome.storage?.sync) return null;
  return chrome.storage.sync;
}

function normalizeBaseUrl(value: unknown): string {
  const raw = typeof value === "string" && value.trim() ? value.trim() : "https://palaceoftruth.test";
  return raw.replace(/\/+$/, "");
}

export async function getCredentials(): Promise<PalaceCredentials | null> {
  const storage = storageArea();
  if (!storage) return null;
  const stored = await storage.get([API_BASE_URL_KEY, CAPTURE_TOKEN_KEY, CAPTURE_TOKEN_EXPIRES_AT_KEY]);
  const accessToken = typeof stored[CAPTURE_TOKEN_KEY] === "string" ? stored[CAPTURE_TOKEN_KEY].trim() : "";
  if (!accessToken) return null;
  return {
    apiBaseUrl: normalizeBaseUrl(stored[API_BASE_URL_KEY]),
    accessToken,
    expiresAt:
      typeof stored[CAPTURE_TOKEN_EXPIRES_AT_KEY] === "string" ? stored[CAPTURE_TOKEN_EXPIRES_AT_KEY] : undefined,
  };
}

export async function saveCredentials(credentials: PalaceCredentials): Promise<void> {
  const storage = storageArea();
  if (!storage) throw new Error("Chrome storage is unavailable.");
  await storage.set({
    [API_BASE_URL_KEY]: normalizeBaseUrl(credentials.apiBaseUrl),
    [CAPTURE_TOKEN_KEY]: credentials.accessToken.trim(),
    [CAPTURE_TOKEN_EXPIRES_AT_KEY]: credentials.expiresAt ?? "",
  });
  await storage.remove([LEGACY_API_KEY_KEY]);
}

export async function clearCredentials(): Promise<void> {
  const storage = storageArea();
  if (!storage) return;
  await storage.remove([API_BASE_URL_KEY, CAPTURE_TOKEN_KEY, CAPTURE_TOKEN_EXPIRES_AT_KEY, LEGACY_API_KEY_KEY]);
}
