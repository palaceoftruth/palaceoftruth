import { expect, test } from "@playwright/test";

const savedAt = "2026-05-12T15:20:00Z";

const webSave = {
  id: "11111111-2222-4333-8444-555555555555",
  item_id: "22222222-3333-4444-8555-666666666666",
  original_url: "https://example.com/research/brief",
  normalized_url: "https://example.com/research/brief",
  source_title: "Research Brief",
  source_domain: "example.com",
  capture_kind: "webpage",
  user_tags: ["research", "policy"],
  saved_at: savedAt,
  archived_at: null,
  extension_version: "0.1.9",
  metadata: { browser_capture: { preview_media: null } },
  item: {
    id: "22222222-3333-4444-8555-666666666666",
    title: "Research Brief",
    source_type: "webpage",
    status: "ready",
    summary: "A compact source summary for saved-web review.",
    tags: ["research", "policy"],
    metadata: {},
    created_at: savedAt,
    updated_at: savedAt,
  },
};

test.describe("Saved Web collection", () => {
  test("lists, filters, opens detail, and archives an active save", async ({ page }) => {
    const requests: string[] = [];
    let archived = false;

    await page.route("**/api/v1/web-saves?*", async (route) => {
      const url = new URL(route.request().url());
      requests.push(url.search);
      const captureKind = url.searchParams.get("capture_kind");
      const q = url.searchParams.get("q") ?? "";
      const tag = url.searchParams.get("tag") ?? "";
      const matches =
        !archived &&
        (!captureKind || captureKind === "webpage") &&
        (!q || webSave.source_title.toLowerCase().includes(q.toLowerCase())) &&
        (!tag || webSave.user_tags.includes(tag));

      await route.fulfill({
        json: {
          web_saves: matches ? [webSave] : [],
          total: matches ? 1 : 0,
          page: 1,
          per_page: 100,
        },
      });
    });

    await page.route(`**/api/v1/items/${webSave.item_id}/related`, async (route) => {
      await route.fulfill({
        json: {
          relationships: [
            {
              item_id: "33333333-4444-4555-8666-777777777777",
              title: "Related source",
              source_type: "webpage",
              relationship: "supports",
              confidence: 0.82,
            },
          ],
        },
      });
    });

    await page.route(`**/api/v1/web-saves/${webSave.id}`, async (route) => {
      archived = true;
      await route.fulfill({ json: { ...webSave, archived_at: "2026-05-12T15:30:00Z" } });
    });

    await page.goto(`/saved-web?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Saved Web" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Research Brief/ })).toBeVisible();
    await expect(page.getByText("example.com").first()).toBeVisible();
    await expect(page.getByText("Related source")).toBeVisible();

    await page.getByPlaceholder("Search saved pages").fill("Research");
    await expect.poll(() => requests.some((request) => request.includes("q=Research"))).toBe(true);

    await page.getByRole("button", { name: /Pages/ }).click();
    await expect.poll(() => requests.some((request) => request.includes("capture_kind=webpage"))).toBe(true);

    await page.getByRole("button", { name: "Archive saved page" }).click();
    await expect(page.getByText("No saved pages match this view.")).toBeVisible();
  });
});
