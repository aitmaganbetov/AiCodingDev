import { defineConfig, devices } from '@playwright/test';

/**
 * Smoke против реального стенда (SPA и API с одного origin). Запускается только
 * если задан AICD_E2E_LIVE_URL; учётные данные — AICD_E2E_EMAIL / AICD_E2E_PASSWORD.
 */
export default defineConfig({
  testDir: './e2e/live',
  outputDir: './test-results/live',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.AICD_E2E_LIVE_URL,
    locale: 'ru-RU',
    trace: 'off',
    ignoreHTTPSErrors: process.env.AICD_E2E_IGNORE_HTTPS_ERRORS === '1',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
