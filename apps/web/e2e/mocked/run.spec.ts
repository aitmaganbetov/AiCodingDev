import type { Page } from '@playwright/test';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { PASSWORD, type MockApi } from '../support/mockApi';
import { expect, test } from './fixtures';

async function login(page: Page, api: MockApi) {
  await page.goto('/');
  await page.getByLabel('Электронная почта').fill(api.user.email);
  await page.getByLabel('Пароль').fill(PASSWORD);
  await page.getByRole('button', { name: 'Войти' }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

/** Run в PLANNED с двумя задачами (B зависит от A) и журналом из 8 событий. */
function seedRun(api: MockApi) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'PLANNED' });
  const a = api.addTask(run, 'Спроектировать API', { agent_role: 'architect', status: 'READY' });
  const b = api.addTask(run, 'Реализовать экспорт', { status: 'PENDING', depends_on: [a.id] });
  const ev = (type: string, payload: Record<string, unknown>) => api.addEvent(run, type, payload);
  ev('run.state_changed', { from_status: 'PLANNED', to_status: 'RUNNING', run_version: 2 });
  ev('task.state_changed', { task_id: a.id, title: a.title, agent_role: 'architect', from_status: 'READY', to_status: 'RUNNING', task_version: 1, attempt_no: 1 });
  ev('agent.message', {
    message_id: '01900000-0000-7000-b000-000000000001',
    message_type: 'HANDOFF',
    from: { role: 'architect', provider: 'claude' },
    to: { role: 'developer' },
    correlation_id: '01900000-0000-7000-b000-000000000002',
    summary: 'Контракт API готов',
  });
  ev('cli.output', {
    execution_id: '01900000-0000-7000-b000-000000000003',
    task_id: a.id,
    provider: 'claude',
    agent_role: 'architect',
    cli_seq: 1,
    cli_event_type: 'output',
    stream: 'stdout',
    text: 'Пишу openapi.yaml',
  });
  ev('task.state_changed', { task_id: a.id, title: a.title, agent_role: 'architect', from_status: 'RUNNING', to_status: 'SUCCEEDED', task_version: 2, attempt_no: 1 });
  ev('task.state_changed', { task_id: b.id, title: b.title, agent_role: 'developer', from_status: 'PENDING', to_status: 'RUNNING', task_version: 1, attempt_no: 1 });
  ev('approval.requested', {
    approval_id: '01900000-0000-7000-b000-000000000004',
    action_type: 'MERGE',
    task_id: b.id,
    summary: 'Слияние ветки feature/export',
    required_role: 'admin',
    expires_at: '2026-11-09T09:00:00Z',
  });
  ev('gate.completed', {
    gate_execution_id: '01900000-0000-7000-b000-000000000005',
    gate_type: 'UNIT_TESTS',
    task_id: a.id,
    status: 'PASS',
    required: true,
    commit_sha: 'a'.repeat(40),
  });
  return { ws, project, run, a, b };
}

async function feedSeqs(page: Page): Promise<number[]> {
  return page
    .getByRole('list', { name: 'События run' })
    .getByRole('listitem')
    .evaluateAll((items) => items.map((li) => Number(li.getAttribute('data-seq'))));
}

test('run view: SSE events are shown; reconnect after a dropped connection uses Last-Event-ID, no duplicates, no gaps', async ({
  page,
  api,
}) => {
  const { ws, project, run } = seedRun(api);
  // 1-е подключение: события 1..3 и половина кадра 4, затем обрыв.
  // 2-е: «сервер» повторяет 2..3 (at-least-once) и досылает 4..8 — клиент обязан отбросить дубли.
  api.sseScripts = [{ toSeq: 3, cut: true }, { fromSeq: 2 }];

  await login(page, api);
  // После входа открывается Dashboard (P7-03); список проектов — отдельный раздел.
  await page.goto(`/w/${ws.id}/projects`);
  await page.getByRole('link', { name: 'Запуски проекта «Платёжный шлюз»' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/projects/${project.id}/runs`);
  await expect(page.getByRole('heading', { level: 1, name: 'Запуски · Платёжный шлюз' })).toBeFocused();
  await expect(page.getByRole('list', { name: 'Список запусков' }).getByRole('listitem')).toHaveCount(1);
  await expectNoSeriousA11yViolations(page, 'runs list');
  await page.getByRole('link', { name: /Открыть run/ }).click();

  await expect(page).toHaveURL(`/w/${ws.id}/projects/${project.id}/runs/${run.id}`);
  await expect(page.getByRole('heading', { level: 1, name: 'Run · Разработка (DEVELOP)' })).toBeVisible();

  const feed = page.getByRole('list', { name: 'События run' });
  await expect(feed.getByRole('listitem')).toHaveCount(8);
  expect(await feedSeqs(page)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  await expect(feed).toContainText('Архитектор → Разработчик: Контракт API готов');
  await expect(feed).toContainText('Пишу openapi.yaml');
  await expect(feed).toContainText('«Реализовать экспорт»: Ожидает → Выполняется');

  // Реконнект: второе подключение с Last-Event-ID последнего целого кадра.
  expect(api.sseConnections[0]).toEqual({ lastEventId: undefined, afterSeq: null });
  expect(api.sseConnections[1]?.lastEventId).toBe('3');
  await expect.poll(() => api.sseConnections[2]?.lastEventId).toBe('8');
  const stream = api.requests.find((r) => r.operationId === 'streamRunEvents');
  expect(stream?.headers.accept).toBe('text/event-stream');

  // События state_changed → перечитаны run и задачи.
  await expect(page.locator('[data-status]').first()).toHaveText('Выполняется');
  await expect(page.getByTestId('run-pending-approvals')).toHaveText('1');
  const tasks = page.getByRole('table', { name: 'Задачи run (план v1)' });
  await expect(tasks.getByRole('row', { name: /^Спроектировать API/ })).toContainText('Успешно');
  const rowB = tasks.getByRole('row', { name: /^Реализовать экспорт/ });
  await expect(rowB).toContainText('Выполняется');
  await expect(rowB).toContainText('Спроектировать API'); // зависит от
  await expect(rowB).toContainText('1 из 3');
  // Между подключениями «Связь потеряна…» допустимо; фатальной ошибки быть не должно.
  await expect(page.getByTestId('stream-state')).not.toHaveAttribute('data-state', 'failed');
  await expectNoSeriousA11yViolations(page, 'run view');

  // Пауза: команда 202, переход приходит событием по SSE (seq 9) — кнопка меняется на «Возобновить».
  await page.getByRole('button', { name: 'Приостановить' }).click();
  await expect(page.getByRole('button', { name: 'Возобновить' })).toBeVisible();
  await expect(feed.getByRole('listitem')).toHaveCount(9);
  expect(await feedSeqs(page)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
  await expect(page.locator('[data-status]').first()).toHaveText('Приостановлен');
  const pause = api.requests.find((r) => r.operationId === 'pauseRun');
  expect(pause?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
  expect(pause?.headers['if-match']).toBe('"v2"');

  // Никаких повторов в DOM за всё время.
  const ids = await feed.getByRole('listitem').evaluateAll((items) => items.map((li) => li.getAttribute('data-event-id')));
  expect(new Set(ids).size).toBe(ids.length);
});

test('run view: 410 replay_window_exceeded → state reread, feed continues from last_seq', async ({ page, api }) => {
  const { ws, project, run } = seedRun(api);
  api.sseScripts = [{ status: 410 }];
  // Retention удалил историю: REST уже видит итоговое состояние.
  run.status = 'RUNNING';
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}/runs/${run.id}`);

  await expect(page.getByText('Часть истории событий вне окна хранения')).toBeVisible();
  await expect.poll(() => api.sseConnections[1]).toEqual({ lastEventId: undefined, afterSeq: '8' });
  await page.getByRole('button', { name: 'Отменить run' }).click();
  await page.getByRole('button', { name: 'Да, отменить' }).click();
  const feed = page.getByRole('list', { name: 'События run' });
  await expect(feed.getByRole('listitem')).toHaveCount(1);
  expect(await feedSeqs(page)).toEqual([9]);
  await expect(page.locator('[data-status]').first()).toHaveText('Отменён');
  await expect(page.getByRole('button', { name: 'Отменить run' })).toBeDisabled();
});

test('start run form: creates a run and opens its page; viewer cannot start or control', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  const project = api.addProject(ws.id, 'Сервис');
  const ro = api.addWorkspace('Только чтение', 'ro', 'viewer');
  const roProject = api.addProject(ro.id, 'Чужой сервис');
  const roRun = api.addRun(roProject);
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}/runs`);
  await expect(page.getByTestId('empty-state')).toBeVisible();

  await page.getByRole('button', { name: 'Запустить' }).click();
  await expect(page.getByLabel('Цель')).toHaveAttribute('aria-invalid', 'true');
  await expectNoSeriousA11yViolations(page, 'start run (validation)');

  await page.getByLabel('Режим').selectOption('DEBUG');
  await page.getByLabel('Цель').fill('Найти утечку памяти');
  await page.getByRole('button', { name: 'Запустить' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Run · Отладка (DEBUG)' })).toBeVisible();
  await expect(page.getByText('Событий пока нет.')).toBeVisible();
  await expect(page.getByText('Задач пока нет — план ещё не построен.')).toBeVisible();
  const create = api.requests.find((r) => r.operationId === 'createRun');
  expect(create?.body).toEqual({
    project_id: project.id,
    mode: 'DEBUG',
    objective: 'Найти утечку памяти',
    approval_policy: 'risk_based',
  });

  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(`/w/${ro.id}/projects/${roProject.id}/runs/${roRun.id}`);
  await expect(page.getByRole('heading', { level: 1, name: 'Run · Разработка (DEVELOP)' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Приостановить' })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
  await page.goto(`/w/${ro.id}/projects/${roProject.id}/runs`);
  await expect(page.getByRole('list', { name: 'Список запусков' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Запустить' })).toHaveCount(0);
});
