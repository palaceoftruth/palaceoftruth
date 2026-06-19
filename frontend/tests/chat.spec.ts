import { expect, test } from "@playwright/test";

const BASE_URL = process.env.PALACE_FRONTEND_BASE_URL ?? "http://127.0.0.1:4173";

test.describe("Chat route", () => {
  test("loads a persisted thread, sends with its conversation id, and resumes it after reload", async ({ page }) => {
    const existingConversation = {
      id: "conv-existing",
      title: "NIST recall thread",
      created_at: "2026-04-23T10:00:00Z",
      updated_at: "2026-04-23T10:12:00Z",
    };
    const streamedConversation = {
      id: "conv-new",
      title: "What changed in recall?",
      created_at: "2026-04-24T10:00:00Z",
      updated_at: "2026-04-24T10:01:00Z",
    };
    const streamBodies: Array<{ conversation_id?: string; messages: Array<{ role: string; content: string }> }> = [];
    let includeStreamedConversation = false;

    await page.route("**/api/v1/conversations", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({ json: streamedConversation });
        return;
      }

      await route.fulfill({
        json: includeStreamedConversation
          ? [streamedConversation, existingConversation]
          : [existingConversation],
      });
    });

    await page.route("**/api/v1/conversations/conv-existing", async (route) => {
      await route.fulfill({
        json: {
          ...existingConversation,
          messages: [
            {
              id: "msg-existing-user",
              conversation_id: existingConversation.id,
              role: "user",
              content: "What should the next operator read?",
              created_at: "2026-04-23T10:01:00Z",
            },
            {
              id: "msg-existing-assistant",
              conversation_id: existingConversation.id,
              role: "assistant",
              content: "Start with the retained NIST benchmark notes.",
              created_at: "2026-04-23T10:02:00Z",
            },
          ],
        },
      });
    });

    await page.route("**/api/v1/conversations/conv-new", async (route) => {
      await route.fulfill({
        json: {
          ...streamedConversation,
          messages: [
            {
              id: "msg-new-user",
              conversation_id: streamedConversation.id,
              role: "user",
              content: "What changed in recall?",
              created_at: "2026-04-24T10:00:30Z",
            },
            {
              id: "msg-new-assistant",
              conversation_id: streamedConversation.id,
              role: "assistant",
              content: "The library now resumes durable chat threads.",
              created_at: "2026-04-24T10:01:00Z",
            },
          ],
        },
      });
    });

    await page.route("**/api/v1/chat/stream", async (route) => {
      streamBodies.push(route.request().postDataJSON());
      includeStreamedConversation = true;
      await route.fulfill({
        contentType: "text/event-stream",
        body: [
          "data: The library now ",
          "",
          "data: resumes durable chat threads.",
          "",
          "data: [DONE]",
          "",
          "",
        ].join("\n"),
      });
    });

    await page.goto(`${BASE_URL}/chat?e2e=${Date.now()}`);

    await expect(page.getByRole("heading", { name: "Chat with the workspace" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "NIST recall thread" })).toBeVisible();
    await expect(page.getByText("What should the next operator read?")).toBeVisible();
    await expect(page.getByText("Start with the retained NIST benchmark notes.")).toBeVisible();
    await expect(page.getByText("Persisted")).toBeVisible();

    await page.getByRole("button", { name: "New conversation" }).click();
    await expect(page.getByText("Draft")).toBeVisible();
    await page.getByPlaceholder("Ask about projects, people, recent captures, or leave a thread for the next operator...").fill("What changed in recall?");
    await page.keyboard.press("Enter");

    await expect.poll(() => streamBodies).toEqual([
      {
        conversation_id: streamedConversation.id,
        messages: [{ role: "user", content: "What changed in recall?" }],
      },
    ]);
    await expect(page.getByRole("heading", { name: "What changed in recall?" })).toBeVisible();
    await expect(page.getByText("The library now resumes durable chat threads.")).toBeVisible();
    await expect(page.getByText("Persisted")).toBeVisible();

    await page.reload();

    await expect(page.getByRole("heading", { name: "What changed in recall?" })).toBeVisible();
    await expect(page.getByText("The library now resumes durable chat threads.")).toBeVisible();
    await expect(page.getByText("NIST recall thread")).toBeVisible();
  });
});
