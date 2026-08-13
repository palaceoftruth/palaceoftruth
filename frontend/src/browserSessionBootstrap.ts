import { api, clearLegacyBrowserApiKey, readLegacyBrowserApiKey } from "./api/client";
import type { BrowserSession } from "./api/client";

/**
 * Remove the legacy tenant API key before React renders. If no cookie session
 * exists, exchange the key once after it has been removed from localStorage.
 */
async function migrateLegacyBrowserSession(legacyKey: string): Promise<BrowserSession | null> {
  try {
    return await api.getBrowserSession();
  } catch {
    if (!legacyKey) return null;
  }

  try {
    return await api.createBrowserSession(legacyKey, false);
  } catch {
    return null;
  }
}

const legacyBrowserApiKey = readLegacyBrowserApiKey();
clearLegacyBrowserApiKey();

export const browserSessionMigrationPending = Boolean(legacyBrowserApiKey);
export const browserSessionBootstrap = legacyBrowserApiKey
  ? migrateLegacyBrowserSession(legacyBrowserApiKey)
  : Promise.resolve(null);
