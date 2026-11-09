import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { focusDocumentStart, login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

/** Разделы боковой навигации (IA редизайна, план §6.1, D-3; порядок = порядок табуляции). */
const SECTIONS = [
  { name: 'Обзор', path: 'dashboard' },
  { name: 'Проекты', path: 'projects' },
  { name: 'AI Mission Control', path: 'mission-control' },
  { name: 'Агенты', path: 'agents' },
  { name: 'Процессы', path: 'runs' },
  { name: 'Одобрения', path: 'approvals' },
  { name: 'Code Intelligence', path: 'code' },
  { name: 'Центр безопасности', path: 'security' },
  { name: 'Гейты качества', path: 'quality' },
  { name: 'Стандарты и соответствие', path: 'compliance' },
  { name: 'Интеграция с GitHub', path: 'integrations' },
  { name: 'Аналитика', path: 'analytics' },
  { name: 'Документация', path: 'knowledge' },
  { name: 'Настройки', path: 'settings' },
];

/** Разделы, экраны которых появятся в следующих фазах (F7, F8): «Раздел в разработке» + ссылки на существующие экраны. Агенты — экран F5 (agents.spec). */
const PLACEHOLDERS = [
  { path: 'code', title: 'Code Intelligence' },
  { path: 'security', title: 'Центр безопасности' },
];

for (const scheme of ['light', 'dark'] as const) {
  test(`a11y (${scheme}): layout and every Phase 7 placeholder route, 404 and 403 have no serious/critical axe violations`, async ({
    page,
    api,
  }) => {
    await page.emulateMedia({ colorScheme: scheme });
    const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
    const ro = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
    await login(page, api);

    for (const p of PLACEHOLDERS) {
      await page.goto(`/w/${ws.id}/${p.path}`);
      await expect(page.getByRole('heading', { level: 1, name: p.title })).toBeFocused();
      await expect(page.getByTestId('area-placeholder')).toContainText('Раздел в разработке');
      await expect(page.getByRole('navigation', { name: 'Связанные разделы' }).getByRole('link').first()).toBeVisible();
      await expect(page).toHaveTitle(`${p.title} — AiCodingDev`);
      await expectNoSeriousA11yViolations(page, `${p.path} (${scheme})`);
    }

    await page.goto(`/w/${ws.id}/no-such-screen`);
    await expect(page.getByRole('heading', { level: 1, name: 'Страница не найдена' })).toBeVisible();
    await expectNoSeriousA11yViolations(page, `404 (${scheme})`);

    await page.goto(`/w/${ro.id}/audit`);
    await expect(page).toHaveURL(`/w/${ro.id}/settings/audit`);
    await expect(page.getByRole('heading', { level: 1, name: 'Недостаточно прав' })).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Разделы настроек' }).getByRole('link', { name: 'Аудит' })).toHaveCount(0);
    await expectNoSeriousA11yViolations(page, `403 (${scheme})`);

    await page.goto(`/w/${ws.id}/dashboard`);
    await page.getByRole('button', { name: 'Сочетания клавиш' }).click();
    await expect(page.getByRole('dialog', { name: 'Сочетания клавиш' })).toBeVisible();
    await expectNoSeriousA11yViolations(page, `shortcuts dialog (${scheme})`);
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: /Меню пользователя/ }).click();
    await expect(page.getByRole('menu')).toBeVisible();
    await expectNoSeriousA11yViolations(page, `user menu (${scheme})`);
    await page.keyboard.press('Escape');

    await page.getByRole('button', { name: /Рабочее пространство/ }).click();
    await expect(page.getByRole('dialog', { name: 'Рабочее пространство' })).toBeVisible();
    await expectNoSeriousA11yViolations(page, `workspace switcher (${scheme})`);
  });
}

test('theme: user menu switches light/dark (data-theme) and the choice persists', async ({ page, api }) => {
  api.addWorkspace('Команда', 'team', 'owner');
  await login(page, api);
  const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  // Фикстура начинает с «Как в системе»; Playwright по умолчанию эмулирует светлую схему.
  await expect.poll(bg).toBe('rgb(246, 248, 252)');
  await page.getByRole('button', { name: /Меню пользователя/ }).click();
  await page.getByRole('menuitemradio', { name: 'Тёмная' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect.poll(bg).toBe('rgb(8, 11, 20)');
  await expectNoSeriousA11yViolations(page, 'projects (forced dark)');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('keyboard-only: skip link, sidebar in tab order, Enter opens a section, shortcuts dialog traps focus, g-sequences', async ({
  page,
  api,
}) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  api.route('GET', '/api/v1/approvals', 'listApprovals', () => ({ status: 200, body: { items: [], next_cursor: null } }));
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Команда Альфа' })).toBeFocused();

  await focusDocumentStart(page);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Перейти к основному содержимому' })).toBeFocused();
  // Порядок фокуса (план §5.9): верхняя панель → боковая навигация → main.
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'AiCodingDev' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: /Рабочее пространство/ })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: /^Проект/ })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('combobox', { name: 'Поиск по рабочему пространству' })).toBeFocused();

  const nav = page.getByRole('navigation', { name: 'Основная навигация' });
  await tabTo(page, nav.getByRole('link', { name: 'Обзор', exact: true }));
  for (const s of SECTIONS.slice(1)) {
    await page.keyboard.press('Tab');
    await expect(nav.getByRole('link', { name: s.name, exact: s.name !== 'Одобрения' })).toBeFocused();
  }
  await page.keyboard.press('Tab');
  await expect(nav.getByRole('link', { name: 'Рабочие пространства' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Свернуть навигацию' })).toBeFocused();

  // Назад к «Одобрениям» и Enter.
  const approvals = nav.getByRole('link', { name: 'Одобрения' });
  await tabTo(page, approvals, { backwards: true });
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(`/w/${ws.id}/approvals`);
  await expect(page.getByRole('heading', { level: 1, name: 'Одобрения' })).toBeFocused();
  await expect(approvals).toHaveAttribute('aria-current', 'page');

  // «?» — справка; фокус заперт в диалоге; Escape закрывает.
  await page.keyboard.press('Shift+Slash');
  const dialog = page.getByRole('dialog', { name: 'Сочетания клавиш' });
  await expect(dialog).toBeVisible();
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press('Tab');
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);

  // g, затем q — Гейты качества; g, затем m — AI Mission Control (новое сочетание F2).
  await page.keyboard.press('g');
  await page.keyboard.press('q');
  await expect(page).toHaveURL(`/w/${ws.id}/quality`);
  await expect(page.getByRole('heading', { level: 1, name: 'Гейты качества' })).toBeFocused();
  await page.keyboard.press('g');
  await page.keyboard.press('m');
  await expect(page).toHaveURL(`/w/${ws.id}/mission-control`);
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeFocused();

  // Кнопка справки: после закрытия фокус возвращается на неё.
  const helpButton = page.getByRole('button', { name: 'Сочетания клавиш' });
  await tabTo(page, helpButton, { backwards: true });
  await page.keyboard.press('Enter');
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('button', { name: 'Закрыть' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(dialog).toHaveCount(0);
  await expect(helpButton).toBeFocused();
});

test('mobile (360px): navigation drawer is a keyboard-accessible dialog; no horizontal scroll', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ws = api.addWorkspace('Очень длинное название рабочего пространства команды', 'long', 'owner');
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({ status: 200, body: { items: [] } }));
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: /^Обзор/ })).toBeFocused();
  await expect(page.getByRole('navigation', { name: 'Основная навигация' })).toBeHidden();
  await expectNoHorizontalScroll(page);

  await focusDocumentStart(page);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Перейти к основному содержимому' })).toBeFocused();
  await page.keyboard.press('Tab');
  const menuButton = page.getByRole('button', { name: 'Открыть меню навигации' });
  await expect(menuButton).toBeFocused();
  await page.keyboard.press('Enter');

  const drawer = page.getByRole('dialog', { name: 'Навигация' });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole('button', { name: 'Закрыть' })).toBeFocused();
  await expectNoSeriousA11yViolations(page, 'mobile drawer');
  await expectNoHorizontalScroll(page);

  // В drawer — переключатели и статус CLI (в шапке на узком экране их нет), затем разделы.
  await expect(drawer.getByRole('list', { name: 'Состояние CLI-провайдеров' })).toBeVisible();
  const projects = drawer.getByRole('link', { name: 'Проекты', exact: true });
  await tabTo(page, projects, { max: 40 });
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(`/w/${ws.id}/projects`);
  await expect(drawer).toHaveCount(0);
  await expect(page.getByRole('heading', { level: 1, name: /^Проекты/ })).toBeFocused();

  await menuButton.click();
  await page.keyboard.press('Escape');
  await expect(drawer).toHaveCount(0);
  await expect(menuButton).toBeFocused();
  await expectNoSeriousA11yViolations(page, 'projects 360px');
});
