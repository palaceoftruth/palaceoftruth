import { defineConfig } from "@playwright/test";

const configuredBaseURL = process.env.PLAYWRIGHT_BASE_URL;
const baseURL = configuredBaseURL || "http://127.0.0.1:3000";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  reporter: "list",
  timeout: 30_000,
  outputDir: "/tmp/palaceoftruth-playwright-results",
  // Mocked browser specs should be runnable from a clean checkout. Full-stack
  // validation can still opt into devinfra with PLAYWRIGHT_BASE_URL.
  webServer: configuredBaseURL ? undefined : {
    command: "npm run dev -- --host 127.0.0.1",
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
  use: {
    // Explicit environment configuration remains available for devinfra or a deployed target.
    baseURL,
    browserName: "chromium",
    ignoreHTTPSErrors: true,
    trace: "on-first-retry",
  },
});
