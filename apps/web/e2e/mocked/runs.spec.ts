import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/** Запуски workspace (GET /runs): фильтры статуса/проекта в URL, cursor-пагинация, ссылки на экраны run. */
function seed(api: MockApi, role: 'owner' | 'viewer' = 'owner') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const gateway = api.addProject(ws.id, 'Платёжный шлюз');
  const portal = api.addProject(ws.id, 'Портал');
  const done = api.addRun(gateway, { status: 'COMPLETED', objective: 'Исправить округление сумм', created_at: '2026-11-07T08:00:00Z' });
  const failed = api.addRun(portal, { status: 'FAILED', objective: 'Обновить зависимости портала', created_at: '2026-11-07T09:00:00Z' });
  const paused = api.addRun(portal, { status: 'PAUSED', objective: 'Документация API портала', created_at: '2026-11-07T10:00:00Z' });
  const running = api.addRun(gateway, { status: 'RUNNING', objective: 'Добавить экспорт отчётов в CSV', created_at: '2026-11-08T08:00:00Z' });
  // Чужой workspace: в списке не появляется.
  const other = api.addWorkspace('Команда Бета', 'beta', 'owner');
  api.addRun(api.addProject(other.id, 'Чужой'), { objective: 'Чужой запуск' });
  return { ws, gateway, portal, done, failed, paused, running };
}

test('runs: workspace list with status/project filters in the URL, pagination, links to run screens (light/dark axe)', async ({ page, api }) => {
  const { ws, gateway, portal, done, failed, running } = seed(api);
  const listUrls: URL[] = [];
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (u.pathname === '/api/v1/runs' && r.method() === 'GET') listUrls.push(u);
  });
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Процессы', exact: true }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/runs`);
  await expect(page.getByRole('heading', { level: 1, name: 'Процессы' })).toBeFocused();
  await expect(page).toHaveTitle('Процессы — AiCodingDev');

  const list = page.getByRole('list', { name: 'Список запусков' });
  await expect(list.locator(':scope > li')).toHaveCount(3);
  await expect(list).not.toContainText('Чужой запуск');
  const first = list.locator(`[data-run-id="${running.id}"]`);
  await expect(first).toContainText('Выполняется');
  await expect(first.getByRole('link', { name: 'Платёжный шлюз' })).toHaveAttribute('href', `/w/${ws.id}/projects/${gateway.id}/runs`);
  const base = `/w/${ws.id}/projects/${gateway.id}/runs/${running.id}`;
  await expect(first.getByRole('link', { name: /^Открыть run/ })).toHaveAttribute('href', base);
  await expect(first.getByRole('link', { name: /^Граф задач/ })).toHaveAttribute('href', `${base}/graph`);
  await expect(first.getByRole('link', { name: /^Live Agent Chat/ })).toHaveAttribute('href', `${base}/chat`);
  await expect(first.getByRole('link', { name: /^Качество/ })).toHaveAttribute('href', `/w/${ws.id}/quality/runs/${running.id}`);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `runs list (${scheme})`);
  }

  await page.getByRole('button', { name: 'Загрузить ещё' }).click();
  await expect(list.locator(':scope > li')).toHaveCount(4);
  await expect(page.getByRole('button', { name: 'Загрузить ещё' })).toHaveCount(0);
  await expect(list.locator(`[data-run-id="${done.id}"]`)).toBeVisible();

  // Фильтр статуса: повторяемый status уходит на сервер; фокус остаётся на select (смена query ≠ навигация).
  const status = page.getByLabel('Статус');
  await status.focus();
  await status.selectOption({ label: 'Завершённые' });
  await expect(page).toHaveURL(`/w/${ws.id}/runs?status=finished`);
  await expect(status).toBeFocused();
  await expect(list.locator(':scope > li')).toHaveCount(2);
  expect(listUrls.at(-1)?.searchParams.getAll('status')).toEqual(['COMPLETED', 'FAILED', 'CANCELLED']);
  expect(api.requests.filter((r) => r.operationId === 'listRuns').at(-1)?.headers['x-workspace-id']).toBe(ws.id);

  const project = page.getByLabel('Проект');
  await project.selectOption({ label: 'Портал' });
  await expect(page).toHaveURL(`/w/${ws.id}/runs?status=finished&project_id=${portal.id}`);
  await expect(list.locator(':scope > li')).toHaveCount(1);
  await expect(list.locator(`[data-run-id="${failed.id}"]`)).toContainText('Обновить зависимости портала');

  // Ссылка с фильтрами воспроизводится после перезагрузки.
  await page.reload();
  await expect(page.getByLabel('Статус')).toHaveValue('finished');
  await expect(page.getByLabel('Проект')).toHaveValue(portal.id);
  await expect(page.getByRole('list', { name: 'Список запусков' }).locator(':scope > li')).toHaveCount(1);

  await page.getByLabel('Статус').selectOption({ label: 'Выполняется' });
  await expect(page.getByTestId('empty-state')).toContainText('Нет запусков по фильтру');
  await expectNoSeriousA11yViolations(page, 'runs list empty (filtered)');
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/runs`);
  await expect(page.getByRole('list', { name: 'Список запусков' }).locator(':scope > li')).toHaveCount(3);

  // Переход в run: навигация run содержит ссылки на качество, соответствие и одобрения этого run.
  // F4: run открывается в AI Mission Control; переходы run — в Agent Inspector (контекстная панель ≥ 1366 px).
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.locator(`[data-run-id="${running.id}"]`).getByRole('link', { name: /^Открыть run/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/mission-control?run=${running.id}&view=system`);
  const runNav = page.getByTestId('context-panel').getByRole('navigation', { name: 'Разделы run' });
  await expect(runNav.getByRole('link', { name: 'Качество' })).toHaveAttribute('href', `/w/${ws.id}/quality/runs/${running.id}`);
  await expect(runNav.getByRole('link', { name: 'Соответствие' })).toHaveAttribute('href', `/w/${ws.id}/compliance/runs/${running.id}`);
  await expect(runNav.getByRole('link', { name: 'Одобрения' })).toHaveAttribute('href', `/w/${ws.id}/approvals?run_id=${running.id}`);
});

test('runs: empty workspace, error with retry and 360px without horizontal scroll', async ({ page, api }) => {
  const ws = api.addWorkspace('Пусто', 'empty', 'viewer');
  await login(page, api);
  // Первый запрос списка — 500 internal_error (тело — как у мока по контракту), затем — обычный мок.
  let fail = 1;
  await page.route(/\/api\/v1\/runs\?/, async (route) => {
    if (fail-- > 0) {
      const p = problem(500, 'internal_error');
      await route.fulfill({ status: p.status, contentType: 'application/problem+json', body: JSON.stringify(p.body) });
    } else await route.fallback();
  });
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(`/w/${ws.id}/runs`);
  await expect(page.getByRole('heading', { level: 1, name: 'Процессы' })).toBeFocused();
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('empty-state')).toContainText('Запусков пока нет');
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'runs list empty (360px)');
});
