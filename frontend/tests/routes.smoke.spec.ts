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
    await expect(page.getByText("No browser API key")).toBeVisible();

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
