import { defineConfig } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_BASE_URL || "https://palaceoftruth.test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  reporter: "list",
  timeout: 30_000,
  outputDir: "/tmp/palaceoftruth-playwright-results",
  use: {
    // Allow local verification against a dev server without rewriting the shared default.
    baseURL,
    browserName: "chromium",
    ignoreHTTPSErrors: true,
    trace: "on-first-retry",
  },
});
