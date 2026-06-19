import assert from "node:assert/strict";
import test from "node:test";

import { classifyCapture } from "../dist/shared/classifier.js";
import { issueExtensionToken, lookupWebSavesForUrl, submitCapture } from "../dist/shared/palaceClient.js";

const credentials = {
  apiBaseUrl: "https://palaceoftruth.test",
  accessToken: "capture-token",
};

function jsonResponse(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("media capture posts to browser capture API", async () => {
  const calls = [];
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://youtu.be/abc" }), tags: ["video"] },
    async (url, init) => {
      calls.push({ url, init });
      return jsonResponse(202, { job_id: "job-media", route: "media", kind: "media", status: "queued" });
    },
  );

  assert.equal(result.state, "queued");
  assert.equal(result.jobId, "job-media");
  assert.equal(calls[0].url, "https://palaceoftruth.test/api/v1/capture/browser");
  assert.equal(calls[0].init.headers.Authorization, "Bearer capture-token");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    url: "https://youtu.be/abc",
    page_title: null,
    selection_text: null,
    tags: ["video"],
    detected_kind: "media",
    image_candidates: [],
    browser_extension_version: "0.1.0",
    extension_metadata: {
      classifier_reason: "Media URLs are queued through the audio/video ingest path.",
    },
  });
});

test("social post capture posts to browser capture API", async () => {
  const calls = [];
  const imageCandidates = [
    {
      url: "https://pbs.twimg.com/media/post-image.jpg",
      source_post_url: "https://x.com/user/status/1",
      alt_text: "Post image",
      width: 1200,
      height: 900,
      role: "img",
      order: 0,
    },
  ];
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://x.com/user/status/1" }), imageCandidates },
    async (url, init) => {
      calls.push({ url, init });
      return jsonResponse(202, { job_id: "job-social", route: "webpage", kind: "social_post", status: "queued" });
    },
  );

  assert.equal(result.state, "queued");
  assert.equal(result.routedTo, "webpage");
  assert.equal(calls[0].url, "https://palaceoftruth.test/api/v1/capture/browser");
  assert.deepEqual(JSON.parse(calls[0].init.body).image_candidates, imageCandidates);
});

test("selection capture posts a provenance-preserving note", async () => {
  const calls = [];
  const result = await submitCapture(
    credentials,
    {
      classification: classifyCapture({
        url: "https://example.com/source",
        selectionText: "Selected passage",
      }),
      pageTitle: "Source title",
      selectionText: "Selected passage",
      tags: [" research ", "research", "quote"],
    },
    async (url, init) => {
      calls.push({ url, init });
      return jsonResponse(202, { job_id: "job-note", route: "note", kind: "selection_note", status: "queued" });
    },
  );

  assert.equal(result.state, "queued");
  assert.equal(calls[0].url, "https://palaceoftruth.test/api/v1/capture/browser");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    url: "https://example.com/source",
    page_title: "Source title",
    selection_text: "Selected passage",
    tags: ["research", "quote"],
    detected_kind: "selection_note",
    image_candidates: [],
    browser_extension_version: "0.1.0",
    extension_metadata: {
      classifier_reason: "Selected text is saved as a note with source provenance.",
    },
  });
});

test("403 returns auth_error", async () => {
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://example.com" }) },
    async () => jsonResponse(403, { detail: "Invalid or revoked API key" }),
  );
  assert.deepEqual(result, {
    state: "auth_error",
    message: "Invalid or revoked API key",
  });
});

test("pairing API key is exchanged for a scoped capture token", async () => {
  const calls = [];
  const result = await issueExtensionToken("https://palaceoftruth.test/", "pairing-key", "0.1.0", async (url, init) => {
    calls.push({ url, init });
    return jsonResponse(201, {
      access_token: "scoped-token",
      expires_at: "2026-06-01T00:00:00Z",
      expires_in: 2592000,
    });
  });

  assert.deepEqual(result, {
    apiBaseUrl: "https://palaceoftruth.test",
    accessToken: "scoped-token",
    expiresAt: "2026-06-01T00:00:00Z",
  });
  assert.equal(calls[0].url, "https://palaceoftruth.test/api/v1/palace/browser-extension-tokens");
  assert.equal(calls[0].init.headers["X-API-Key"], "pairing-key");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    display_name: "Palace Capture Extension",
    extension_version: "0.1.0",
  });
});

test("expired or revoked capture tokens surface settings-ready auth errors", async () => {
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://example.com" }) },
    async () => jsonResponse(403, { detail: "extension bearer token expired" }),
  );
  assert.deepEqual(result, {
    state: "auth_error",
    message: "extension bearer token expired",
  });
});

test("409 returns duplicate", async () => {
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://example.com" }) },
    async () => jsonResponse(409, { detail: "URL already ingested" }),
  );
  assert.deepEqual(result, {
    state: "duplicate",
    message: "URL already ingested",
  });
});

test("202 duplicate no-op returns duplicate", async () => {
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://example.com" }) },
    async () => jsonResponse(202, { status: "duplicate", duplicate_of: "item-1", web_save_id: "save-1" }),
  );
  assert.deepEqual(result, {
    state: "duplicate",
    message: "This URL is already saved in Palace.",
    webSaveId: "save-1",
    itemId: "item-1",
  });
});

test("lookupWebSavesForUrl checks exact saved state and related domain saves", async () => {
  const calls = [];
  const result = await lookupWebSavesForUrl(credentials, "https://Example.com/story?x=1#section", async (url, init) => {
    calls.push({ url, init });
    if (url.includes("q=https%3A%2F%2Fexample.com%2Fstory%3Fx%3D1")) {
      return jsonResponse(200, {
        web_saves: [
          {
            id: "save-current",
            item_id: "item-current",
            original_url: "https://example.com/story?x=1",
            normalized_url: "https://example.com/story?x=1",
            source_title: "Current story",
            source_domain: "example.com",
            capture_kind: "webpage",
            user_tags: ["saved"],
            saved_at: "2026-05-12T12:00:00Z",
            archived_at: null,
            item: {
              id: "item-current",
              title: "Current story",
              source_type: "webpage",
              status: "ready",
              summary: null,
              tags: [],
            },
          },
        ],
      });
    }
    return jsonResponse(200, {
      web_saves: [
        {
          id: "save-current",
          item_id: "item-current",
          original_url: "https://example.com/story?x=1",
          normalized_url: "https://example.com/story?x=1",
          source_title: "Current story",
          source_domain: "example.com",
          capture_kind: "webpage",
          user_tags: ["saved"],
          saved_at: "2026-05-12T12:00:00Z",
          archived_at: null,
          item: { id: "item-current", title: "Current story", source_type: "webpage", status: "ready", summary: null, tags: [] },
        },
        {
          id: "save-related",
          item_id: "item-related",
          original_url: "https://example.com/related",
          normalized_url: "https://example.com/related",
          source_title: "Related brief",
          source_domain: "example.com",
          capture_kind: "webpage",
          user_tags: [],
          saved_at: "2026-05-11T12:00:00Z",
          archived_at: null,
          item: { id: "item-related", title: "Related brief", source_type: "webpage", status: "ready", summary: null, tags: [] },
        },
      ],
    });
  });

  assert.equal(result.state, "ready");
  assert.equal(result.saved.id, "save-current");
  assert.deepEqual(result.related.map((save) => save.id), ["save-related"]);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].init.headers.Authorization, "Bearer capture-token");
  assert.ok(calls[0].url.includes("/api/v1/web-saves?"));
});

test("lookupWebSavesForUrl returns auth_error for expired capture token", async () => {
  const result = await lookupWebSavesForUrl(credentials, "https://example.com", async () =>
    jsonResponse(403, { detail: "extension bearer token expired" }),
  );
  assert.deepEqual(result, {
    state: "auth_error",
    message: "extension bearer token expired",
  });
});

test("network failures return error state", async () => {
  const result = await submitCapture(
    credentials,
    { classification: classifyCapture({ url: "https://example.com" }) },
    async () => {
      throw new Error("offline");
    },
  );
  assert.deepEqual(result, {
    state: "error",
    message: "offline",
  });
});
