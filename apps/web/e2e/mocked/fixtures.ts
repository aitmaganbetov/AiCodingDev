import { test as base, expect } from '@playwright/test';
import { MockApi } from '../support/mockApi';

/** Каждый тест получает свой мок API; после теста — нарушений контракта быть не должно. */
export const test = base.extend<{ api: MockApi }>({
  api: async ({ page }, use) => {
    const api = new MockApi();
    await api.install(page);
    await use(api);
    expect(api.violations, 'contract violations (UI requests / mock responses)').toEqual([]);
  },
});

export { expect };
