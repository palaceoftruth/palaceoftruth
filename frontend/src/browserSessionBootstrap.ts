import { api, clearLegacyBrowserApiKey, readLegacyBrowserApiKey } from "./api/client";
import type { BrowserSession } from "./api/client";

/**
 * Remove the legacy tenant API key before React renders. If no cookie session
 * exists, exchange the key once after it has been removed from localStorage.
 */
async function migrateLegacyBrowserSession(): Promise<BrowserSession | null> {
  const legacyKey = readLegacyBrowserApiKey();
  clearLegacyBrowserApiKey();

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

export const browserSessionBootstrap = migrateLegacyBrowserSession();
