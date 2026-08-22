import assert from "node:assert/strict";
import test from "node:test";

import { classifyCapture } from "../dist/shared/classifier.js";
import {
  issueExtensionToken,
  lookupWebSavesForUrl,
  submitCapture,
  uploadCaptureImage,
} from "../dist/shared/palaceClient.js";

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

test("one-time pairing key is exchanged for a scoped capture token", async () => {
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
  assert.equal(calls[0].init.headers["X-Palace-Pairing-Key"], "pairing-key");
  assert.equal(calls[0].init.headers["X-API-Key"], undefined);
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

test("image bytes are uploaded as multipart, not as a URL to fetch", async () => {
  const calls = [];
  const result = await uploadCaptureImage(
    credentials,
    {
      bytes: new Uint8Array([137, 80, 78, 71]),
      mediaType: "image/png",
      byteOrigin: "page_fetch",
      imageUrl: "https://private.example.com/session-only.png",
      sourceUrl: "https://private.example.com/album/7",
      pageTitle: "Album 7",
      altText: "A gated image",
      width: 800,
      height: 600,
      tags: ["private", "private", " "],
    },
    async (url, init) => {
      calls.push({ url, init });
      return jsonResponse(202, {
        job_id: "job-image",
        item_id: "item-image",
        status: "queued",
        parent_item_id: null,
      });
    },
  );

  assert.equal(result.state, "queued");
  assert.equal(result.itemId, "item-image");
  assert.equal(result.jobId, "job-image");
  assert.equal(calls[0].url, "https://palaceoftruth.test/api/v1/capture/browser/images");
  assert.equal(calls[0].init.headers.Authorization, "Bearer capture-token");
  // fetch has to supply the multipart boundary, so no Content-Type is set.
  assert.equal(calls[0].init.headers["Content-Type"], undefined);

  const form = calls[0].init.body;
  const file = form.get("file");
  assert.equal(file.type, "image/png");
  assert.equal(file.name, "capture.png");
  assert.deepEqual([...new Uint8Array(await file.arrayBuffer())], [137, 80, 78, 71]);
  assert.equal(form.get("origin"), "page_fetch");
  assert.equal(form.get("image_url"), "https://private.example.com/session-only.png");
  assert.equal(form.get("source_url"), "https://private.example.com/album/7");
  assert.equal(form.get("page_title"), "Album 7");
  assert.equal(form.get("alt_text"), "A gated image");
  assert.equal(form.get("width"), "800");
  assert.equal(form.get("height"), "600");
  assert.equal(form.get("tags"), "private");
  assert.equal(form.get("item_id"), null);
});

test("an image attached to an existing capture carries its parent item id", async () => {
  const calls = [];
  const result = await uploadCaptureImage(
    credentials,
    {
      bytes: new Uint8Array([255, 216, 255]),
      mediaType: "image/jpeg",
      byteOrigin: "canvas",
      itemId: "item-parent",
      imageUrl: "https://pbs.twimg.com/media/post-image.jpg",
      order: 2,
      role: "img",
    },
    async (url, init) => {
      calls.push({ url, init });
      return jsonResponse(202, {
        job_id: "job-child",
        item_id: "item-child",
        status: "queued",
        parent_item_id: "item-parent",
      });
    },
  );

  assert.equal(result.state, "queued");
  assert.equal(result.parentItemId, "item-parent");
  const form = calls[0].init.body;
  assert.equal(form.get("item_id"), "item-parent");
  assert.equal(form.get("origin"), "canvas");
  assert.equal(form.get("order"), "2");
  assert.equal(form.get("role"), "img");
  assert.equal(form.get("file").name, "capture.jpg");
});

test("repeated bytes report as a duplicate, not a failure", async () => {
  const result = await uploadCaptureImage(
    credentials,
    { bytes: new Uint8Array([1]), mediaType: "image/png", byteOrigin: "context_menu" },
    async () => jsonResponse(202, { item_id: "item-image", status: "duplicate", duplicate_of: "item-image" }),
  );

  assert.equal(result.state, "duplicate");
  assert.equal(result.itemId, "item-image");
});

test("an image upload with an expired token returns auth_error", async () => {
  const result = await uploadCaptureImage(
    credentials,
    { bytes: new Uint8Array([1]), mediaType: "image/png", byteOrigin: "page_fetch" },
    async () => jsonResponse(403, { detail: "Capture token expired." }),
  );

  assert.equal(result.state, "auth_error");
  assert.equal(result.message, "Capture token expired.");
});

test("an image upload network failure returns error state", async () => {
  const result = await uploadCaptureImage(
    credentials,
    { bytes: new Uint8Array([1]), mediaType: "image/png", byteOrigin: "page_fetch" },
    async () => {
      throw new Error("Failed to fetch");
    },
  );

  assert.equal(result.state, "error");
  assert.equal(result.message, "Failed to fetch");
});
