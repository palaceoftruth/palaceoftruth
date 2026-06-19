import { expect, test } from "@playwright/test";

const BASE_URL = process.env.PALACE_FRONTEND_BASE_URL ?? "http://127.0.0.1:4173";

const capturedItem = {
  id: "22222222-2222-4222-8222-222222222222",
  source_type: "note",
  source_url: null,
  title: "Market positioning memo",
  summary: "A concise capture about the launch wedge and buyer urgency.",
  raw_content: "Position around urgent recall for teams that need sourced answers.",
  metadata: {},
  tags: ["launch", "positioning"],
  categories: ["strategy"],
  status: "ready",
  created_at: "2026-04-24T13:45:00Z",
  updated_at: "2026-04-24T13:45:00Z",
};

test.describe("Capture-to-recall journey", () => {
  test("captures a note, opens the completed item, and recalls it through search", async ({ page }) => {
    const ingestRequests: Array<{ title: string; content: string; tags?: string[] }> = [];
    const searchRequests: Array<{ query: string; source_type?: string; limit?: number }> = [];

    await page.route("**/api/v1/ingest/note", async (route) => {
      ingestRequests.push(route.request().postDataJSON());

      await route.fulfill({
        json: {
          job_id: "job-capture-1",
          status: "queued",
          progress: 0,
          error: null,
          item_id: null,
          duplicate_of: null,
        },
      });
    });

    await page.route("**/api/v1/jobs/job-capture-1", async (route) => {
      await route.fulfill({
        json: {
          job_id: "job-capture-1",
          status: "completed",
          progress: 100,
          error: null,
          item_id: capturedItem.id,
          duplicate_of: null,
        },
      });
    });

    await page.route(`**/api/v1/items/${capturedItem.id}`, async (route) => {
      await route.fulfill({ json: capturedItem });
    });

    await page.route(`**/api/v1/items/${capturedItem.id}/related`, async (route) => {
      await route.fulfill({
        json: {
          relationships: [
            {
              item_id: "33333333-3333-4333-8333-333333333333",
              title: "Launch brief",
              source_type: "note",
              relationship: "supports",
              confidence: 0.86,
            },
          ],
        },
      });
    });

    await page.route("**/api/v1/search", async (route) => {
      const body = route.request().postDataJSON() as { query: string; source_type?: string; limit?: number };
      searchRequests.push(body);

      await route.fulfill({
        json: {
          results: [
            {
              item_id: capturedItem.id,
              title: capturedItem.title,
              source_type: "note",
              score: 0.94,
              summary: capturedItem.summary,
              chunk_text: "Position around urgent recall for sourced answers.",
            },
          ],
          total: 1,
        },
      });
    });

    await page.goto(`${BASE_URL}/ingest?e2e=${Date.now()}`);

    await page.getByRole("button", { name: "Note" }).click();
    await page.getByPlaceholder("Note title").fill(capturedItem.title);
    await page.getByPlaceholder("Write your note here…").fill(capturedItem.raw_content);
    await page.getByPlaceholder("machine-learning, productivity").fill("launch, positioning");
    await page.getByRole("button", { name: "Capture" }).click();

    await expect.poll(() => ingestRequests).toEqual([
      {
        title: capturedItem.title,
        content: capturedItem.raw_content,
        tags: ["launch", "positioning"],
      },
    ]);
    await expect(page.getByText("Job queued")).toBeVisible();

    await page.getByRole("link", { name: /View Item/i }).click();
    await expect(page).toHaveURL(new RegExp(`/items/${capturedItem.id}`));
    await expect(page.getByRole("heading", { name: capturedItem.title })).toBeVisible();
    await expect(page.getByText(capturedItem.summary)).toBeVisible();
    await expect(page.getByText("Launch brief")).toBeVisible();
    await expect(page.getByRole("link", { name: "launch", exact: true })).toBeVisible();

    await page.goto(`${BASE_URL}/search?e2e=${Date.now()}`);
    await page.getByLabel("Search query").fill("launch wedge urgency");

    await expect.poll(() => searchRequests).toEqual([
      {
        query: "launch wedge urgency",
        limit: 20,
      },
    ]);
    await expect(page.getByText(capturedItem.title)).toBeVisible();
    await expect(page.getByText(capturedItem.summary)).toBeVisible();

    await page.getByRole("button", { name: capturedItem.title }).click();
    await expect(page).toHaveURL(new RegExp(`/items/${capturedItem.id}`));
  });

  test("renders library item detail relationships and tag edits on a narrow viewport", async ({ page }) => {
    const itemId = "44444444-4444-4444-8444-444444444444";
    const longTitle = "Quarterly workspace recall memo with a long retained source title";
    const patchedItems: Array<{ tags?: string[] }> = [];

    await page.setViewportSize({ width: 390, height: 844 });

    await page.route(`**/api/v1/items/${itemId}`, async (route) => {
      if (route.request().method() === "PATCH") {
        const body = route.request().postDataJSON() as { tags?: string[] };
        patchedItems.push(body);
        await route.fulfill({
          json: {
            id: itemId,
            source_type: "feed_article",
            source_url: "https://example.com/research/quarterly-recall",
            title: longTitle,
            summary: "Connects durable recall work to the library review loop.",
            raw_content: "A long source body that should stay readable when expanded on mobile.",
            metadata_: {
              feed_name: "Research Feed",
              feed_url: "https://example.com/feed.xml",
              author: "Palace Research",
              published: "2026-04-22T09:30:00Z",
            },
            tags: body.tags ?? ["recall", "agent-handoff"],
            categories: ["research"],
            status: "ready",
            created_at: "2026-04-22T09:30:00Z",
          },
        });
        return;
      }

      await route.fulfill({
        json: {
          id: itemId,
          source_type: "feed_article",
          source_url: "https://example.com/research/quarterly-recall",
          title: longTitle,
          summary: "Connects durable recall work to the library review loop.",
          raw_content: "A long source body that should stay readable when expanded on mobile.",
          metadata_: {
            feed_name: "Research Feed",
            feed_url: "https://example.com/feed.xml",
            author: "Palace Research",
            published: "2026-04-22T09:30:00Z",
          },
          tags: ["recall", "agent-handoff"],
          categories: ["research"],
          status: "ready",
          created_at: "2026-04-22T09:30:00Z",
        },
      });
    });

    await page.route(`**/api/v1/items/${itemId}/related`, async (route) => {
      await route.fulfill({
        json: {
          relationships: [
            {
              item_id: "55555555-5555-4555-8555-555555555555",
              title: "Very long related workspace transition note that should truncate cleanly",
              source_type: "note",
              relationship: "supports",
              confidence: 0.91,
            },
            {
              item_id: "66666666-6666-4666-8666-666666666666",
              title: "Recall readiness checklist",
              source_type: "doc",
              relationship: "related",
              confidence: 0.74,
            },
          ],
        },
      });
    });

    await page.goto(`${BASE_URL}/items/${itemId}?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: longTitle })).toBeVisible();
    await expect(page.getByText("Connects durable recall work to the library review loop.")).toBeVisible();
    await expect(page.getByText("Research Feed")).toBeVisible();
    await expect(page.getByText("Very long related workspace transition note")).toBeVisible();
    await expect(page.getByText("91%")).toBeVisible();

    await page.getByRole("button", { name: "Edit tags" }).click();
    await page.getByLabel("Comma-separated tags").fill("recall, mobile-review, handoff");
    await page.getByRole("button", { name: "Save" }).click();

    await expect.poll(() => patchedItems).toEqual([{ tags: ["recall", "mobile-review", "handoff"] }]);
    await expect(page.getByRole("link", { name: "mobile-review" })).toBeVisible();
    await expect
      .poll(() =>
        page.locator("main").evaluate((element) => element.scrollWidth - element.clientWidth),
      )
      .toBeLessThanOrEqual(1);
  });
});
