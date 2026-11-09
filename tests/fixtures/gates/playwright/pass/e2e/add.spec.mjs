import { test, expect } from 'playwright/test';

// Без браузера: сеть и браузинг в verification запрещены.
test('adds', () => {
  expect(1 + 1).toBe(2);
});
