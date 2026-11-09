import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/** P8-07 Analytics: KPI, диаграмма с табличной альтернативой, фильтры в URL, пустое/ошибочное состояния; axe light/dark. */
type S = components['schemas'];

function summary(projectId: string | null, empty = false): S['AnalyticsSummary'] {
  const stats = { p50: 120, p90: 600, avg: 200 };
  const none = { p50: null, p90: null, avg: null };
  return {
    window: { from: '2026-10-09T13:00:00Z', to: '2026-11-08T13:00:00Z' },
    project_id: projectId,
    runs: {
      total: empty ? 0 : 5,
      finished: empty ? 0 : 4,
      completed: empty ? 0 : 3,
      success_rate: empty ? null : 0.75,
      by_status: empty ? {} : { COMPLETED: 3, FAILED: 1, RUNNING: 1 },
      duration_seconds: empty ? none : stats,
      by_mode: empty ? [] : [{ mode: 'DEVELOP', total: 5, finished: 4, completed: 3, success_rate: 0.75, avg_duration_seconds: 300 }],
    },
    daily: empty
      ? []
      : [
          { date: '2026-11-05', runs_created: 1, runs_completed: 1, runs_failed: 0, runs_cancelled: 0 },
          { date: '2026-11-06', runs_created: 3, runs_completed: 2, runs_failed: 1, runs_cancelled: 0 },
          { date: '2026-11-07', runs_created: 1, runs_completed: 0, runs_failed: 0, runs_cancelled: 0 },
        ],
    tasks: {
      attempts: empty ? 0 : 8,
      succeeded: empty ? 0 : 6,
      failed: empty ? 0 : 2,
      retries: empty ? 0 : 2,
      verified_completion_rate: empty ? null : 0.857,
      tasks_finished: empty ? 0 : 6,
      latency_seconds: empty ? none : stats,
      by_role: empty
        ? []
        : [
            { role: 'developer', attempts: 5, succeeded: 4, failed: 1, retries: 1, verified_completion_rate: 0.8 },
            { role: 'reviewer', attempts: 3, succeeded: 2, failed: 1, retries: 1, verified_completion_rate: 1 },
          ],
    },
    gates: empty
      ? []
      : [
          { gate_type: 'LINT', total: 4, passed: 4, passed_verified: 4, passed_unverified: 0, failed: 0, errored: 0, skipped: 0, pass_rate: 1, avg_duration_seconds: 3 },
          { gate_type: 'UNIT', total: 4, passed: 3, passed_verified: 3, passed_unverified: 0, failed: 1, errored: 0, skipped: 1, pass_rate: 0.75, avg_duration_seconds: 40 },
        ],
    providers: empty
      ? []
      : [
          {
            provider: 'claude',
            executions: 3,
            collected: 3,
            errors: 0,
            rate_limited: 0,
            auth_required: 0,
            cancelled: 0,
            in_progress: 0,
            success_rate: 1,
            avg_duration_seconds: 80,
            failures_by_category: {},
            tokens: { total: 15200, source: 'cli_reported', executions_reported: 3 },
          },
          {
            provider: 'codex',
            executions: 5,
            collected: 3,
            errors: 1,
            rate_limited: 1,
            auth_required: 0,
            cancelled: 0,
            in_progress: 0,
            success_rate: 0.6,
            avg_duration_seconds: 95,
            failures_by_category: { RATE_LIMITED: 1, PROCESS_FAILED: 1 },
            tokens: { total: null, source: 'not_exposed_by_cli', executions_reported: 0 },
          },
        ],
    review_loops: {
      review_requests: empty ? 0 : 3,
      fix_requests: empty ? 0 : 2,
      review_chains: empty ? 0 : 2,
      chains_with_fixes: empty ? 0 : 1,
      avg_fix_rounds: empty ? null : 1,
      max_fix_rounds: empty ? 0 : 2,
      loops_exhausted: 0,
    },
    agent_messages: empty ? { total: 0, by_type: {} } : { total: 9, by_type: { REVIEW_REQUEST: 3, FIX_REQUEST: 2, TASK_COMPLETED: 4 } },
    approvals: { requested: empty ? 0 : 2, pending: empty ? 0 : 1, by_status: empty ? {} : { APPROVED: 1, PENDING: 1 }, wait_seconds: empty ? none : { p50: 90, p90: 4000, avg: 2000 } },
    context: {
      packs: empty ? 0 : 4,
      avg_size_bytes: empty ? null : 20480,
      max_size_bytes: empty ? null : 40000,
      fragments: empty ? 0 : 40,
      stale_fragment_ratio: empty ? null : 0.1,
      usefulness: { rate: empty ? null : 0.25, packs_evaluated: empty ? 0 : 3, fragments_referenced: empty ? 0 : 6, fragments_total: empty ? 0 : 24, truncated: false },
    },
  };
}

function mockAnalytics(api: MockApi, emptyProject: string | null = null): URLSearchParams[] {
  const seen: URLSearchParams[] = [];
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', ({ url }) => {
    seen.push(url.searchParams);
    const project = url.searchParams.get('project_id');
    return { status: 200, body: summary(project, project !== null && project === emptyProject) };
  });
  return seen;
}

test('analytics: KPIs, chart + table alternative, filters in URL, empty project (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'viewer');
  const p1 = api.addProject(ws.id, 'Платёжный сервис');
  const p2 = api.addProject(ws.id, 'Пустой проект');
  const seen = mockAnalytics(api, p2.id);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Аналитика' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Аналитика' })).toBeFocused();
  await expect(page.getByTestId('kpi-success')).toContainText('75%');
  await expect(page.getByTestId('kpi-vcr')).toContainText('85,7%');
  await expect(page.getByRole('img', { name: /всего 5, успешно 3, с ошибкой 1, отменено 0/ })).toBeVisible();
  const providers = page.getByRole('table', { name: /CLI-провайдеров/ });
  await expect(providers).toContainText('неизвестно (CLI не сообщает)');
  await expect(providers).toContainText('15 200');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `analytics (${scheme})`);
  }

  // Табличная альтернатива диаграммы открывается с клавиатуры.
  const summaryToggle = page.getByText('Показать данные диаграммы таблицей');
  await summaryToggle.focus();
  await page.keyboard.press('Enter');
  const table = page.getByRole('table', { name: /Запуски по дням/ });
  await expect(table.getByRole('row')).toHaveCount(4);
  await expectNoSeriousA11yViolations(page, 'analytics chart table');

  // Фильтры — в URL и в запросе; пустой проект — пустое состояние.
  await page.getByLabel('Период').selectOption('7d');
  await expect(page).toHaveURL(`/w/${ws.id}/analytics?period=7d`);
  // Дождаться, пока экран применит новый период (контролируемый select после transition роутера),
  // и только затем менять проект: иначе изменение может прийтись на промежуточный рендер.
  await expect(page.getByLabel('Период')).toHaveValue('7d');
  await expect(page.getByLabel('Проект')).toBeEnabled();
  await page.getByLabel('Проект').selectOption(p2.id);
  await expect(page.getByLabel('Проект')).toHaveValue(p2.id);
  await expect(page).toHaveURL(`/w/${ws.id}/analytics?period=7d&project=${p2.id}`);
  await expect(page.getByText('За период нет запусков')).toBeVisible();
  const last = api.requests.filter((r) => r.operationId === 'getAnalyticsSummary').at(-1);
  expect(last?.headers['x-workspace-id']).toBe(ws.id);
  expect(seen.at(-1)?.get('project_id')).toBe(p2.id);
  expect(seen.at(-1)?.get('from')).toMatch(/Z$/);
  await expectNoSeriousA11yViolations(page, 'analytics empty');

  await page.reload();
  await expect(page.getByLabel('Период')).toHaveValue('7d');
  await page.getByLabel('Проект').selectOption(p1.id);
  await expect(page.getByTestId('kpi-success')).toContainText('75%');
});

test('analytics: error state with retry and 360px without horizontal scroll', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ws = api.addWorkspace('Команда', 'team', 'developer');
  let fail = true;
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', ({ url }) =>
    fail ? problem(500, 'internal_error') : { status: 200, body: summary(url.searchParams.get('project_id')) },
  );
  await login(page, api);
  await page.goto(`/w/${ws.id}/analytics`);
  const error = page.getByTestId('error-state');
  await expect(error).toContainText('Не удалось загрузить аналитику');
  await expectNoSeriousA11yViolations(page, 'analytics error 360px');
  fail = false;
  await error.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('kpi-success')).toContainText('75%');
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'analytics 360px');
});
