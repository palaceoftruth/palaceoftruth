import { api, clearLegacyBrowserApiKey, readLegacyBrowserApiKey } from "./api/client";
import type { BrowserSession } from "./api/client";

const LEGACY_SESSION_MIGRATION_TIMEOUT_MS = 2_000;

/**
 * Remove the legacy tenant API key before React renders. If no cookie session
 * exists, exchange the key once after it has been removed from localStorage.
 */
async function migrateLegacyBrowserSession(legacyKey: string): Promise<BrowserSession | null> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), LEGACY_SESSION_MIGRATION_TIMEOUT_MS);
  try {
    try {
      return await api.getBrowserSession(controller.signal);
    } catch {
      if (controller.signal.aborted) return null;
    }

    try {
      return await api.createBrowserSession(legacyKey, false, controller.signal);
    } catch {
      return null;
    }
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
}

const legacyBrowserApiKey = readLegacyBrowserApiKey();
clearLegacyBrowserApiKey();

export const browserSessionMigrationPending = Boolean(legacyBrowserApiKey);
export const browserSessionBootstrap = legacyBrowserApiKey
  ? migrateLegacyBrowserSession(legacyBrowserApiKey)
  : Promise.resolve(null);
