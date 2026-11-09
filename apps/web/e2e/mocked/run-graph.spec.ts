/**
 * P7-05 Run Graph → с F4 Workflow Navigator AI Mission Control: DAG на @xyflow/react, живые статусы по SSE,
 * текущий исполнитель, доступная альтернатива (таблица того же графа), клавиатура и axe. Прежний путь
 * `…/runs/:runId/graph` перенаправляется в Mission Control.
 */
import type { Page } from '@playwright/test';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import type { MockApi } from '../support/mockApi';
import { focusDocumentStart, login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

function seed(api: MockApi) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'developer');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'RUNNING', version: 3 });
  const a = api.addTask(run, 'Спроектировать API', { agent_role: 'architect', status: 'SUCCEEDED', attempt_count: 1, current_provider: 'claude' });
  const b = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING', depends_on: [a.id], attempt_count: 1, current_provider: 'codex', version: 2 });
  const c = api.addTask(run, 'Проверить экспорт', { agent_role: 'qa', status: 'PENDING', depends_on: [b.id] });
  const url = `/w/${ws.id}/projects/${project.id}/runs/${run.id}/graph`;
  return { ws, project, run, a, b, c, url };
}

function trackExternalRequests(page: Page): string[] {
  const external: string[] = [];
  page.on('request', (r) => {
    const u = new URL(r.url());
    if (u.hostname !== '127.0.0.1' && u.protocol !== 'data:' && u.protocol !== 'blob:') external.push(r.url());
  });
  return external;
}

test('run graph: DAG with statuses and current executor, live update via SSE, axe', async ({ page, api }) => {
  const { run, c, url } = seed(api);
  const external = trackExternalRequests(page);
  await login(page, api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto(url);

  await expect(page).toHaveURL(new RegExp(`/mission-control\\?run=${run.id}$`));
  await expect(page.getByRole('heading', { level: 1, name: 'AI Mission Control' })).toBeVisible();
  await expect(page.getByTestId('mc-navigator').getByRole('heading', { name: 'Навигатор процесса' })).toBeVisible();
  const canvas = page.getByTestId('run-graph-canvas');
  await expect(canvas.locator('.react-flow__node')).toHaveCount(3);
  await expect(canvas.locator('.react-flow__edge')).toHaveCount(2);
  await expect(canvas.locator(`[data-task-id="${c.id}"]`)).toHaveAttribute('data-status', 'PENDING');
  await expect(page.getByTestId('current-executors')).toContainText('Разработчик · Codex');
  await expect(page.getByTestId('stream-state')).toHaveAttribute('data-state', /open|connecting/);
  await expectNoSeriousA11yViolations(page, 'run graph');

  // Live: задача QA начинает выполняться у Claude — узел и «Сейчас выполняют» обновляются без перезагрузки.
  api.addEvent(run, 'task.state_changed', {
    task_id: c.id,
    title: c.title,
    agent_role: 'qa',
    from_status: 'PENDING',
    to_status: 'RUNNING',
    task_version: 1,
    attempt_no: 1,
    provider: 'claude',
  });
  await expect(canvas.locator(`[data-task-id="${c.id}"]`)).toHaveAttribute('data-status', 'RUNNING');
  await expect(canvas.locator(`[data-task-id="${c.id}"]`)).toContainText('Claude');
  await expect(canvas.locator(`[data-task-id="${c.id}"]`)).toHaveClass(/aicd-node-active/);
  await expect(page.getByTestId('current-executors')).toContainText('QA · Claude');
  await expect(page.getByTestId('graph-announcement')).toHaveText('«Проверить экспорт»: Ожидает → Выполняется');
  expect(external, 'no CDN/external requests').toEqual([]);
});

test('run graph: accessible list view of the same DAG — keyboard only', async ({ page, api }) => {
  const { a, b, c, url, ws, project, run } = seed(api);
  await login(page, api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await page.goto(url);
  await expect(page.getByTestId('run-graph-canvas').locator('.react-flow__node')).toHaveCount(3);

  await focusDocumentStart(page);
  const listButton = page.getByRole('button', { name: 'Список' });
  await tabTo(page, listButton, { max: 60 });
  await page.keyboard.press('Enter');
  await expect(listButton).toHaveAttribute('aria-pressed', 'true');
  await expect(page).toHaveURL(/nav=list/);
  await expect(listButton).toBeFocused(); // смена вида не уводит фокус на h1

  const table = page.getByRole('table', { name: /Задачи run и их зависимости/ });
  await expect(table.getByRole('row')).toHaveCount(4);
  const rowB = table.locator(`tr[data-task-id="${b.id}"]`);
  await expect(rowB).toContainText('Разработчик · Codex');
  await expect(rowB).toContainText(a.title);
  await expect(table.locator(`tr[data-task-id="${c.id}"]`)).toContainText(b.title);
  const link = table.getByRole('link', { name: new RegExp(`^Карточка задачи\\s*: ${b.title}$`) });
  await expect(link).toHaveAttribute('href', `/w/${ws.id}/projects/${project.id}/runs/${run.id}/tasks/${b.id}`);
  await tabTo(page, link, { max: 40 });
  await expectNoSeriousA11yViolations(page, 'run graph list view');

  // Тёмная тема — тот же граф.
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await page.getByRole('button', { name: 'Граф' }).click();
  await expect(page.getByTestId('run-graph-canvas').locator('.react-flow__node')).toHaveCount(3);
  await expectNoSeriousA11yViolations(page, 'run graph dark');
});

test('run graph: empty plan and load error states', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'viewer');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'PLANNED' });
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}/runs/${run.id}/graph`);
  await expect(page.getByRole('heading', { name: 'Граф пока пуст' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'run graph empty');

  await page.goto(`/w/${ws.id}/projects/${project.id}/runs/01900000-0000-7000-8000-00000000dead/graph`);
  await expect(page.getByTestId('error-state')).toContainText('Не найдено или нет доступа.');
});
