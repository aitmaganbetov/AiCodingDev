import { defineConfig, devices } from '@playwright/test';

// AICD_E2E_PORT позволяет параллельные прогоны (отдельный порт и каталог сборки).
const PORT = Number(process.env.AICD_E2E_PORT ?? 4173);
const OUT_DIR = PORT === 4173 ? 'dist' : `dist-e2e-${PORT}`;

/** E2E против `vite preview` со всеми ответами API из контрактно-валидированных моков (page.route). */
export default defineConfig({
  testDir: './e2e/mocked',
  outputDir: './test-results/mocked',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    locale: 'ru-RU',
    trace: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `pnpm exec vite build --logLevel warn --outDir ${OUT_DIR} && pnpm exec vite preview --outDir ${OUT_DIR} --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
