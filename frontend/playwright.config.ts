import { defineConfig, devices } from "@playwright/test";

// Smoke tests run against the real FastAPI app with offline stand-ins (in-memory Qdrant,
// deterministic mock LLM, a local fixture repo): no network, no API keys.
const API_PORT = 8001;
const WEB_PORT = 3100;

export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } }, grep: /@mobile/ },
  ],
  webServer: [
    {
      command: `python -m uv run python -m tests.e2e_server --port ${API_PORT}`,
      cwd: "../backend",
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: `npx next build && npx next start --port ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}`,
      // NEXT_PUBLIC_* is inlined at build time, so the app is built with the test API URL.
      env: { NEXT_PUBLIC_API_URL: `http://127.0.0.1:${API_PORT}`, NEXT_TELEMETRY_DISABLED: "1" },
      reuseExistingServer: false,
      timeout: 300_000,
    },
  ],
});
