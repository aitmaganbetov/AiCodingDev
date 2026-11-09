import { test as base, expect } from '@playwright/test';
import { MockApi } from '../support/mockApi';

/**
 * Каждый тест получает свой мок API; после теста — нарушений контракта быть не должно.
 *
 * `themeDefault`: приложение по умолчанию в тёмной теме (F1 редизайна). Большинство спеков
 * проверяют обе темы через `page.emulateMedia({ colorScheme })`, поэтому по умолчанию тест
 * начинает с выбора «Как в системе» (если пользователь в тесте ещё ничего не выбрал);
 * `test.use({ themeDefault: 'app' })` — проверить настоящее значение по умолчанию.
 */
export const test = base.extend<{ api: MockApi; themeDefault: 'system' | 'app' }>({
  themeDefault: ['system', { option: true }],
  api: async ({ page, themeDefault }, use) => {
    if (themeDefault === 'system') {
      await page.addInitScript(() => {
        try {
          if (window.localStorage.getItem('aicd.theme') === null) window.localStorage.setItem('aicd.theme', 'system');
        } catch {
          /* хранилище недоступно — останется тема по умолчанию */
        }
      });
    }
    const api = new MockApi();
    await api.install(page);
    await use(api);
    expect(api.violations, 'contract violations (UI requests / mock responses)').toEqual([]);
  },
});

export { expect };
