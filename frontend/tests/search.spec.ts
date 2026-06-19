import { expect, test } from "@playwright/test";

const BASE_URL = process.env.PALACE_FRONTEND_BASE_URL ?? "http://127.0.0.1:4173";

test.describe("Search route", () => {
  test("uses the media filter, refetches on filter change, and clears stale results when the query is emptied", async ({ page }) => {
    const requests: Array<{ query: string; source_type?: string }> = [];

    await page.route("**/api/v1/search", async (route) => {
      const body = route.request().postDataJSON() as { query: string; source_type?: string };
      requests.push(body);

      const resultLabel = body.source_type ? `Filtered: ${body.source_type}` : "Filtered: all";
      await route.fulfill({
        json: {
          results: [
            {
              item_id: `item-${requests.length}`,
              title: resultLabel,
              source_type: body.source_type ?? "note",
              score: 0.93,
              summary: `Search for ${body.query}`,
              chunk_text: `Chunk for ${body.query}`,
            },
          ],
          total: 1,
        },
      });
    });

    await page.goto(`${BASE_URL}/search?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Search the memory graph" })).toBeVisible();
    await expect(page.getByText("Semantic ranking")).toBeVisible();
    await expect(page.getByText("All source types")).toBeVisible();
    await expect(page.getByText("Tenant corpus")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open Library" })).toBeVisible();

    const queryInput = page.getByLabel("Search query");
    const sourceType = page.getByLabel("Source type");

    await queryInput.fill("launch brief");
    await expect.poll(() => requests.length).toBe(1);
    expect(requests[0]).toMatchObject({ query: "launch brief" });
    await expect(page.getByText("Filtered: all")).toBeVisible();

    await sourceType.selectOption("media");
    await expect.poll(() => requests.length).toBe(2);
    expect(requests[1]).toMatchObject({
      query: "launch brief",
      source_type: "media",
    });
    await expect(page.getByText("Filtered: media")).toBeVisible();

    await queryInput.fill("");
    await expect(page.getByText("Filtered: media")).toHaveCount(0);
    await expect(page.getByText("Search across everything you have captured.")).toBeVisible();
  });

  test("keeps a framed Palace shell around search API errors", async ({ page }) => {
    await page.route("**/api/v1/search", async (route) => {
      await route.fulfill({
        status: 503,
        body: "Search unavailable",
      });
    });

    await page.goto(`${BASE_URL}/search?e2e=${Date.now()}`);

    await page.getByLabel("Search query").fill("broken index");
    await expect(page.getByRole("alert")).toContainText("Search unavailable");
    await expect(page.getByRole("button", { name: "Try search again" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open Library" })).toBeVisible();
  });
});

test.describe("Settings route", () => {
  test("frames environment access and local preferences as utility surfaces", async ({ page }) => {
    await page.goto(`${BASE_URL}/settings?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByText("Read-only environment")).toBeVisible();
    await expect(page.getByText("Browser-local preferences")).toBeVisible();
    await expect(page.getByText("API access")).toBeVisible();
    await expect(page.getByText("Credential source")).toBeVisible();
    await expect(page.getByLabel("Items per page (Library)")).toBeVisible();
    await expect(page.getByLabel("Default sort order")).toBeVisible();

    await page.getByLabel("Items per page (Library)").selectOption("50");
    await page.getByRole("button", { name: "Save preferences" }).click();
    await expect(page.getByRole("button", { name: "Saved" })).toBeVisible();
    await expect(page.getByText("Preferences updated in local storage.")).toBeVisible();
  });
});
