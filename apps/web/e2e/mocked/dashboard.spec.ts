import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { focusDocumentStart, login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

/** P7-03 Dashboard: стартовый экран после входа; данные — listProjects + listRuns (последние 50). */

test('dashboard is the landing page: stats, runs needing attention, active and recent runs (light/dark axe)', async ({ page, api }) => {
  api.pageSize = 50;
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const pay = api.addProject(ws.id, 'Платёжный шлюз');
  const crm = api.addProject(ws.id, 'CRM');
  api.addRun(pay, { objective: 'Обновить зависимости', status: 'COMPLETED', created_at: '2026-11-05T08:00:00Z' });
  const blocked = api.addRun(crm, {
    objective: 'Аудит безопасности API',
    status: 'BLOCKED',
    blocked_reason: 'AUTH_REQUIRED',
    created_at: '2026-11-06T08:00:00Z',
  });
  api.addRun(pay, { objective: 'Миграция схемы БД', status: 'AWAITING_APPROVAL', created_at: '2026-11-07T08:00:00Z' });
  const running = api.addRun(pay, { objective: 'Экспорт отчётов в CSV', status: 'RUNNING', created_at: '2026-11-08T08:00:00Z' });

  await login(page, api);
  await expect(page).toHaveURL(`/w/${ws.id}/dashboard`);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Команда Альфа' })).toBeFocused();
  await expect(page).toHaveTitle('Обзор · Команда Альфа — AiCodingDev');

  const attention = page.getByRole('list', { name: 'Требуют внимания' });
  await expect(attention.getByRole('listitem')).toHaveCount(2);
  await expect(attention.getByRole('listitem').first()).toContainText('Миграция схемы БД');
  await expect(attention.getByRole('link', { name: 'К одобрениям' })).toHaveAttribute('href', `/w/${ws.id}/approvals`);
  const blockedItem = attention.locator(`[data-run-id="${blocked.id}"]`);
  await expect(blockedItem).toContainText('Требуется аутентификация провайдера');
  await expect(blockedItem).toContainText('CRM');
  await expect(blockedItem.getByRole('link', { name: 'К CLI-провайдерам' })).toHaveAttribute('href', `/w/${ws.id}/providers`);

  const active = page.getByRole('list', { name: 'Идут сейчас' });
  await expect(active.getByRole('listitem')).toHaveCount(1);
  await expect(active.getByRole('link', { name: /Открыть run/ })).toHaveAttribute('href', `/w/${ws.id}/projects/${pay.id}/runs/${running.id}`);

  const recent = page.getByRole('table', { name: /Последние запуски/ });
  await expect(recent.getByRole('row')).toHaveCount(5);
  await expect(page.getByText('Завершились с ошибкой')).toBeVisible();

  const runsRequest = api.requests.find((r) => r.operationId === 'listRuns');
  expect(runsRequest?.headers['x-workspace-id']).toBe(ws.id);

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `dashboard (${scheme})`);
  }

  // Клавиатура: быстрый переход к проектам.
  await focusDocumentStart(page);
  const allProjects = page.getByRole('navigation', { name: 'Быстрые переходы' }).getByRole('link', { name: 'Все проекты' });
  await tabTo(page, allProjects, { max: 60 });
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { level: 1, name: 'Проекты · Команда Альфа' })).toBeFocused();
});

test('dashboard empty state (viewer, no projects) and error state with retry; 360px without horizontal scroll', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ro = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Наблюдатели' })).toBeFocused();
  const empty = page.getByTestId('empty-state');
  await expect(empty).toContainText('Проектов пока нет');
  await expect(empty).toContainText('Создавать проекты могут владелец и администраторы');
  await expect(empty.getByRole('link')).toHaveCount(0);
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'dashboard empty 360px');

  api.addProject(ro.id, 'Сервис');
  api.failProjectsTimes = 1;
  await page.reload();
  const error = page.getByTestId('error-state');
  await expect(error).toContainText('Внутренняя ошибка сервера');
  await expectNoSeriousA11yViolations(page, 'dashboard error');
  await error.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('heading', { level: 2, name: 'Последние запуски' })).toBeVisible();
  await expect(page.getByText('Сейчас нет выполняющихся запусков.')).toBeVisible();
  await expectNoHorizontalScroll(page);
});
