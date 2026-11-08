import { expect, test } from '@playwright/test';
import { expectNoSeriousA11yViolations } from '../support/a11y';

/**
 * Smoke против реального backend (P1-13 DoD: вход → список проектов; axe без serious).
 * Пропускается, если не задан AICD_E2E_LIVE_URL. Создаёт workspace с уникальным slug
 * (создатель становится owner ⇒ есть project:create) и проект в нём.
 */
const LIVE_URL = process.env.AICD_E2E_LIVE_URL;
const EMAIL = process.env.AICD_E2E_EMAIL;
const PASSWORD = process.env.AICD_E2E_PASSWORD;

test.skip(!LIVE_URL, 'AICD_E2E_LIVE_URL не задан — live smoke пропущен');

test('live smoke: login → workspace → projects → create project → logout', async ({ page }) => {
  expect(EMAIL, 'AICD_E2E_EMAIL обязателен для live smoke').toBeTruthy();
  expect(PASSWORD, 'AICD_E2E_PASSWORD обязателен для live smoke').toBeTruthy();
  const suffix = Date.now().toString(36);

  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Вход' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'login');
  await page.getByLabel('Электронная почта').fill(EMAIL ?? '');
  await page.getByLabel('Пароль').fill(PASSWORD ?? '');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).not.toHaveURL(/\/login/);

  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Рабочие пространства' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Рабочие пространства' })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Загрузка' })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page, 'workspaces');

  await page.goto('/workspaces/new');
  await page.getByLabel('Название').fill(`E2E ${suffix}`);
  await page.getByLabel('Slug').fill(`e2e-${suffix}`);
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByRole('heading', { level: 1, name: `Проекты · E2E ${suffix}` })).toBeVisible();
  await expect(page.getByTestId('empty-state')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'projects (empty)');

  await page.getByRole('link', { name: 'Создать проект' }).first().click();
  await page.getByLabel('Название').fill(`Smoke ${suffix}`);
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByText(`Проект «Smoke ${suffix}» создан.`)).toBeVisible();
  await expect(page.getByRole('list', { name: 'Список проектов' })).toContainText(`Smoke ${suffix}`);
  await expectNoSeriousA11yViolations(page, 'projects (list)');

  await page.getByRole('button', { name: /Меню пользователя/ }).click();
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect(page.getByText('Вы вышли из системы.')).toBeVisible();
});
