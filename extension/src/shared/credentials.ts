export type PalaceCredentials = {
  apiBaseUrl: string;
  accessToken: string;
  expiresAt?: string;
};

const API_BASE_URL_KEY = "palaceApiBaseUrl";
const CAPTURE_TOKEN_KEY = "palaceCaptureToken";
const CAPTURE_TOKEN_EXPIRES_AT_KEY = "palaceCaptureTokenExpiresAt";
const LEGACY_API_KEY_KEY = "palaceApiKey";

const CREDENTIAL_KEYS = [API_BASE_URL_KEY, CAPTURE_TOKEN_KEY, CAPTURE_TOKEN_EXPIRES_AT_KEY];
const ALL_KEYS = [...CREDENTIAL_KEYS, LEGACY_API_KEY_KEY];

/**
 * The capture token is a bearer credential, so it stays on this device.
 * `chrome.storage.sync` would replicate it to Google's servers and to every
 * browser signed into the same account; `chrome.storage.local` does not.
 */
function storageArea(): ChromeStorageArea | null {
  if (typeof chrome === "undefined" || !chrome.storage?.local) return null;
  return chrome.storage.local;
}

/** The area credentials used to live in. Read-only now, and drained on sight. */
function legacySyncArea(): ChromeStorageArea | null {
  if (typeof chrome === "undefined" || !chrome.storage?.sync) return null;
  return chrome.storage.sync;
}

function normalizeBaseUrl(value: unknown): string {
  const raw = typeof value === "string" && value.trim() ? value.trim() : "https://palaceoftruth.test";
  return raw.replace(/\/+$/, "");
}

/**
 * Move any credentials left in `chrome.storage.sync` into local storage and
 * delete the synced copies. Returns the local record after the migration.
 */
async function readWithSyncMigration(storage: ChromeStorageArea): Promise<Record<string, unknown>> {
  const local = await storage.get(CREDENTIAL_KEYS);
  const legacy = legacySyncArea();
  if (!legacy) return local;

  let synced: Record<string, unknown>;
  try {
    synced = await legacy.get(ALL_KEYS);
  } catch {
    // A sync area that cannot be read leaves local storage as the answer.
    return local;
  }
  if (!ALL_KEYS.some((key) => typeof synced[key] === "string" && synced[key])) {
    return local;
  }

  // Local values win: they are the newer, device-scoped copy.
  const merged: Record<string, unknown> = { ...local };
  for (const key of CREDENTIAL_KEYS) {
    if (typeof merged[key] === "string" && merged[key]) continue;
    if (typeof synced[key] === "string" && synced[key]) {
      merged[key] = synced[key];
    }
  }
  if (CREDENTIAL_KEYS.some((key) => merged[key] !== local[key])) {
    await storage.set(
      Object.fromEntries(CREDENTIAL_KEYS.filter((key) => key in merged).map((key) => [key, merged[key]])),
    );
  }
  // Revoke the off-device copy regardless of whether anything was adopted.
  await legacy.remove(ALL_KEYS);
  return merged;
}

export async function getCredentials(): Promise<PalaceCredentials | null> {
  const storage = storageArea();
  if (!storage) return null;
  const stored = await readWithSyncMigration(storage);
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
  await legacySyncArea()?.remove(ALL_KEYS);
}

export async function clearCredentials(): Promise<void> {
  const storage = storageArea();
  await storage?.remove(ALL_KEYS);
  await legacySyncArea()?.remove(ALL_KEYS);
}
