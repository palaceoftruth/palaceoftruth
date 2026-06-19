import assert from "node:assert/strict";
import test from "node:test";

import { classifyCapture } from "../dist/shared/classifier.js";

test("selected text overrides URL classification", () => {
  const result = classifyCapture({
    url: "https://youtu.be/abc123",
    selectionText: "Important quote",
  });
  assert.equal(result.kind, "selection_note");
  assert.equal(result.url, "https://youtu.be/abc123");
});

test("classifies YouTube URLs as media", () => {
  assert.equal(classifyCapture({ url: "https://www.youtube.com/watch?v=abc" }).kind, "media");
  assert.equal(classifyCapture({ url: "https://youtu.be/abc" }).kind, "media");
  assert.equal(classifyCapture({ url: "https://youtube.com/shorts/abc" }).kind, "media");
});

test("classifies direct audio and video files as media", () => {
  assert.equal(classifyCapture({ url: "https://example.com/audio.mp3" }).kind, "media");
  assert.equal(classifyCapture({ url: "https://cdn.example.com/video.webm" }).kind, "media");
});

test("classifies social post hosts", () => {
  const cases = [
    "https://x.com/user/status/1",
    "https://twitter.com/user/status/1",
    "https://bsky.app/profile/user/post/1",
    "https://threads.net/@user/post/1",
    "https://reddit.com/r/test/comments/abc/title/",
    "https://linkedin.com/posts/user_activity-123",
    "https://mastodon.social/@user/123456",
  ];
  for (const url of cases) {
    assert.equal(classifyCapture({ url }).kind, "social_post", url);
  }
});

test("classifies ordinary http URLs as webpage", () => {
  assert.equal(classifyCapture({ url: "https://example.com/article" }).kind, "webpage");
});

test("rejects invalid and unsupported URLs", () => {
  assert.equal(classifyCapture({ url: "not a url" }).kind, "invalid");
  assert.equal(classifyCapture({ url: "chrome://settings" }).kind, "invalid");
});
