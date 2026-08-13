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

test("routes wait for legacy session exchange before rendering", async ({ page }) => {
  let releaseExchange: (() => void) | undefined;
  const exchangeMayFinish = new Promise<void>((resolve) => {
    releaseExchange = resolve;
  });
  await page.addInitScript(() => localStorage.setItem("sb:browser_api_key", "legacy-secret"));
  await page.route("**/api/v1/browser/session", async (route, request) => {
    if (request.method() === "POST") {
      await exchangeMayFinish;
      await route.fulfill({ status: 200, json: { tenant_id: "default", scopes: ["read"], expires_at: null } });
      return;
    }
    await route.fulfill({ status: 401, json: { detail: "No browser session" } });
  });

  await page.goto("/search");
  await expect(
    page.getByRole("heading", { name: "Search the memory graph", exact: true }),
  ).toHaveCount(0);
  releaseExchange?.();
  await expect(
    page.getByRole("heading", { name: "Search the memory graph", exact: true }),
  ).toBeVisible();
});

test("routes render without waiting for a session request when no legacy key exists", async ({ page }) => {
  await page.route("**/api/v1/browser/session", async () => {
    await new Promise(() => undefined);
  });

  await page.goto("/search");

  await expect(
    page.getByRole("heading", { name: "Search the memory graph", exact: true }),
  ).toBeVisible();
});

test("legacy migration cannot leave the application blank indefinitely", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("sb:browser_api_key", "legacy-secret"));
  await page.route("**/api/v1/browser/session", async () => {
    await new Promise(() => undefined);
  });

  await page.goto("/search");

  await expect(
    page.getByRole("heading", { name: "Search the memory graph", exact: true }),
  ).toBeVisible({ timeout: 4_000 });
  expect(await page.evaluate(() => localStorage.getItem("sb:browser_api_key"))).toBeNull();
});
