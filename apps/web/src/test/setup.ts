import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
import { clientConfig, setCsrfToken } from '../api/client';
import { loadAllNamespaces } from '../i18n';

// В приложении namespace областей грузятся лениво (lazyPage); unit-тестам нужны все строки сразу.
await loadAllNamespaces();

clientConfig.retryDelayMs = 1;

// jsdom не реализует API, которые использует Radix UI (floating-ui, pointer capture).
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => undefined;
Element.prototype.scrollIntoView ??= () => undefined;

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
