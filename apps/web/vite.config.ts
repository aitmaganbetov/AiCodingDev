/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Бэкенд FastAPI (/api/v1). Переопределяется переменной окружения AICD_API_URL.
const apiTarget = process.env.AICD_API_URL ?? 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      // changeOrigin=false: бэкенд проверяет Origin/Sec-Fetch-Site (API_DESIGN §3).
      '/api': { target: apiTarget, changeOrigin: false },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy: {
      '/api': { target: apiTarget, changeOrigin: false },
    },
  },
  build: {
    sourcemap: true,
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    unstubGlobals: true,
    // tokens.test.ts читает tokens.css как текст (?raw): остальной CSS в unit-тестах не нужен.
    css: { include: [/styles\/tokens\.css/] },
  },
});
