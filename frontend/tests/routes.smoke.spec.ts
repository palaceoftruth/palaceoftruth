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

async function mockGraph(
  page: Parameters<typeof test>[0]["page"],
  options:
    | { type: "success"; json: { nodes: Array<Record<string, unknown>>; edges: Array<Record<string, unknown>> } }
    | { type: "error"; status?: number; body?: string },
) {
  await page.route("**/api/v1/graph?*", async (route) => {
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
  test("consent accepts tenant authentication inline without a client secret", async ({ page }) => {
    const interactionId = "22222222-2222-2222-2222-222222222222";
    let interactionRequests = 0;
    const sessionRequests: Array<{ method: string; body: string | null }> = [];
    await page.route("**/api/v1/browser/session", async (route) => {
      sessionRequests.push({ method: route.request().method(), body: route.request().postData() });
      await route.fulfill({
        status: 201,
        json: {
          tenant_id: "default",
          scopes: ["read", "write"],
          expires_at: "2099-08-07T12:10:00Z",
        },
      });
    });
    await page.route(`**/api/v1/memory/mcp/oauth/authorize/${interactionId}`, async (route) => {
      interactionRequests += 1;
      expect(route.request().headers()["x-api-key"]).toBeUndefined();
      expect(route.request().headers()["x-palace-consent-session"]).toBe("consent-session-inline");
      await route.fulfill({
        json: {
          client_name: "QuietFirm Staging",
          tenant_id: "default",
          resource: "https://api.palace.sarvent.cloud/api/v1",
          scopes: ["read", "write", "write:agent", "write:workspace", "write:session", "destructive_prohibited"],
          agent_scope_keys: [],
          workspace_scope_keys: [],
          all_memory_scopes: true,
        },
      });
    });

    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(`/oauth/consent?interaction_id=${interactionId}&e2e=${Date.now()}#consent_session=consent-session-inline&csrf_token=csrf-inline`);
    await expect(page).not.toHaveURL(/consent_session|csrf_token/);
    await expect(page.getByText("Authenticate this browser to the Palace tenant")).toBeVisible();
    await expect(page.getByText("Public PKCE client: no client secret is created or stored.")).toBeVisible();
    await page.getByLabel("Palace tenant API key").fill("tenant-browser-key");
    await page.getByRole("button", { name: "Save and review request" }).click();
    await expect(page.getByRole("heading", { name: "Review access request" })).toBeVisible();
    await expect(page.getByText("QuietFirm Staging", { exact: true })).toBeVisible();
    await expect(page.getByText("default", { exact: true })).toBeVisible();
    await expect(page.getByText("destructive_prohibited", { exact: true })).toBeVisible();
    expect(sessionRequests).toEqual([
      { method: "GET", body: null },
      { method: "POST", body: JSON.stringify({ api_key: "tenant-browser-key", elevated: true }) },
    ]);
    expect(interactionRequests).toBe(1);

    const screenshotDir = process.env.SAR1321_SCREENSHOT_DIR;
    if (screenshotDir) {
      await page.screenshot({ path: `${screenshotDir}/sar-1321-palace-consent-desktop.png`, fullPage: true });
      await page.setViewportSize({ width: 390, height: 844 });
      await expect(page.getByRole("button", { name: "Approve access" })).toBeVisible();
      await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
      await page.screenshot({ path: `${screenshotDir}/sar-1321-palace-consent-mobile.png`, fullPage: true });
    }
  });

  test("tenant admin can review a consent request on desktop and mobile", async ({ page }, testInfo) => {
    const interactionId = "11111111-1111-1111-1111-111111111111";
    const decisions: Array<{ headers: Record<string, string>; body: string }> = [];
    await page.context().addCookies([
      {
        // The session cookie itself is HttpOnly and server-set; the page only
        // sees this companion token, which is what gates the consent view.
        name: "palace_session_csrf",
        value: "session-csrf-test-token",
        url: testInfo.project.use.baseURL,
      },
    ]);
    await page.route(`**/api/v1/memory/mcp/oauth/authorize/${interactionId}`, async (route) => {
      expect(route.request().headers()["x-palace-consent-session"]).toBe("consent-session-test");
      await route.fulfill({
        json: {
          client_name: "NebulaiOS",
          tenant_id: "tenant-demo",
          resource: "https://api.palace.sarvent.cloud/api/v1",
          scopes: ["read", "write:workspace"],
          agent_scope_keys: ["codex"],
          workspace_scope_keys: ["palaceoftruth"],
          all_memory_scopes: false,
        },
      });
    });
    await page.route(`**/api/v1/memory/mcp/oauth/authorize/${interactionId}/decision`, async (route) => {
      decisions.push({ headers: route.request().headers(), body: route.request().postData() ?? "" });
      await route.fulfill({ json: { redirect_uri: "/oauth/complete" } });
    });

    await page.setViewportSize({ width: 1440, height: 960 });
    await page.goto(`/oauth/consent?interaction_id=${interactionId}&e2e=${Date.now()}#consent_session=consent-session-test&csrf_token=csrf-test-token`);
    await expect(page).not.toHaveURL(/consent_session|csrf_token/);
    await expect(page.getByRole("heading", { name: "Review access request" })).toBeVisible();
    await expect(page.getByText("NebulaiOS", { exact: true })).toBeVisible();
    await expect(page.getByText("tenant-demo", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Approve access" })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("oauth-consent-desktop.png"), fullPage: true });
    if (process.env.SAR1324_SCREENSHOT_DIR) {
      await page.screenshot({ path: `${process.env.SAR1324_SCREENSHOT_DIR}/sar-1324-consent-desktop.png`, fullPage: true });
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("heading", { name: "Review access request" })).toBeVisible();
    await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
    await page.screenshot({ path: testInfo.outputPath("oauth-consent-mobile.png"), fullPage: true });
    if (process.env.SAR1324_SCREENSHOT_DIR) {
      await page.screenshot({ path: `${process.env.SAR1324_SCREENSHOT_DIR}/sar-1324-consent-mobile.png`, fullPage: true });
    }

    await page.getByRole("button", { name: "Approve access" }).click();
    await expect.poll(() => decisions).toHaveLength(1);
    // H-20: no key header. The session cookie authenticates and the companion
    // token is echoed so an ambient cookie alone cannot approve a grant.
    expect(decisions[0]?.headers["x-api-key"]).toBeUndefined();
    expect(decisions[0]?.headers["x-palace-csrf"]).toBe("session-csrf-test-token");
    expect(decisions[0]?.headers["x-palace-consent-session"]).toBe("consent-session-test");
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

  test("home route never sends an API key header", async ({ page }) => {
    // H-20: the browser holds no key at all now. Authentication rides on the
    // HttpOnly session cookie, which Playwright cannot read either.
    const seenKeys: Array<{ endpoint: string; key: string }> = [];
    await page.route("**/api/v1/stats", async (route) => {
      seenKeys.push({ endpoint: "stats", key: route.request().headers()["x-api-key"] ?? "" });
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
      seenKeys.push({ endpoint: "items", key: route.request().headers()["x-api-key"] ?? "" });
      await route.fulfill({ json: { items: [], total: 0, page: 1, per_page: 10 } });
    });
    await page.route("**/api/v1/export?*", async (route) => {
      seenKeys.push({ endpoint: "export", key: route.request().headers()["x-api-key"] ?? "" });
      await route.fulfill({
        body: "export",
        headers: { "Content-Type": "application/zip" },
      });
    });

    await page.goto(`/?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Home" })).toBeVisible();
    await page.getByRole("button", { name: "Export JSON" }).click();
    await expect.poll(() => seenKeys.some(({ endpoint }) => endpoint === "export")).toBe(true);
    expect(seenKeys.length).toBeGreaterThanOrEqual(3);
    expect(seenKeys.every(({ key }) => key === "")).toBe(true);
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
    const pairingRequests: Array<{ headers: Record<string, string>; body: string | null }> = [];
    await page.route("**/api/v1/palace/browser-extension-pairing-keys", async (route) => {
      pairingRequests.push({ headers: route.request().headers(), body: route.request().postData() });
      await route.fulfill({
        status: 201,
        json: {
          pairing_key: "palpair_one-time-secret-not-persisted",
          tenant_id: "tenant-a",
          purpose: "browser_extension_token",
          expires_at: "2099-08-07T12:10:00Z",
          expires_in: 600,
        },
      });
    });
    // H-20: the SPA exchanges the key for a session and keeps no copy of it.
    const sessionRequests: Array<{ method: string; body: string | null }> = [];
    let signedIn = false;
    await page.route("**/api/v1/browser/session", async (route) => {
      const method = route.request().method();
      sessionRequests.push({ method, body: route.request().postData() });
      if (method === "POST") {
        signedIn = true;
        await route.fulfill({
          status: 201,
          json: {
            tenant_id: "tenant-a",
            scopes: ["read", "write", "admin"],
            expires_at: "2099-08-07T12:10:00Z",
          },
          headers: { "set-cookie": "palace_session_csrf=session-csrf-token; Path=/" },
        });
        return;
      }
      if (method === "DELETE") {
        signedIn = false;
        await route.fulfill({
          status: 204,
          body: "",
          headers: { "set-cookie": "palace_session_csrf=; Path=/; Max-Age=0" },
        });
        return;
      }
      if (!signedIn) {
        await route.fulfill({ status: 401, json: { detail: "No browser session" } });
        return;
      }
      await route.fulfill({
        json: {
          tenant_id: "tenant-a",
          scopes: ["read", "write", "admin"],
          expires_at: "2099-08-07T12:10:00Z",
        },
      });
    });
    await page.goto(`/settings?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByText("Browser-local preferences")).toBeVisible();
    await expect(page.getByText("Sign in needed")).toBeVisible();

    await page.getByLabel("Tenant API key").fill("tenant-browser-key");
    await page.getByLabel(/Enable administration tools/).check();
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Signed in", { exact: true })).toBeVisible();
    await expect(page.getByText("Signed in as tenant-a", { exact: false })).toBeVisible();
    // The key itself must never be written anywhere the page can read it back.
    expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("tenant-browser-key");
    expect(await page.evaluate(() => document.cookie)).not.toContain("tenant-browser-key");
    expect(sessionRequests.some(({ method }) => method === "POST")).toBe(true);

    await page.getByRole("link", { name: "Home", exact: true }).click();
    await page.getByRole("link", { name: "Settings", exact: true }).click();
    await expect(page.getByText("Signed in as tenant-a", { exact: false })).toBeVisible();

    await page.getByRole("button", { name: "Generate pairing key" }).click();
    await expect(page.getByTestId("pairing-key-reveal")).toBeVisible();
    await expect(page.getByLabel("One-time pairing key")).toHaveValue("palpair_one-time-secret-not-persisted");
    await expect(page.getByText("Tenant tenant-a")).toBeVisible();
    expect(pairingRequests).toHaveLength(1);
    // No key header at all; the unsafe request carries the CSRF echo instead.
    expect(pairingRequests[0].headers["x-api-key"]).toBeUndefined();
    expect(pairingRequests[0].headers["x-palace-csrf"]).toBe("session-csrf-token");
    expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("palpair_one-time-secret-not-persisted");

    await page.getByRole("button", { name: "Dismiss pairing key" }).click();
    await expect(page.getByLabel("One-time pairing key")).toHaveCount(0);

    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page.getByText("Sign in needed")).toBeVisible();
    expect(sessionRequests.some(({ method }) => method === "DELETE")).toBe(true);

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
