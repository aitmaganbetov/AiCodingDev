import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
import { clientConfig, setCsrfToken } from '../api/client';
import '../i18n';

clientConfig.retryDelayMs = 1;

afterEach(() => {
  cleanup();
  setCsrfToken(null);
  document.cookie = 'aicd_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
  try {
    window.localStorage.clear();
  } catch {
    /* ignore */
  }
});
