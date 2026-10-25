/**
 * End-to-end tests on the full stack with real Keycloak (ADR 0015).
 *
 * Needs the infrastructure up (`make up`). Playwright then starts the application
 * processes (`scripts/e2e_stack.py`: API, AI gateway on MockLLM, both workers) and the
 * UI dev server, and stops them afterwards. Locally, already running servers are reused.
 */
import { defineConfig, devices } from "@playwright/test";

const CI = Boolean(process.env.CI);

export default defineConfig({
  testDir: "e2e",
  // Every test works through its own cases, but they share the demo tenant's settings.
  workers: 1,
  // Whole multi-person flows (intake, data entry, approval) with real sign-ins.
  timeout: 480_000,
  expect: { timeout: 30_000 },
  retries: CI ? 1 : 0,
  reporter: CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "uv run python ../../scripts/e2e_stack.py",
      url: "http://127.0.0.1:8000/healthz",
      reuseExistingServer: !CI,
      timeout: 240_000,
      stdout: "pipe",
    },
    {
      command: "npm run dev -- --port 5173 --strictPort",
      url: "http://localhost:5173",
      reuseExistingServer: !CI,
      timeout: 120_000,
    },
  ],
});
