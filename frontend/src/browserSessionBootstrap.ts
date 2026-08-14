import { ApiError, api, clearLegacyBrowserApiKey, readLegacyBrowserApiKey } from "./api/client";
import type { BrowserSession } from "./api/client";
import { createBrowserSessionMaintenance } from "./browserSessionMaintenance";

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

let activeMaintenance: ReturnType<typeof createBrowserSessionMaintenance> | null = null;

export function checkBrowserSessionMaintenance(): void {
  void activeMaintenance?.check();
}

export function startBrowserSessionMaintenance(): () => void {
  const maintenance = createBrowserSessionMaintenance({
    readSession: () => api.getBrowserSession(),
    refreshSession: () => api.refreshBrowserSession(),
    isTerminalError: (error) => error instanceof ApiError && (error.status === 401 || error.status === 403),
    now: () => Date.now(),
    setTimer: (callback, delay) => window.setTimeout(callback, delay),
    clearTimer: (timer) => window.clearTimeout(timer as number),
  });
  activeMaintenance?.stop();
  activeMaintenance = maintenance;

  const check = () => checkBrowserSessionMaintenance();
  const checkWhenVisible = () => {
    if (document.visibilityState === "visible") check();
  };
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", checkWhenVisible);
  void maintenance.start();

  return () => {
    window.removeEventListener("focus", check);
    document.removeEventListener("visibilitychange", checkWhenVisible);
    maintenance.stop();
    if (activeMaintenance === maintenance) activeMaintenance = null;
  };
}
