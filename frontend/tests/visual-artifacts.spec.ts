import { expect, test } from "@playwright/test";

const BASE_URL = process.env.PALACE_FRONTEND_BASE_URL ?? "http://127.0.0.1:4173";

const artifactCitation = {
  kind: "browser_image_candidate",
  thumbnail_url: "https://pbs.twimg.com/media/palace-diagram.jpg",
  caption: "Diagram from the launch post",
  extracted_text: ["Launch", "Recall"],
  source_url: "https://x.com/example/status/123",
  source_label: "Parent social post",
  original_artifact_url: "https://pbs.twimg.com/media/palace-diagram.jpg",
  original_artifact_label: "https://pbs.twimg.com/media/palace-diagram.jpg",
  media_type: "image/jpeg",
  dimensions: { width: 1200, height: 675 },
  provider: "openai",
  model: "gpt-4o-mini",
  confidence: 0.84,
};

test.describe("Visual artifact citations", () => {
  test("renders visual artifact metadata in search results and item detail", async ({ page }) => {
    await page.route("**/api/v1/search", async (route) => {
      await route.fulfill({
        json: {
          results: [
            {
              item_id: "image-item-1",
              title: "Launch diagram",
              source_type: "image_candidate",
              source_url: null,
              score: 0.94,
              summary: "Captured image from a social post.",
              chunk_text: "Diagram from the launch post",
              artifact_citation: artifactCitation,
            },
          ],
          total: 1,
        },
      });
    });

    await page.goto(`${BASE_URL}/search?e2e=${Date.now()}`);
    await page.getByLabel("Search query").fill("launch diagram");

    await expect(page.getByText("Visual artifact").first()).toBeVisible();
    await expect(page.getByText("Diagram from the launch post").first()).toBeVisible();
    await expect(page.getByRole("link", { name: /Parent social post/ })).toHaveAttribute(
      "href",
      "https://x.com/example/status/123",
    );
    await expect(page.getByRole("link", { name: /Inspect original/ })).toHaveAttribute(
      "href",
      "https://pbs.twimg.com/media/palace-diagram.jpg",
    );

    await page.route("**/api/v1/items/image-item-1", async (route) => {
      await route.fulfill({
        json: {
          id: "image-item-1",
          title: "Launch diagram",
          source_type: "image_candidate",
          source_url: null,
          summary: "Captured image from a social post.",
          raw_content: "Diagram from the launch post",
          metadata: {
            browser_capture_image: {
              source_post_url: "https://x.com/example/status/123",
              final_url: "https://pbs.twimg.com/media/palace-diagram.jpg",
              alt_text: "Diagram from the launch post",
              media_type: "image/jpeg",
              dimensions: { width: 1200, height: 675 },
            },
          },
          tags: ["launch"],
          categories: [],
          status: "ready",
          created_at: "2026-06-03T12:00:00Z",
        },
      });
    });
    await page.route("**/api/v1/items/image-item-1/related", async (route) => {
      await route.fulfill({ json: { relationships: [] } });
    });

    await page.getByText("Launch diagram").click();
    await expect(page.getByRole("heading", { name: "Launch diagram" })).toBeVisible();
    await expect(page.getByText("Visual artifact").first()).toBeVisible();
    await expect(page.getByText("image/jpeg")).toBeVisible();
    await expect(page.getByText("1200 x 675")).toBeVisible();
  });

  test("renders visual artifact metadata from streamed chat sources", async ({ page }) => {
    const conversation = {
      id: "conv-visual",
      title: "Find the diagram",
      created_at: "2026-06-03T12:00:00Z",
      updated_at: "2026-06-03T12:00:00Z",
    };
    await page.route("**/api/v1/conversations", async (route) => {
      await route.fulfill({ json: [conversation] });
    });
    await page.route("**/api/v1/conversations/conv-visual", async (route) => {
      await route.fulfill({
        json: {
          ...conversation,
          messages: [],
        },
      });
    });

    await page.route("**/api/v1/chat/stream", async (route) => {
      await route.fulfill({
        contentType: "text/event-stream",
        body: [
          "data: Found the visual source.",
          "",
          'data: {"type":"sources","sources":[{"item_id":"image-item-1","title":"Launch diagram","source_type":"image_candidate","chunk_text":"Diagram from the launch post","artifact_citation":' +
            JSON.stringify(artifactCitation) +
            "}]}",
          "",
          "data: [DONE]",
          "",
          "",
        ].join("\n"),
      });
    });

    await page.goto(`${BASE_URL}/chat?e2e=${Date.now()}`);
    await expect(page.getByRole("heading", { name: "Find the diagram" })).toBeVisible();
    await page.getByPlaceholder("Ask about projects, people, recent captures, or leave a thread for the next operator...").fill("Find the diagram");
    await page.keyboard.press("Enter");

    await expect(page.getByText("Found the visual source.")).toBeVisible();
    await page.getByText("1 source").click();
    await expect(page.getByText("Visual artifact")).toBeVisible();
    await expect(page.getByRole("link", { name: /Parent social post/ })).toHaveAttribute(
      "href",
      "https://x.com/example/status/123",
    );
  });
});
