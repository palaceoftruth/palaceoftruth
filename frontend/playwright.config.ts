import { defineConfig } from "@playwright/test";

const configuredBaseURL = process.env.PLAYWRIGHT_BASE_URL;
const localPort = Number(process.env.PLAYWRIGHT_PORT ?? "3000");
const baseURL = configuredBaseURL || `http://127.0.0.1:${localPort}`;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["dot"], ["html", { open: "never" }], ["github"]]
    : "list",
  timeout: 30_000,
  outputDir: process.env.CI
    ? "test-results"
    : "/tmp/palaceoftruth-playwright-results",
  // Mocked browser specs should be runnable from a clean checkout. Full-stack
  // validation can still opt into devinfra with PLAYWRIGHT_BASE_URL.
  webServer: configuredBaseURL ? undefined : {
    command: `npm run dev -- --host 127.0.0.1 --port ${localPort}`,
    url: baseURL,
    reuseExistingServer: !process.env.CI && process.env.PLAYWRIGHT_PORT === undefined,
    timeout: 30_000,
  },
  use: {
    // Explicit environment configuration remains available for devinfra or a deployed target.
    baseURL,
    browserName: "chromium",
    ignoreHTTPSErrors: true,
    screenshot: "only-on-failure",
    trace: "on-first-retry",
  },
});
