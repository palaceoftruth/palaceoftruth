import assert from "node:assert/strict";
import test from "node:test";

import {
  decodeGrabbedImage,
  grabImageBytesInPage,
  MAX_UPLOAD_IMAGE_BYTES,
} from "../dist/shared/imageBytes.js";

const IMAGE_URL = "https://private.example.com/session-only.jpg";

function fakeImage(overrides = {}) {
  return {
    currentSrc: IMAGE_URL,
    src: IMAGE_URL,
    alt: "A gated image",
    naturalWidth: 800,
    naturalHeight: 600,
    ...overrides,
  };
}

/** Stands in for the page the injected function normally runs inside. */
function installPage({ images = [], canvasBlob = null, fetchImpl = null } = {}) {
  globalThis.document = {
    images,
    createElement() {
      return {
        width: 0,
        height: 0,
        getContext: () => ({ drawImage() {} }),
        toBlob(callback) {
          callback(canvasBlob);
        },
      };
    },
  };
  globalThis.fetch =
    fetchImpl ??
    (async () => {
      throw new Error("no network in this test");
    });
}

test.afterEach(() => {
  delete globalThis.document;
});

test("page fetch keeps the original bytes and the session that gated them", async () => {
  const calls = [];
  installPage({
    images: [fakeImage()],
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response(new Blob([new Uint8Array([1, 2, 3, 4])], { type: "image/jpeg" }), {
        status: 200,
      });
    },
  });

  const grabbed = await grabImageBytesInPage(IMAGE_URL, MAX_UPLOAD_IMAGE_BYTES);

  assert.equal(calls[0].url, IMAGE_URL);
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(grabbed.origin, "page_fetch");
  assert.equal(grabbed.mediaType, "image/jpeg");
  assert.equal(grabbed.byteSize, 4);
  assert.equal(grabbed.width, 800);
  assert.equal(grabbed.height, 600);
  assert.equal(grabbed.altText, "A gated image");
  assert.deepEqual([...decodeGrabbedImage(grabbed)], [1, 2, 3, 4]);
});

test("a blocked fetch falls back to re-encoding what the page rendered", async () => {
  installPage({
    images: [fakeImage()],
    canvasBlob: new Blob([new Uint8Array([9, 9, 9])], { type: "image/png" }),
  });

  const grabbed = await grabImageBytesInPage(IMAGE_URL, MAX_UPLOAD_IMAGE_BYTES);

  assert.equal(grabbed.origin, "canvas");
  assert.equal(grabbed.mediaType, "image/png");
  assert.equal(grabbed.byteSize, 3);
});

test("a tainted canvas and a blocked fetch leave nothing to upload", async () => {
  installPage({ images: [fakeImage()], canvasBlob: null });

  assert.equal(await grabImageBytesInPage(IMAGE_URL, MAX_UPLOAD_IMAGE_BYTES), null);
});

test("bytes over the upload ceiling are refused before the network round trip pays off", async () => {
  installPage({
    images: [fakeImage()],
    fetchImpl: async () =>
      new Response(new Blob([new Uint8Array(64)], { type: "image/png" }), { status: 200 }),
  });

  assert.equal(await grabImageBytesInPage(IMAGE_URL, 16), null);
});

test("an unsupported media type is not uploaded as an image", async () => {
  installPage({
    images: [fakeImage()],
    fetchImpl: async () =>
      new Response(new Blob([new Uint8Array([1, 2])], { type: "image/svg+xml" }), { status: 200 }),
  });

  assert.equal(await grabImageBytesInPage(IMAGE_URL, MAX_UPLOAD_IMAGE_BYTES), null);
});

test("an image the page never rendered has no canvas to fall back to", async () => {
  installPage({ images: [] });

  assert.equal(await grabImageBytesInPage(IMAGE_URL, MAX_UPLOAD_IMAGE_BYTES), null);
});
