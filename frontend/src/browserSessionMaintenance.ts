import type { BrowserSession } from "./api/client";

const DAY_MS = 24 * 60 * 60 * 1_000;

export const REFRESH_THRESHOLD_MS = 7 * DAY_MS;
export const REFRESH_RETRY_DELAY_MS = 5 * 60 * 1_000;

// `setTimeout` truncates its delay to a signed 32-bit integer. A longer delay
// wraps, and once it wraps negative the timer fires at once, so a session with
// a far-future expiry would re-check in a hot loop instead of waiting. Wait in
// chunks no larger than the maximum a timer can represent.
export const MAX_TIMER_DELAY_MS = 2_147_483_647;

interface BrowserSessionMaintenanceDependencies {
  readSession: () => Promise<BrowserSession>;
  refreshSession: () => Promise<BrowserSession>;
  isTerminalError: (error: unknown) => boolean;
  now: () => number;
  setTimer: (callback: () => void, delay: number) => unknown;
  clearTimer: (timer: unknown) => void;
}

export interface BrowserSessionMaintenance {
  start: () => Promise<BrowserSession | null>;
  check: () => Promise<BrowserSession | null>;
  stop: () => void;
}

/**
 * Maintain one rolling browser session without retaining the API key.
 *
 * The controller has no direct browser dependencies so its timing, retry, and
 * multi-tab recovery behavior can be tested deterministically.
 */
export function createBrowserSessionMaintenance(
  dependencies: BrowserSessionMaintenanceDependencies,
): BrowserSessionMaintenance {
  let timer: unknown;
  let inFlight: Promise<BrowserSession | null> | null = null;
  let disposed = false;

  const clearScheduledCheck = () => {
    if (timer !== undefined) {
      dependencies.clearTimer(timer);
      timer = undefined;
    }
  };

  const schedule = (delay: number, check: () => Promise<BrowserSession | null>) => {
    if (disposed) return;
    clearScheduledCheck();
    const remainingDelay = Math.max(0, delay);
    const chunk = Math.min(remainingDelay, MAX_TIMER_DELAY_MS);
    timer = dependencies.setTimer(() => {
      timer = undefined;
      if (remainingDelay > chunk) {
        schedule(remainingDelay - chunk, check);
        return;
      }
      void check();
    }, chunk);
  };

  const remainingLifetime = (session: BrowserSession) => {
    const expiresAt = Date.parse(session.expires_at);
    return Number.isFinite(expiresAt) ? expiresAt - dependencies.now() : Number.NaN;
  };

  const scheduleFromSession = (
    session: BrowserSession,
    check: () => Promise<BrowserSession | null>,
  ) => {
    const remaining = remainingLifetime(session);
    if (!Number.isFinite(remaining) || remaining <= 0) return;
    if (remaining <= REFRESH_THRESHOLD_MS) {
      schedule(REFRESH_RETRY_DELAY_MS, check);
      return;
    }
    schedule(remaining - REFRESH_THRESHOLD_MS, check);
  };

  const performCheck = async (
    check: () => Promise<BrowserSession | null>,
  ): Promise<BrowserSession | null> => {
    let current: BrowserSession;
    try {
      current = await dependencies.readSession();
    } catch (error) {
      if (!dependencies.isTerminalError(error)) {
        schedule(REFRESH_RETRY_DELAY_MS, check);
      }
      return null;
    }

    const remaining = remainingLifetime(current);
    if (!Number.isFinite(remaining) || remaining <= 0) return null;
    if (remaining > REFRESH_THRESHOLD_MS) {
      scheduleFromSession(current, check);
      return current;
    }

    try {
      const refreshed = await dependencies.refreshSession();
      scheduleFromSession(refreshed, check);
      return refreshed;
    } catch (refreshError) {
      // A different tab may have rotated the shared cookie between our read
      // and refresh. Read it back once before treating the session as lost.
      try {
        const recovered = await dependencies.readSession();
        scheduleFromSession(recovered, check);
        return recovered;
      } catch (readbackError) {
        if (!dependencies.isTerminalError(refreshError) || !dependencies.isTerminalError(readbackError)) {
          schedule(REFRESH_RETRY_DELAY_MS, check);
        }
        return null;
      }
    }
  };

  const check = (): Promise<BrowserSession | null> => {
    if (disposed) return Promise.resolve(null);
    if (inFlight) return inFlight;
    clearScheduledCheck();
    inFlight = performCheck(check).finally(() => {
      inFlight = null;
    });
    return inFlight;
  };

  return {
    start: check,
    check,
    stop: () => {
      disposed = true;
      clearScheduledCheck();
    },
  };
}
