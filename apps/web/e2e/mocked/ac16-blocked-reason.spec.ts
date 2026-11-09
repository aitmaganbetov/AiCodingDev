import { problem } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/**
 * AC-16, условие «Причина видна в API и UI»: run и задача, заблокированные из-за недоступной CLI-авторизации
 * (BLOCKED(AUTH_REQUIRED)) или неподходящего провайдера (BLOCKED(PROVIDER_UNAVAILABLE)), показывают
 * человекочитаемую причину блокировки на всех экранах, где пользователь видит этот run/задачу:
 * «Процессы», AI Mission Control (инспектор run и инспектор задачи) и карточка задачи.
 * API-часть условия доказывает tests/acceptance/test_ac03_ac16_routing_without_ai_api.py.
 */
const CASES = [
  { reason: 'AUTH_REQUIRED', label: 'Требуется аутентификация провайдера' },
  { reason: 'PROVIDER_UNAVAILABLE', label: 'Провайдер CLI недоступен' },
] as const;

for (const { reason, label } of CASES) {
  test(`AC-16: blocked reason ${reason} is visible in Workflows, Mission Control and Task Detail`, async ({ page, api }) => {
    await page.setViewportSize({ width: 1366, height: 900 });
    const ws = api.addWorkspace('Команда Альфа', 'alpha', 'developer');
    const project = api.addProject(ws.id, 'Платёжный шлюз');
    const run = api.addRun(project, {
      status: 'BLOCKED',
      blocked_reason: reason,
      objective: 'Обновить зависимости шлюза',
      created_at: '2026-11-08T07:00:00Z',
      started_at: '2026-11-08T07:00:05Z',
      pending_approvals: 0,
    });
    const task = api.addTask(run, 'Обновление зависимостей', { status: 'BLOCKED', blocked_reason: reason });
    // Операции задачи (инспектор Mission Control и карточка задачи) — ответы сверяются с контрактом.
    api.route('GET', '/api/v1/tasks/{id}', 'getTask', ({ id }) => {
      if (id !== task.id) return problem(404, 'not_found');
      const full = {
        ...task,
        instructions: 'Обновить зависимости.',
        acceptance_criteria: [],
        review_loop_count: 0,
        next_attempt_at: null,
        timeout_seconds: null,
        created_at: '2026-11-08T07:00:05Z',
        links: {},
      };
      return { status: 200, body: full, headers: { ETag: `"v${task.version}"` } };
    });
    api.route('GET', '/api/v1/tasks/{id}/attempts', 'listTaskAttempts', () => ({ status: 200, body: { items: [], next_cursor: null } }));
    api.route('GET', '/api/v1/tasks/{id}/evidence', 'listTaskEvidence', () => ({ status: 200, body: { items: [], next_cursor: null } }));
    api.route('GET', '/api/v1/tasks/{id}/messages', 'listTaskMessages', () => ({ status: 200, body: { items: [], next_cursor: null } }));
    await login(page, api);

    // «Процессы»: причина блокировки на карточке процесса.
    await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Процессы', exact: true }).click();
    const card = page.getByRole('list', { name: 'Список запусков' }).locator(`[data-run-id="${run.id}"]`);
    await expect(card.getByTestId('workflow-blocked')).toContainText(`Причина блокировки: ${label}`);

    // AI Mission Control: инспектор run показывает причину.
    await page.goto(`/w/${ws.id}/mission-control?run=${run.id}`);
    await expect(page.getByTestId('mc-layout')).toBeVisible();
    await expect(page.getByTestId('run-blocked-reason')).toHaveText(label);

    // AI Mission Control: инспектор выбранной задачи показывает причину среди ошибок.
    await page.goto(`/w/${ws.id}/mission-control?run=${run.id}&task=${task.id}`);
    const inspector = page.getByTestId('mc-task-inspector');
    await expect(inspector).toHaveAttribute('data-task-id', task.id);
    await expect(inspector).toContainText(label);

    // Карточка задачи.
    await page.goto(`/w/${ws.id}/projects/${project.id}/runs/${run.id}/tasks/${task.id}`);
    await expect(page.getByRole('main')).toContainText(label);
  });
}
