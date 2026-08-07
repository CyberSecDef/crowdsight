import { defineConfig, devices } from '@playwright/test'

/* Runs against the real sealed stack, not a dev server. The point is to see
   what the gateway actually serves — the CSP, the cache headers and the
   history-mode fallback are all gateway and nginx behaviour that a Vite dev
   server would paper over. Bring the stack up first: `docker compose up -d`. */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  reporter: process.env.CI ? 'line' : [['list']],
  use: {
    baseURL: process.env.CROWDSIGHT_BASE || 'http://127.0.0.1:8080',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
