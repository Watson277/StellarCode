import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 25000,
  fullyParallel: true,
  workers: 2,
  use: {
    baseURL: "http://127.0.0.1:1435",
    viewport: { width: 1380, height: 860 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 1435",
    url: "http://127.0.0.1:1435",
    reuseExistingServer: false,
  },
});
