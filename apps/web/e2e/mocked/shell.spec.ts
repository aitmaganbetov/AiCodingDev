import type { Locator, Page } from '@playwright/test';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { PASSWORD, type MockApi } from '../support/mockApi';
import { expect, test } from './fixtures';

async function login(page: Page, api: MockApi) {
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Электронная почта').fill(api.user.email);
  await page.getByLabel('Пароль').fill(PASSWORD);
  await page.getByRole('button', { name: 'Войти' }).click();
}

/** Только клавиатура: Tab (или Shift+Tab), пока фокус не окажется на целевом элементе. */
async function tabTo(page: Page, target: Locator, { backwards = false, max = 20 } = {}) {
  for (let i = 0; i < max; i++) {
    if (await target.evaluate((el) => el === document.activeElement)) return;
    await page.keyboard.press(backwards ? 'Shift+Tab' : 'Tab');
  }
  await expect(target).toBeFocused();
}

test('login → workspace → projects list (pagination) → create project', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  for (let i = 1; i <= 4; i++) api.addProject(ws.id, `Проект ${i}`, i === 4 ? 'Описание проекта' : null);

  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1, name: 'Вход' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'login');

  await page.getByLabel('Электронная почта').fill(api.user.email);
  await page.getByLabel('Пароль').fill('wrong password');
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('alert')).toHaveText('Неверная электронная почта или пароль.');
  await expectNoSeriousA11yViolations(page, 'login error');

  await page.getByLabel('Пароль').fill(PASSWORD);
  await page.getByRole('button', { name: 'Войти' }).click();

  const heading = page.getByRole('heading', { level: 1, name: 'Проекты · Команда Альфа' });
  await expect(heading).toBeFocused();
  const list = page.getByRole('list', { name: 'Список проектов' });
  await expect(list.getByRole('listitem')).toHaveCount(3);
  await expectNoSeriousA11yViolations(page, 'projects list');

  await page.getByRole('button', { name: 'Загрузить ещё' }).click();
  await expect(list.getByRole('listitem')).toHaveCount(4);
  await expect(page.getByRole('button', { name: 'Загрузить ещё' })).toHaveCount(0);
  await expect(page.getByText('Показано 4 элемента')).toBeVisible();

  await page.getByRole('link', { name: 'Создать проект' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Новый проект' })).toBeFocused();
  await expect(page.getByRole('radio', { name: 'Умный (SMART)', exact: true })).toBeChecked();
  await expect(page.getByRole('radio', { name: 'Строгий (strict)', exact: true })).toBeChecked();
  await expectNoSeriousA11yViolations(page, 'project create');

  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByLabel('Название')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByLabel('Название')).toBeFocused();
  await expectNoSeriousA11yViolations(page, 'project create (validation errors)');

  await page.getByLabel('Название').fill('Платёжный шлюз');
  await page.getByLabel('Описание').fill('Сервис платежей');
  await page.getByRole('radio', { name: 'Строгий (STRICT)', exact: true }).check();
  await page.getByRole('button', { name: 'Создать' }).click();

  await expect(page.getByRole('status').filter({ hasText: 'Проект «Платёжный шлюз» создан.' })).toBeVisible();
  await expect(list.getByRole('listitem').first()).toContainText('Платёжный шлюз');
  await expect(list.getByRole('listitem').first()).toContainText('Строгий (STRICT)');

  const create = api.requests.find((r) => r.operationId === 'createProject');
  expect(create?.body).toEqual({
    name: 'Платёжный шлюз',
    description: 'Сервис платежей',
    documentation_mode: 'STRICT',
    quality_profile: 'strict',
  });
  expect(create?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
});

test('no workspaces → create workspace (client + server validation) → empty projects', async ({ page, api }) => {
  api.takenSlugs.add('my-team'); // slug занят чужим workspace
  await login(page, api);

  await expect(page).toHaveURL(/\/workspaces$/);
  await expect(page.getByTestId('empty-state')).toContainText('Рабочих пространств пока нет');
  await expectNoSeriousA11yViolations(page, 'workspaces empty');

  await page.getByRole('link', { name: 'Создать пространство' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Новое рабочее пространство' })).toBeFocused();
  await page.getByLabel('Название').fill('Моя команда');
  await page.getByLabel('Slug').fill('Моя-команда');
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByLabel('Slug')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByText(/Slug должен состоять из 2–63 символов/)).toBeVisible();
  expect(api.requests.some((r) => r.operationId === 'createWorkspace')).toBe(false);
  await expectNoSeriousA11yViolations(page, 'workspace create (errors)');

  await page.getByLabel('Slug').fill('my-team');
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByText('Значение уже используется.')).toBeVisible();
  await expect(page.getByLabel('Slug')).toHaveAttribute('aria-invalid', 'true');

  await page.getByLabel('Slug').fill('my-team-2');
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Проекты · Моя команда' })).toBeVisible();
  await expect(page.getByText('Рабочее пространство «Моя команда» создано.')).toBeVisible();
  await expect(page.getByTestId('empty-state')).toContainText('Проектов пока нет');
  await expectNoSeriousA11yViolations(page, 'projects empty');

  await page.getByRole('link', { name: 'Рабочие пространства' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Рабочие пространства' })).toBeFocused();
  await expect(page.getByRole('link', { name: 'Моя команда' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'workspaces list');
});

test('error state with retry; session expiry redirects to login', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  api.addProject(ws.id, 'Сервис');
  api.failProjectsTimes = 1;
  await login(page, api);

  const error = page.getByTestId('error-state');
  await expect(error).toContainText('Слишком много запросов');
  await expect(error).toContainText('0af7651916cd43dd8448eb211c80319c');
  await expectNoSeriousA11yViolations(page, 'projects error');
  await error.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('list', { name: 'Список проектов' }).getByRole('listitem')).toHaveCount(1);

  api.expireSessionOnNextRequest = true;
  await page.getByRole('link', { name: 'Рабочие пространства' }).click();
  await expect(page).toHaveURL(/\/login\?next=%2Fworkspaces$/);
  await page.getByLabel('Электронная почта').fill(api.user.email);
  await page.getByLabel('Пароль').fill(PASSWORD);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).toHaveURL(/\/workspaces$/);
});

test('role-aware UI: viewer does not see "Создать проект"', async ({ page, api }) => {
  const ws = api.addWorkspace('Только чтение', 'ro', 'viewer');
  api.addProject(ws.id, 'Проект');
  await login(page, api);
  await expect(page.getByRole('list', { name: 'Список проектов' }).getByRole('listitem')).toHaveCount(1);
  await expect(page.getByRole('link', { name: 'Создать проект' })).toHaveCount(0);
  await page.goto(`/w/${ws.id}/projects/new`);
  await expect(page.getByText(/нет права создавать проекты/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Создать' })).toHaveCount(0);
});

test('keyboard-only: login, skip link, switch workspace, create project, logout', async ({ page, api }) => {
  const a = api.addWorkspace('Альфа', 'alpha', 'owner');
  const b = api.addWorkspace('Бета', 'beta', 'admin');
  api.addProject(a.id, 'Альфа-проект');
  api.addProject(b.id, 'Бета-проект');

  await page.goto('/login');
  await expect(page.getByRole('heading', { level: 1, name: 'Вход' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Электронная почта')).toBeFocused();
  await page.keyboard.type(api.user.email);
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Пароль')).toBeFocused();
  await page.keyboard.type(PASSWORD);
  await page.keyboard.press('Enter');

  await expect(page.getByRole('heading', { level: 1, name: 'Проекты · Альфа' })).toBeFocused();

  // Skip link — первый элемент в порядке табуляции.
  // Точка начала последовательной навигации — начало документа (как после загрузки страницы).
  await page.evaluate(() => {
    document.body.tabIndex = -1;
    document.body.focus();
    document.body.removeAttribute('tabindex');
  });
  await page.keyboard.press('Tab');
  const skip = page.getByRole('link', { name: 'Перейти к основному содержимому' });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(page.locator('main#main')).toBeFocused();

  // Переключение workspace с клавиатуры.
  const switcher = page.getByRole('button', { name: /Рабочее пространство/ });
  await tabTo(page, switcher, { backwards: true });
  await page.keyboard.press('Enter');
  await expect(switcher).toHaveAttribute('aria-expanded', 'true');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: /Бета/ })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { level: 1, name: 'Проекты · Бета' })).toBeFocused();
  await expect(switcher).toHaveAttribute('aria-expanded', 'false');

  // Создание проекта: только Tab/стрелки/Enter.
  const createLink = page.getByRole('link', { name: 'Создать проект' }).first();
  await tabTo(page, createLink);
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { level: 1, name: 'Новый проект' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Название')).toBeFocused();
  await page.keyboard.type('Клавиатурный проект');
  await page.keyboard.press('Tab'); // описание
  await page.keyboard.press('Tab'); // documentation_mode: выбранная радиокнопка SMART
  await expect(page.getByRole('radio', { name: 'Умный (SMART)', exact: true })).toBeFocused();
  await page.keyboard.press('ArrowUp');
  await expect(page.getByRole('radio', { name: 'Выключен (OFF)', exact: true })).toBeChecked();
  await page.keyboard.press('Tab'); // quality_profile: strict
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('radio', { name: 'Критичный (critical)', exact: true })).toBeChecked();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Создать' })).toBeFocused();
  await page.keyboard.press('Enter');

  await expect(page.getByText('Проект «Клавиатурный проект» создан.')).toBeVisible();
  const created = api.requests.find((r) => r.operationId === 'createProject');
  expect(created?.body).toEqual({ name: 'Клавиатурный проект', documentation_mode: 'OFF', quality_profile: 'critical' });
  expect(created?.headers['x-workspace-id']).toBe(b.id);

  // Выход через меню пользователя.
  const userMenu = page.getByRole('button', { name: /Меню пользователя/ });
  await tabTo(page, userMenu, { backwards: true });
  await page.keyboard.press('Enter');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Выйти' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('Вы вышли из системы.')).toBeVisible();
  expect(api.loggedIn).toBe(false);
});

test('responsive at 360px: no horizontal scroll, controls reachable', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ws = api.addWorkspace('Очень длинное название рабочего пространства команды', 'long', 'owner');
  api.addProject(ws.id, 'Проект с очень длинным названием, которое должно переноситься', 'Описание '.repeat(30));

  await page.goto('/login');
  await expectNoHorizontalScroll(page);
  await page.getByLabel('Электронная почта').fill(api.user.email);
  await page.getByLabel('Пароль').fill(PASSWORD);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page.getByRole('list', { name: 'Список проектов' }).getByRole('listitem')).toHaveCount(1);
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'projects 360px');

  await page.getByRole('button', { name: /Меню пользователя/ }).click();
  await expect(page.getByRole('button', { name: 'Выйти' })).toBeInViewport();
  await page.keyboard.press('Escape');

  await page.getByRole('link', { name: 'Создать проект' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Новый проект' })).toBeVisible();
  await expectNoHorizontalScroll(page);
});
