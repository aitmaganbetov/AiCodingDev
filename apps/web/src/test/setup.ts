import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach, vi } from 'vitest';
import { clientConfig, setCsrfToken } from '../api/client';
import { resetPageHeadingFocus } from '../components/PageHeading';
import { loadAllNamespaces } from '../i18n';

// В приложении namespace областей грузятся лениво (lazyPage); unit-тестам нужны все строки сразу.
await loadAllNamespaces();

clientConfig.retryDelayMs = 1;

// Ожидания findBy*/waitFor: по умолчанию Testing Library ждёт 1 с — под параллельной нагрузкой
// (несколько воркеров на CPU) рендер экрана с запросами и React Query укладывается в это не всегда.
// 10 с — верхняя граница ожидания, а не задержка: успешное условие возвращается сразу.
configure({ asyncUtilTimeout: 10_000 });

// То же для vi.waitFor (по умолчанию 1 с, глобальной настройки нет): явные timeout/interval теста сохраняются.
const viWaitFor = vi.waitFor.bind(vi);
vi.waitFor = ((callback, options) =>
  viWaitFor(callback, typeof options === 'number' ? options : { timeout: 10_000, ...options })) as typeof vi.waitFor;

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
  resetPageHeadingFocus();
  setCsrfToken(null);
  document.cookie = 'aicd_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/';
  try {
    window.localStorage.clear();
  } catch {
    /* ignore */
  }
});
