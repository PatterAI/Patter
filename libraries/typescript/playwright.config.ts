import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 30000,
  webServer: {
    command: "npx tsx tests/e2e/test-server.ts",
    port: 8765,
    reuseExistingServer: !process.env.CI,
  },
});
