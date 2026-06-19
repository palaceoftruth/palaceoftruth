import assert from "node:assert/strict";
import test from "node:test";

import { extractXPostImageCandidates } from "../dist/shared/imageCandidates.js";

const originalDocument = globalThis.document;
const originalLocation = globalThis.location;

function fakeImage({
  alt = "",
  currentSrc,
  height = 640,
  role = null,
  src,
  testId = null,
  width = 960,
}) {
  return {
    alt,
    className: "",
    currentSrc,
    height,
    naturalHeight: height,
    naturalWidth: width,
    src: src ?? currentSrc,
    width,
    closest(selector) {
      if (selector === "[role]" && role) return { getAttribute: () => role };
      if (selector === "[data-testid]" && testId) return { getAttribute: () => testId };
      return null;
    },
    getAttribute(name) {
      if (name === "role") return role;
      if (name === "data-testid") return testId;
      return null;
    },
    getBoundingClientRect() {
      return { width, height };
    },
  };
}

function fakeArticle({ links = [], images = [] }) {
  return {
    querySelectorAll(selector) {
      if (selector === 'a[href*="/status/"]') {
        return links.map((href) => ({ href }));
      }
      if (selector === "img") return images;
      return [];
    },
  };
}

function installDocument({ articles = [], href = "https://x.com/example/status/123" }) {
  globalThis.location = { href };
  globalThis.document = {
    querySelectorAll(selector) {
      if (selector === 'article, [role="article"]') return articles;
      if (selector === "img") return articles.flatMap((article) => article.querySelectorAll("img"));
      return [];
    },
  };
}

test.afterEach(() => {
  globalThis.document = originalDocument;
  globalThis.location = originalLocation;
});

test("extracts focused X post image candidates with metadata and order", () => {
  const article = fakeArticle({
    links: ["https://x.com/example/status/123"],
    images: [
      fakeImage({
        alt: "Launch diagram",
        currentSrc: "https://pbs.twimg.com/media/post-a.jpg?format=jpg&name=large",
        height: 720,
        role: "img",
        width: 1280,
      }),
      fakeImage({
        alt: "Second slide",
        currentSrc: "https://pbs.twimg.com/media/post-b.jpg?format=jpg&name=large",
        height: 900,
        role: "img",
        width: 900,
      }),
    ],
  });
  installDocument({ articles: [article] });

  const candidates = extractXPostImageCandidates("https://x.com/example/status/123");

  assert.deepEqual(candidates, [
    {
      url: "https://pbs.twimg.com/media/post-a.jpg?format=jpg&name=large",
      source_post_url: "https://x.com/example/status/123",
      alt_text: "Launch diagram",
      width: 1280,
      height: 720,
      role: "img",
      order: 0,
    },
    {
      url: "https://pbs.twimg.com/media/post-b.jpg?format=jpg&name=large",
      source_post_url: "https://x.com/example/status/123",
      alt_text: "Second slide",
      width: 900,
      height: 900,
      role: "img",
      order: 1,
    },
  ]);
});

test("dedupes candidates and excludes avatars, emoji, pixels, data urls, and tiny images", () => {
  const validUrl = "https://pbs.twimg.com/media/post-a.jpg?format=jpg&name=large";
  const article = fakeArticle({
    links: ["https://x.com/example/status/123"],
    images: [
      fakeImage({ alt: "Avatar", currentSrc: "https://pbs.twimg.com/profile_images/avatar.jpg" }),
      fakeImage({ alt: "emoji", currentSrc: "https://abs.twimg.com/emoji/v2/72x72/1f44d.png" }),
      fakeImage({ alt: "pixel", currentSrc: "https://analytics.example.com/pixel.gif", width: 1, height: 1 }),
      fakeImage({ alt: "inline", currentSrc: "data:image/png;base64,abc" }),
      fakeImage({ alt: "too small", currentSrc: "https://pbs.twimg.com/media/tiny.jpg", width: 32, height: 32 }),
      fakeImage({ alt: "Post image", currentSrc: validUrl }),
      fakeImage({ alt: "Duplicate post image", currentSrc: validUrl }),
    ],
  });
  installDocument({ articles: [article] });

  const candidates = extractXPostImageCandidates("https://x.com/example/status/123");

  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].url, validUrl);
  assert.equal(candidates[0].alt_text, "Post image");
});

test("returns no candidates on ordinary webpages", () => {
  const article = fakeArticle({
    images: [fakeImage({ alt: "Hero", currentSrc: "https://example.com/hero.jpg" })],
  });
  installDocument({ articles: [article], href: "https://example.com/article" });

  assert.deepEqual(extractXPostImageCandidates("https://example.com/article"), []);
});

test("returns no candidates on non-status X pages", () => {
  const article = fakeArticle({
    images: [fakeImage({ alt: "Timeline image", currentSrc: "https://pbs.twimg.com/media/feed.jpg" })],
  });
  installDocument({ articles: [article], href: "https://x.com/home" });

  assert.deepEqual(extractXPostImageCandidates("https://x.com/home"), []);
});

test("caps extracted candidates at the backend image_candidates limit", () => {
  const article = fakeArticle({
    links: ["https://x.com/example/status/123"],
    images: Array.from({ length: 6 }, (_, index) =>
      fakeImage({
        alt: `Image ${index}`,
        currentSrc: `https://pbs.twimg.com/media/post-${index}.jpg`,
      }),
    ),
  });
  installDocument({ articles: [article] });

  const candidates = extractXPostImageCandidates("https://x.com/example/status/123");

  assert.equal(candidates.length, 4);
  assert.deepEqual(
    candidates.map((candidate) => candidate.order),
    [0, 1, 2, 3],
  );
});
