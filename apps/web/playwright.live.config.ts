import { defineConfig, devices } from '@playwright/test';

/**
 * Live E2E (SPA и API с одного origin, без моков backend).
 *
 * 1. Внешний стенд: AICD_E2E_LIVE_URL (+ AICD_E2E_EMAIL / AICD_E2E_PASSWORD) — только smoke; сценарии
 *    P7-14 (e2e/live/mission-control.spec.ts) дополнительно требуют AICD_E2E_LIVE_CONTROL_URL.
 * 2. AICD_E2E_LIVE_STACK=1 — конфиг сам собирает SPA (dist-live) и поднимает стенд
 *    tests/support/web_live_stack.py: API + воркеры outbox/orchestrator/exec + PostgreSQL (отдельная
 *    временная база) + Redis + S3 (если задан AICD_E2E_S3_ENDPOINT) + Fake CLI. Нужны uv и
 *    AICD_TEST_DATABASE_ADMIN_URL (суперпользователь PostgreSQL 17).
 */
const STACK = process.env.AICD_E2E_LIVE_STACK === '1';
const PORT = Number(process.env.AICD_E2E_LIVE_PORT ?? 4190);
const CONTROL_PORT = Number(process.env.AICD_E2E_LIVE_CONTROL_PORT ?? PORT + 1);

if (STACK) {
  process.env.AICD_E2E_LIVE_URL ??= `http://127.0.0.1:${PORT}`;
  process.env.AICD_E2E_LIVE_CONTROL_URL ??= `http://127.0.0.1:${CONTROL_PORT}`;
  process.env.AICD_E2E_EMAIL ??= 'e2e-operator@example.test';
  process.env.AICD_E2E_PASSWORD ??= 'Correct-Horse-Battery-9';
}

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
  webServer: STACK
    ? {
        command:
          'pnpm exec vite build --logLevel warn --outDir dist-live && cd ../.. && ' +
          `uv run python -m tests.support.web_live_stack --port ${PORT} --control-port ${CONTROL_PORT} --dist apps/web/dist-live`,
        url: `http://127.0.0.1:${CONTROL_PORT}/fixture`,
        reuseExistingServer: false,
        timeout: 300_000,
        stdout: 'pipe',
        stderr: 'pipe',
        // SIGTERM: стенд останавливает процессы и удаляет временную базу.
        gracefulShutdown: { signal: 'SIGTERM', timeout: 90_000 },
      }
    : undefined,
});
