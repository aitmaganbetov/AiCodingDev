import type { Page } from '@playwright/test';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/**
 * F1 редизайна (docs/design/UI_REDESIGN_PLAN.md §5): тема по умолчанию (тёмная), переключение
 * тёмная/светлая/как в системе с сохранением, prefers-reduced-motion, отсутствие горизонтальной
 * прокрутки на 375/768/1366/1920, справочник дизайн-системы (axe в обеих темах, клавиатура).
 */

/** CSS-длительность («.18s», «180ms», «0s») → мс. */
const ms = (v: string) => (v.endsWith('ms') ? Number(v.slice(0, -2)) : Number(v.slice(0, -1)) * 1000);
const bg = (page: Page) => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const DARK_BG = 'rgb(8, 11, 20)';
const LIGHT_BG = 'rgb(246, 248, 252)';

test.describe('theme default', () => {
  test.use({ themeDefault: 'app' });

  test('dark by default; light/dark/system switch via the user menu, persists across reloads; Inter is the UI font', async ({ page, api }) => {
    api.addWorkspace('Команда', 'team', 'owner');
    await login(page, api);
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect.poll(() => bg(page)).toBe(DARK_BG);
    expect(await page.evaluate(() => getComputedStyle(document.body).fontFamily)).toMatch(/^"Inter Variable"/);
    await expect.poll(() => page.evaluate(() => document.fonts.check('16px "Inter Variable"', 'Обзор'))).toBe(true);

    const menu = page.getByRole('button', { name: /Меню пользователя/ });
    await menu.click();
    await page.getByRole('menuitemradio', { name: 'Светлая' }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
    await expect.poll(() => bg(page)).toBe(LIGHT_BG);
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
    await expect.poll(() => bg(page)).toBe(LIGHT_BG);

    await page.getByRole('button', { name: /Меню пользователя/ }).click();
    await page.getByRole('menuitemradio', { name: 'Как в системе' }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'system');
    await page.emulateMedia({ colorScheme: 'dark' });
    await expect.poll(() => bg(page)).toBe(DARK_BG);
    await page.emulateMedia({ colorScheme: 'light' });
    await expect.poll(() => bg(page)).toBe(LIGHT_BG);
    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'system');
  });
});

test('reduced motion: motion tokens collapse to 0 ms and overlays open without running animations', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/design-system`);
  await expect(page.getByRole('heading', { level: 1, name: 'Дизайн-система' })).toBeVisible();
  const tokens = await page.evaluate(() => {
    const s = getComputedStyle(document.documentElement);
    return ['--dur-instant', '--dur-base', '--dur-slow'].map((k) => s.getPropertyValue(k).trim());
  });
  expect(tokens.map(ms)).toEqual([0, 0, 0]);
  await page.getByRole('button', { name: 'Открыть диалог' }).click();
  await expect(page.getByRole('dialog', { name: 'Пример диалога' })).toBeVisible();
  const durations = await page.evaluate(() => document.getAnimations().map((a) => Number(a.effect?.getComputedTiming().duration ?? 0)));
  for (const d of durations) expect(d).toBeLessThanOrEqual(0.01);
  await page.keyboard.press('Escape');

  // Без reduced motion токены — 120/180/250 мс.
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  const normal = await page.evaluate(() =>
    ['--dur-instant', '--dur-base', '--dur-slow'].map((k) => getComputedStyle(document.documentElement).getPropertyValue(k).trim()),
  );
  expect(normal.map(ms)).toEqual([120, 180, 250]);
});

test('no horizontal page overflow at 375/768/1366/1920 on dashboard, projects and the design system page (both themes)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа с очень длинным названием рабочего пространства', 'alpha', 'owner');
  const p = api.addProject(ws.id, 'Платёжный шлюз', 'Сервис приёма платежей и выплат с длинным описанием для проверки переносов строк');
  api.addRun(p, { objective: 'Обновить зависимости и проверить совместимость', status: 'RUNNING', created_at: '2026-11-08T08:00:00Z' });
  await login(page, api);
  for (const width of [375, 768, 1366, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of ['dashboard', 'projects', 'settings/design-system']) {
      await page.goto(`/w/${ws.id}/${path}`);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      for (const scheme of ['dark', 'light'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoHorizontalScroll(page);
      }
    }
  }
});

test('design system page: axe (light/dark), status never color-only, virtual list and resizable panels by keyboard with persisted size', async ({
  page,
  api,
}) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/design-system`);
  await expect(page.getByRole('heading', { level: 1, name: 'Дизайн-система' })).toBeFocused();
  await expect(page).toHaveTitle('Дизайн-система — AiCodingDev');
  // Страницы нет в навигации продукта.
  await expect(page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Дизайн-система' })).toHaveCount(0);

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `design system (${scheme})`);
  }

  // Пять состояний верификации: текст + иконка, исходный статус контракта рядом.
  for (const [state, label] of [
    ['PASS', 'Пройдено'],
    ['FAIL', 'Не пройдено'],
    ['BLOCKED', 'Заблокировано'],
    ['NOT_RUN', 'Не запускалось'],
    ['INSUFFICIENT_EVIDENCE', 'Недостаточно доказательств'],
  ] as const) {
    const badge = page.locator(`[data-verification="${state}"]`).first();
    await expect(badge).toContainText(label);
    await expect(badge.locator('svg')).toHaveCount(1);
  }
  await expect(page.locator('[data-agent-role]')).toHaveCount(14);
  await expect(page.locator('[data-agent-role="system"]')).toContainText('Система');

  // Виртуальный список: 10 000 строк, в DOM — только окно; End → последняя строка в фокусе.
  const feed = page.getByRole('feed', { name: /Пример ленты вывода CLI/ });
  expect(await feed.getByRole('article').count()).toBeLessThan(60);
  await feed.focus();
  await page.keyboard.press('ArrowDown');
  await expect(page.locator(':focus')).toHaveAttribute('aria-posinset', '1');
  await page.keyboard.press('End');
  await expect(page.locator(':focus')).toHaveAttribute('aria-posinset', '10000');
  await expect(page.locator(':focus')).toHaveAttribute('aria-setsize', '10000');
  await expect(page.locator(':focus')).toContainText('MOCK 10000');
  await page.keyboard.press('Home');
  await expect(page.locator(':focus')).toHaveAttribute('aria-posinset', '1');

  // Панели: разделитель с клавиатуры (WCAG 2.1.1, 2.5.7), размер сохраняется после перезагрузки.
  const sep = page.getByRole('separator', { name: 'Изменить размер: Левая панель / Правая панель' });
  const before = Number(await sep.getAttribute('aria-valuenow'));
  expect(before).toBeGreaterThan(35);
  expect(before).toBeLessThan(45);
  await sep.focus();
  for (let i = 0; i < 3; i++) await page.keyboard.press('ArrowRight');
  await expect.poll(async () => Number(await sep.getAttribute('aria-valuenow'))).toBeGreaterThan(before + 5);
  const after = Number(await sep.getAttribute('aria-valuenow'));
  await page.reload();
  await expect(page.getByRole('heading', { level: 1, name: 'Дизайн-система' })).toBeVisible();
  await expect.poll(async () => Math.round(Number(await sep.getAttribute('aria-valuenow')))).toBe(Math.round(after));
  await page.getByRole('button', { name: 'Сбросить размеры панелей' }).click();
  await expect.poll(async () => Math.round(Number(await sep.getAttribute('aria-valuenow')))).toBe(40);
});

test('forced-colors smoke: layout and design system render without horizontal overflow, focus ring stays visible', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  await page.emulateMedia({ forcedColors: 'active' });
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/design-system`);
  await expect(page.getByRole('heading', { level: 1, name: 'Дизайн-система' })).toBeVisible();
  await expectNoHorizontalScroll(page);
  const glass = await page.locator('[data-variant="glass"]').evaluate((el) => getComputedStyle(el).backdropFilter);
  expect(glass).toBe('none');
});
