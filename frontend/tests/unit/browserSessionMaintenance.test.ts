import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_TIMER_DELAY_MS,
  REFRESH_RETRY_DELAY_MS,
  REFRESH_THRESHOLD_MS,
  createBrowserSessionMaintenance,
} from "../../src/browserSessionMaintenance.ts";
import type { BrowserSession } from "../../src/api/client.ts";

const DAY_MS = 24 * 60 * 60 * 1_000;

function session(now: number, remainingMs: number): BrowserSession {
  return {
    tenant_id: "tenant-a",
    scopes: ["read", "write", "admin"],
    expires_at: new Date(now + remainingMs).toISOString(),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function harness(options: {
  reads: Array<BrowserSession | Error | Promise<BrowserSession>>;
  refreshes?: Array<BrowserSession | Error | Promise<BrowserSession>>;
  terminal?: (error: unknown) => boolean;
}) {
  let now = Date.parse("2026-08-14T12:00:00Z");
  let readCount = 0;
  let refreshCount = 0;
  const timers: Array<{ callback: () => void; delay: number; cleared: boolean }> = [];

  const resolveStep = async (step: BrowserSession | Error | Promise<BrowserSession>) => {
    if (step instanceof Error) throw step;
    return step;
  };

  const maintenance = createBrowserSessionMaintenance({
    readSession: async () => resolveStep(options.reads[readCount++]),
    refreshSession: async () => resolveStep((options.refreshes ?? [])[refreshCount++]),
    isTerminalError: options.terminal ?? (() => false),
    now: () => now,
    setTimer: (callback, delay) => {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (timer) => {
      (timer as (typeof timers)[number]).cleared = true;
    },
  });

  return {
    maintenance,
    timers,
    readCount: () => readCount,
    refreshCount: () => refreshCount,
    now: () => now,
    setNow: (value: number) => {
      now = value;
    },
  };
}

test("a restored 30-day session schedules renewal when seven days remain", async () => {
  const run = harness({ reads: [session(Date.parse("2026-08-14T12:00:00Z"), 30 * DAY_MS)] });

  await run.maintenance.start();

  assert.equal(run.readCount(), 1);
  assert.equal(run.refreshCount(), 0);
  assert.equal(run.timers.length, 1);
  assert.equal(run.timers[0].delay, 23 * DAY_MS);
});

test("a session inside the threshold refreshes and schedules the new expiry", async () => {
  const now = Date.parse("2026-08-14T12:00:00Z");
  const run = harness({
    reads: [session(now, 6 * DAY_MS)],
    refreshes: [session(now, 30 * DAY_MS)],
  });

  await run.maintenance.start();

  assert.equal(run.refreshCount(), 1);
  assert.equal(run.timers.length, 1);
  assert.equal(run.timers[0].delay, 23 * DAY_MS);
});

test("concurrent checks share one session request", async () => {
  const pending = deferred<BrowserSession>();
  const run = harness({ reads: [pending.promise] });

  const first = run.maintenance.check();
  const second = run.maintenance.check();

  assert.equal(first, second);
  assert.equal(run.readCount(), 1);
  pending.resolve(session(run.now(), 30 * DAY_MS));
  await first;
});

test("a refresh race accepts the session rotated by another tab", async () => {
  const now = Date.parse("2026-08-14T12:00:00Z");
  const terminal = new Error("old cookie was rotated");
  const run = harness({
    reads: [session(now, 6 * DAY_MS), session(now, 30 * DAY_MS)],
    refreshes: [terminal],
    terminal: (error) => error === terminal,
  });

  await run.maintenance.start();

  assert.equal(run.readCount(), 2);
  assert.equal(run.refreshCount(), 1);
  assert.equal(run.timers.length, 1);
  assert.equal(run.timers[0].delay, 23 * DAY_MS);
});

test("a temporary restore failure schedules a bounded retry", async () => {
  const run = harness({ reads: [new Error("network unavailable")] });

  await run.maintenance.start();

  assert.equal(run.timers.length, 1);
  assert.equal(run.timers[0].delay, REFRESH_RETRY_DELAY_MS);
});

test("an absent or revoked session stops without retrying", async () => {
  const terminal = new Error("session missing");
  const run = harness({
    reads: [terminal],
    terminal: (error) => error === terminal,
  });

  await run.maintenance.start();

  assert.equal(run.timers.length, 0);
});

test("the refresh threshold is seven days", () => {
  assert.equal(REFRESH_THRESHOLD_MS, 7 * DAY_MS);
});

test("a far-future expiry waits in timer-safe chunks instead of looping", async () => {
  // setTimeout truncates to int32. Before the clamp this delay wrapped
  // negative, the timer fired at once, and every fire re-read the session.
  const now = Date.parse("2026-08-14T12:00:00Z");
  const remaining = Date.parse("2099-08-07T12:10:00Z") - now;
  const run = harness({ reads: [session(now, remaining)] });

  await run.maintenance.start();

  assert.equal(run.readCount(), 1);
  assert.equal(run.timers.length, 1);
  assert.equal(run.timers[0].delay, MAX_TIMER_DELAY_MS);

  // Firing a chunk must resume the wait, not re-read the session.
  run.timers[0].callback();
  assert.equal(run.readCount(), 1);
  assert.equal(run.timers.length, 2);
  assert.equal(run.timers[1].delay, MAX_TIMER_DELAY_MS);
});

test("the largest schedulable timer delay fits in a signed 32-bit integer", () => {
  assert.equal(MAX_TIMER_DELAY_MS, 2_147_483_647);
  assert.equal(MAX_TIMER_DELAY_MS | 0, MAX_TIMER_DELAY_MS);
});
