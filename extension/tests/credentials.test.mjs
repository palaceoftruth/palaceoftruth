import assert from "node:assert/strict";
import test from "node:test";

import { clearCredentials, getCredentials, saveCredentials } from "../dist/shared/credentials.js";

function fakeArea(initial = {}) {
  const data = { ...initial };
  return {
    data,
    async get(keys) {
      if (!keys) return { ...data };
      const wanted = Array.isArray(keys) ? keys : [keys];
      return Object.fromEntries(wanted.filter((key) => key in data).map((key) => [key, data[key]]));
    },
    async set(items) {
      Object.assign(data, items);
    },
    async remove(keys) {
      for (const key of Array.isArray(keys) ? keys : [keys]) delete data[key];
    },
  };
}

function installChrome(localInitial, syncInitial) {
  const local = fakeArea(localInitial);
  const sync = fakeArea(syncInitial);
  globalThis.chrome = { storage: { local, sync } };
  return { local, sync };
}

test("credentials are written to local storage, never to sync", async () => {
  const { local, sync } = installChrome({}, {});

  await saveCredentials({
    apiBaseUrl: "https://api.palaceoftruth.test/",
    accessToken: " capture-token ",
    expiresAt: "2026-01-01T00:00:00Z",
  });

  assert.equal(local.data.palaceCaptureToken, "capture-token");
  assert.equal(local.data.palaceApiBaseUrl, "https://api.palaceoftruth.test");
  assert.deepEqual(sync.data, {});
});

test("a token left in sync storage is adopted locally and deleted from sync", async () => {
  const { local, sync } = installChrome(
    {},
    {
      palaceApiBaseUrl: "https://api.palaceoftruth.test",
      palaceCaptureToken: "synced-token",
      palaceCaptureTokenExpiresAt: "2026-01-01T00:00:00Z",
      palaceApiKey: "legacy-key",
    },
  );

  const credentials = await getCredentials();

  assert.equal(credentials.accessToken, "synced-token");
  assert.equal(credentials.apiBaseUrl, "https://api.palaceoftruth.test");
  assert.equal(local.data.palaceCaptureToken, "synced-token");
  assert.deepEqual(sync.data, {}, "the off-device copy must not survive the migration");
});

test("a local token wins over a stale synced one, which is still purged", async () => {
  const { local, sync } = installChrome(
    { palaceCaptureToken: "local-token", palaceApiBaseUrl: "https://api.palaceoftruth.test" },
    { palaceCaptureToken: "stale-synced-token" },
  );

  const credentials = await getCredentials();

  assert.equal(credentials.accessToken, "local-token");
  assert.equal(local.data.palaceCaptureToken, "local-token");
  assert.deepEqual(sync.data, {});
});

test("clearing credentials empties both areas", async () => {
  const { local, sync } = installChrome(
    { palaceCaptureToken: "local-token" },
    { palaceCaptureToken: "synced-token" },
  );

  await clearCredentials();

  assert.deepEqual(local.data, {});
  assert.deepEqual(sync.data, {});
  assert.equal(await getCredentials(), null);
});
