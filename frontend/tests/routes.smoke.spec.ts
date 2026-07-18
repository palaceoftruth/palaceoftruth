import { expect, test } from "@playwright/test";

async function mockDashboard(page: Parameters<typeof test>[0]["page"]) {
  await page.route("**/api/v1/stats", async (route) => {
    await route.fulfill({
      json: {
        total_items: 42,
        ready_items: 40,
        indexed_items: 39,
        embedding_chunks: 84,
        total_embeddings: 84,
        active_jobs: 1,
        feed_count: 3,
      },
    });
  });

  await page.route("**/api/v1/items?*", async (route) => {
    await route.fulfill({
      json: {
        items: [
          {
            id: "11111111-1111-1111-1111-111111111111",
            source_type: "note",
            source_url: null,
            title: "Launch brief",
            summary: "Shared launch context for the next agent.",
            raw_content: "Agents should reuse the launch brief first.",
            content_chunks: null,
            metadata: {},
            tags: ["launch"],
            categories: [],
            status: "ready",
            created_at: "2026-04-13T12:00:00Z",
            updated_at: "2026-04-13T12:00:00Z",
          },
        ],
        total: 1,
        page: 1,
        per_page: 10,
      },
    });
  });
}

async function mockApiDocs(page: Parameters<typeof test>[0]["page"]) {
  let requested = false;
  await page.route("**/api/openapi.json", async (route) => {
    requested = true;
    await route.fulfill({
      json: {
        openapi: "3.1.0",
        info: {
          title: "Palace of Truth",
          version: "0.1.0",
        },
        paths: {
          "/api/v1/health": {
            get: {
              tags: ["system"],
              summary: "Health check",
              responses: {
                "200": {
                  description: "OK",
                },
              },
            },
          },
        },
      },
    });
  });
  return {
    wasRequested: () => requested,
  };
}

async function mockGraph(
  page: Parameters<typeof test>[0]["page"],
  options:
    | { type: "success"; json: { nodes: Array<Record<string, unknown>>; edges: Array<Record<string, unknown>> } }
    | { type: "error"; status?: number; body?: string },
) {
  await page.route("**/api/v1/graph", async (route) => {
    if (options.type === "error") {
      await route.fulfill({
        status: options.status ?? 503,
        body: options.body ?? "Graph unavailable",
      });
      return;
    }

    await route.fulfill({ json: options.json });
  });
}

test.describe("Route smoke", () => {
  test("tenant admin can review a consent request on desktop and mobile", async ({ page }, testInfo) => {
    const interactionId = "11111111-1111-1111-1111-111111111111";
    const decisions: Array<{ headers: Record<string, string>; body: string }> = [];
    await page.addInitScript(() => {
      localStorage.setItem("sb:browser_api_key", "tenant-browser-key");
    });
    await page.context().addCookies([{
      name: "palace_oauth_consent_csrf",
      value: "csrf-test-token",
      url: testInfo.project.use.baseURL,
    }]);
    await page.route(`**/api/v1/memory/mcp/oauth/authorize/${interactionId}`, async (route) => {
      await route.fulfill({
        json: {
          client_name: "NebulaiOS",
          tenant_id: "tenant-demo",
          resource: "https://api.palace.sarvent.cloud/api/v1",
          scopes: ["read", "write:workspace"],
          agent_scope_keys: ["codex"],
          workspace_scope_keys: ["palaceoftruth"],
        },
      });
    });
    await page.route(`**/api/v1/memory/mcp/oauth/authorize/${interactionId}/decision`, async (route) => {
      decisions.push({ headers: route.request().headers(), body: route.request().postData() ?? "" });
      await route.fulfill({ json: { redirect_uri: "/oauth/complete" } });
    });

    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(`/oauth/consent?interaction_id=${interactionId}&e2e=${Date.now()}`);
    await expect(page.getByRole("heading", { name: "Review access request" })).toBeVisible();
    await expect(page.getByText("NebulaiOS", { exact: true })).toBeVisible();
    await expect(page.getByText("tenant-demo", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve access" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("oauth-consent-desktop.png"), fullPage: true });

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("heading", { name: "Review access request" })).toBeVisible();
    await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
    await page.screenshot({ path: testInfo.outputPath("oauth-consent-mobile.png"), fullPage: true });

    await page.getByRole("button", { name: "Approve access" }).click();
    await expect.poll(() => decisions).toHaveLength(1);
    expect(decisions[0]?.headers["x-api-key"]).toBe("tenant-browser-key");
    expect(decisions[0]?.body).toContain('name="decision"');
    expect(decisions[0]?.body).toContain("approved");
    expect(decisions[0]?.body).toContain('name="csrf_token"');
    expect(decisions[0]?.body).toContain("csrf-test-token");
  });

  test("home route shows stats shell and recent captures", async ({ page }) => {
    await mockDashboard(page);

    await page.goto(`/?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Export JSON" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Export Markdown" })).toBeVisible();
    await expect(page.getByText("Recent captures")).toBeVisible();
    await expect(page.getByText("Launch brief")).toBeVisible();
    await expect(page.getByText("Library Items")).toBeVisible();
    await expect(page.getByText("Indexed Items")).toBeVisible();
  });

  test("home route sends the browser API key from local storage", async ({ page }) => {
    const seenKeys: string[] = [];
    await page.addInitScript(() => {
      localStorage.setItem("sb:browser_api_key", "browser-test-key");
    });
    await page.route("**/api/v1/stats", async (route) => {
      seenKeys.push(route.request().headers()["x-api-key"] ?? "");
      await route.fulfill({
        json: {
          total_items: 0,
          ready_items: 0,
          indexed_items: 0,
          embedding_chunks: 0,
          total_embeddings: 0,
          active_jobs: 0,
          feed_count: 0,
        },
      });
    });
    await page.route("**/api/v1/items?*", async (route) => {
      seenKeys.push(route.request().headers()["x-api-key"] ?? "");
      await route.fulfill({ json: { items: [], total: 0, page: 1, per_page: 10 } });
    });
    await page.route("**/api/v1/export?*", async (route) => {
      seenKeys.push(route.request().headers()["x-api-key"] ?? "");
      await route.fulfill({
        body: "export",
        headers: { "Content-Type": "application/zip" },
      });
    });

    await page.goto(`/?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
    await page.getByRole("button", { name: "Export JSON" }).click();
    await expect.poll(() => seenKeys).toEqual(["browser-test-key", "browser-test-key", "browser-test-key"]);
  });

  test("api docs route requests and renders the OpenAPI document", async ({ page }) => {
    const docs = await mockApiDocs(page);

    await page.goto(`/api-docs?e2e=${Date.now()}`);

    await expect.poll(docs.wasRequested).toBe(true);
    await expect(page.getByRole("heading", { name: "API Docs" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Open raw spec" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Reference surface" })).toContainText("/api/openapi.json");
    await expect(page.getByRole("region", { name: "Contract explorer" })).toContainText("Tenant-aware backend");
    await expect(page.getByRole("heading", { name: "Palace of Truth" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Health check/ })).toBeVisible();
  });

  test("graph route keeps Palace shell chrome around the empty state", async ({ page }) => {
    await mockGraph(page, { type: "success", json: { nodes: [], edges: [] } });

    await page.goto(`/graph?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Knowledge graph" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Reload graph" })).toBeVisible();
    await expect(page.getByText("No relationships are mapped yet.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Open Library" })).toBeVisible();
  });

  test("graph route keeps Palace shell chrome around API errors", async ({ page }) => {
    await mockGraph(page, { type: "error", body: "Graph unavailable" });

    await page.goto(`/graph?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Knowledge graph" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Reload graph" })).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("Graph unavailable");
    await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  });

  test("graph route sizes the rendered canvas to its responsive viewport", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 760 });
    await mockGraph(page, {
      type: "success",
      json: {
        nodes: [
          { id: "node-a", title: "Launch brief", source_type: "note", tags: ["launch"] },
          { id: "node-b", title: "Planning memo", source_type: "doc", tags: ["launch"] },
        ],
        edges: [{ source: "node-a", target: "node-b", relationship: "supports", confidence: 0.86 }],
      },
    });

    await page.goto(`/graph?e2e=${Date.now()}`);

    const viewport = page.getByTestId("graph-canvas-viewport");
    const canvas = viewport.locator("canvas").first();
    await expect(canvas).toBeVisible();

    const canvasFitsViewport = async () =>
      viewport.evaluate((element) => {
        const canvasElement = element.querySelector("canvas");
        const main = document.querySelector("main");
        if (!canvasElement || !main) return false;

        const viewportRect = element.getBoundingClientRect();
        const canvasRect = canvasElement.getBoundingClientRect();
        const mainOverflow = main.scrollWidth - main.clientWidth;

        return (
          viewportRect.width > 0 &&
          viewportRect.height > 0 &&
          canvasRect.width > 0 &&
          canvasRect.height > 0 &&
          canvasRect.left >= viewportRect.left - 1 &&
          canvasRect.right <= viewportRect.right + 1 &&
          mainOverflow <= 1
        );
      });

    await expect.poll(canvasFitsViewport).toBe(true);

    await page.setViewportSize({ width: 390, height: 844 });
    await expect.poll(canvasFitsViewport).toBe(true);
  });

  test("settings route exposes utility metadata and saves browser-local preferences", async ({ page }) => {
    await page.goto(`/settings?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByText("Browser-local preferences")).toBeVisible();
    await expect(page.getByText("Browser API key needed")).toBeVisible();

    await page.getByLabel("Browser API key").fill("tenant-browser-key");
    await page.getByRole("button", { name: "Save API key" }).click();
    await expect(page.getByText("Browser API key saved")).toBeVisible();
    await expect(page.getByText("API key saved for this browser.")).toBeVisible();
    await expect.poll(() => page.evaluate(() => localStorage.getItem("sb:browser_api_key"))).toBe("tenant-browser-key");

    await page.getByRole("button", { name: "Clear" }).click();
    await expect(page.getByText("Browser API key needed")).toBeVisible();
    await expect.poll(() => page.evaluate(() => localStorage.getItem("sb:browser_api_key"))).toBeNull();

    await page.getByLabel("Items per page (Library)").selectOption("50");
    await page.getByLabel("Default sort order").selectOption("title|asc");
    await page.getByRole("button", { name: "Save preferences" }).click();

    await expect(page.getByText("Preferences updated in local storage.")).toBeVisible();
    await expect
      .poll(() =>
        page.evaluate(() => ({
          perPage: localStorage.getItem("sb:per_page"),
          defaultSort: localStorage.getItem("sb:default_sort"),
        })),
      )
      .toEqual({
        perPage: "50",
        defaultSort: "title|asc",
      });
  });
});
