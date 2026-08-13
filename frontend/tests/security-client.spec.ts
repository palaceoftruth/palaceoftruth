import { expect, test } from "@playwright/test";

test("legacy API key is purged and exchanged before a non-settings route renders", async ({ page }) => {
  let exchangedKey = "";
  await page.addInitScript(() => localStorage.setItem("sb:browser_api_key", "legacy-secret"));
  await page.route("**/api/v1/browser/session", async (route, request) => {
    if (request.method() === "POST") {
      exchangedKey = (request.postDataJSON() as { api_key?: string }).api_key ?? "";
      await route.fulfill({ status: 200, json: { tenant_id: "default", scopes: ["read"], expires_at: null } });
      return;
    }
    await route.fulfill({ status: 401, json: { detail: "No browser session" } });
  });

  await page.goto("/search");

  await expect(page.getByRole("heading", { name: "Search" })).toBeVisible();
  await expect.poll(() => exchangedKey).toBe("legacy-secret");
  expect(await page.evaluate(() => localStorage.getItem("sb:browser_api_key"))).toBeNull();
});

test("public API documentation route is no longer part of the SPA", async ({ page }) => {
  await page.goto("/api-docs");

  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("link", { name: "API" })).toHaveCount(0);
});
