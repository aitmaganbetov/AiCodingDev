import type { Page } from '@playwright/test';
import type { MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

function seed(api: MockApi) {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  const project = api.addProject(ws.id, 'Сервис');
  const run = api.addRun(project, { status: 'RUNNING', version: 2 });
  const task = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING' });
  const output = (n: number) =>
    api.addEvent(run, 'cli.output', {
      execution_id: '01900000-0000-7000-b000-000000000003',
      task_id: task.id,
      provider: 'codex',
      agent_role: 'developer',
      cli_seq: n,
      cli_event_type: 'output',
      stream: 'stdout',
      text: `строка ${n}`,
    });
  return { ws, project, run, output };
}

async function feed(page: Page) {
  return page
    .getByRole('list', { name: 'События run' })
    .getByRole('listitem')
    .evaluateAll((items) => items.map((li) => ({ seq: Number(li.getAttribute('data-seq')), id: li.getAttribute('data-event-id') })));
}

test('network drop (offline → online): stream pauses, resumes with Last-Event-ID, every event exactly once, no gaps', async ({
  page,
  api,
}) => {
  const { ws, project, run, output } = seed(api);
  for (let i = 1; i <= 4; i++) output(i);
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}/runs/${run.id}`);

  const items = page.getByRole('list', { name: 'События run' }).getByRole('listitem');
  await expect(items).toHaveCount(4);
  const state = page.getByTestId('stream-state');
  // Мок закрывает каждый ответ (как сервер по истечении lifetime) — open/reconnecting чередуются.
  await expect(state).not.toHaveAttribute('data-state', 'failed');

  // Сеть пропала.
  api.offline = true;
  await page.context().setOffline(true);
  await expect(state).toHaveAttribute('data-state', 'offline');
  await expect(state).toHaveText('Нет сети — поток событий возобновится при подключении.');
  const connectionsWhileOffline = api.sseConnections.length;

  // Пока клиент offline, run продолжает работу: 5..7 записаны в журнал.
  for (let i = 5; i <= 7; i++) output(i);
  await page.waitForTimeout(600);
  // Нет «долбёжки» сервера при отсутствии сети (поток приостановлен, а не в backoff).
  expect(api.sseConnections.length).toBe(connectionsWhileOffline);
  await expect(items).toHaveCount(4);

  // Сеть вернулась. Сервер (at-least-once) повторяет 3..4 и досылает 5..7.
  api.sseScripts = [{ fromSeq: 3 }];
  api.offline = false;
  await page.context().setOffline(false);

  await expect(items).toHaveCount(7);
  await expect(state).not.toHaveAttribute('data-state', 'failed');
  const resumed = api.sseConnections[connectionsWhileOffline];
  expect(resumed?.lastEventId).toBe('4');

  const shown = await feed(page);
  expect(shown.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  expect(new Set(shown.map((e) => e.id)).size).toBe(7);
  await expect(page.getByRole('list', { name: 'События run' })).toContainText('строка 7');

  // Live после восстановления: новое событие приходит в тот же поток без дублей.
  output(8);
  await expect(items).toHaveCount(8);
  expect((await feed(page)).map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
});

test('connection cut mid-frame while online: reconnect without duplicates or gaps (no offline event)', async ({ page, api }) => {
  const { ws, project, run, output } = seed(api);
  for (let i = 1; i <= 6; i++) output(i);
  // 1-е подключение: 1..2 + половина кадра 3, обрыв; 2-е: повтор 1..2 и остальное.
  api.sseScripts = [{ toSeq: 2, cut: true }, { fromSeq: 1 }];
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}/runs/${run.id}`);
  const items = page.getByRole('list', { name: 'События run' }).getByRole('listitem');
  await expect(items).toHaveCount(6);
  expect((await feed(page)).map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6]);
  expect(api.sseConnections[1]?.lastEventId).toBe('2');
});
