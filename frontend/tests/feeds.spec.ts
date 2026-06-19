import { expect, test } from "@playwright/test";

const BASE_URL = process.env.PALACE_FRONTEND_BASE_URL ?? "http://127.0.0.1:4173";

const feed = {
  id: "11111111-1111-1111-1111-111111111111",
  url: "https://example.com/feed.xml",
  name: "Founders Feed",
  auto_tags: ["founders"],
  poll_interval: 900,
  enabled: true,
  paused_reason: null,
  last_fetched_at: "2026-04-20T11:30:00Z",
  last_error: null,
  consecutive_failures: 0,
  feed_metadata: {
    feed_title: "Founders Feed",
    description: "Signal for the palace.",
  },
  item_count: 15,
  created_at: "2026-04-20T10:00:00Z",
  updated_at: "2026-04-20T11:30:00Z",
};

test.describe("Feeds route", () => {
  test("requests paginated feed items with limit/offset and surfaces OPML import counts", async ({ page }) => {
    const feedItemsRequests: string[] = [];

    await page.route("**/api/v1/feeds", async (route) => {
      if (route.request().method() === "GET") {
        await route.fulfill({
          json: {
            feeds: [feed],
            total: 1,
          },
        });
        return;
      }

      await route.fulfill({ status: 405, body: "unexpected method" });
    });

    await page.route(`**/api/v1/feeds/${feed.id}/items?*`, async (route) => {
      const url = new URL(route.request().url());
      feedItemsRequests.push(url.search);

      const limit = Number(url.searchParams.get("limit"));
      const offset = Number(url.searchParams.get("offset"));
      const items = Array.from({ length: Math.min(limit, 15 - offset) }, (_, index) => ({
        id: `item-${offset + index + 1}`,
        tenant_id: "tenant-a",
        title: `Feed item ${offset + index + 1}`,
        source_type: "feed_article",
        summary: `Summary ${offset + index + 1}`,
        raw_content: `Body ${offset + index + 1}`,
        source_url: `https://example.com/posts/${offset + index + 1}`,
        metadata: { feed_id: feed.id },
        tags: [],
        categories: [],
        status: "ready",
        created_at: `2026-04-20T10:${String(offset + index).padStart(2, "0")}:00Z`,
        updated_at: `2026-04-20T10:${String(offset + index).padStart(2, "0")}:00Z`,
      }));

      await route.fulfill({
        json: {
          items,
          total: 15,
        },
      });
    });

    await page.route("**/api/v1/feeds/import_opml", async (route) => {
      await route.fulfill({
        json: {
          created: 1,
          skipped: 2,
          feeds: [feed],
        },
      });
    });

    await page.goto(`${BASE_URL}/feeds?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Feeds", exact: true })).toBeVisible();
    await expect(page.getByText("Founders Feed")).toBeVisible();
    await page.getByRole("button", { name: "Open details" }).click();

    await expect.poll(() => feedItemsRequests).toEqual(["?limit=10&offset=0"]);
    await expect(page.getByRole("button", { name: /^Feed item 1\b/ })).toBeVisible();

    await page.getByRole("button", { name: /Next/i }).click();

    await expect.poll(() => feedItemsRequests).toEqual([
      "?limit=10&offset=0",
      "?limit=10&offset=10",
    ]);
    await expect(page.getByRole("button", { name: /^Feed item 11\b/ })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Feed item 1\b/ })).toHaveCount(0);

    await page.getByRole("button", { name: "Back" }).click();
    await page.setInputFiles('input[type="file"]', {
      name: "feeds.opml",
      mimeType: "text/xml",
      buffer: Buffer.from(
        '<?xml version="1.0"?><opml><body><outline type="rss" xmlUrl="https://example.com/new.xml" /></body></opml>',
      ),
    });

    await expect(page.getByText("Imported 1 feed • 2 already existed")).toBeVisible();
  });
});
