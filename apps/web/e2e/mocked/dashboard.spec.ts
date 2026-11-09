import type { Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { emptyAnalyticsSummary, problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

/**
 * F3 Bento Dashboard (план §3.2, §7.1): стартовый экран; плитки только из реальных операций
 * (listProjects, listRuns + getRun, GET /tasks, GET /analytics/summary, GET /findings,
 * listApprovals, listProviders, GET /activity, GET /system/health). Все ответы мока валидируются
 * по контракту (fixtures.ts). Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */

const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

function provider(code: 'codex' | 'claude', auth: S['AuthStatus']): S['Provider'] {
  return {
    provider_id: code === 'codex' ? '01900000-0000-7000-b000-000000000001' : '01900000-0000-7000-b000-000000000002',
    code,
    display_name: code === 'codex' ? 'Codex CLI' : 'Claude Code CLI',
    enabled_globally: true,
    installations: [
      {
        installation_id: code === 'codex' ? '01900000-0000-7000-b000-000000000011' : '01900000-0000-7000-b000-000000000012',
        host: 'worker-exec-1',
        detected_version: '0.42.0',
        compatibility: 'SUPPORTED',
        auth_status: auth,
        availability: 'AVAILABLE',
        capabilities: {},
        last_error_category: null,
        checked_at: '2026-11-08T08:00:00Z',
      },
    ],
    workspace_settings: { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, allowed_roles: [], settings_version: 1 },
  };
}

function analytics(): S['AnalyticsSummary'] {
  const base = emptyAnalyticsSummary();
  return {
    ...base,
    runs: { ...base.runs, total: 6, finished: 5, completed: 4, success_rate: 0.8 },
    daily: [
      { date: '2026-11-05', runs_created: 1, runs_completed: 1, runs_failed: 0, runs_cancelled: 0 },
      { date: '2026-11-06', runs_created: 2, runs_completed: 1, runs_failed: 1, runs_cancelled: 0 },
      { date: '2026-11-07', runs_created: 3, runs_completed: 2, runs_failed: 0, runs_cancelled: 0 },
    ],
    tasks: {
      ...base.tasks,
      attempts: 14,
      succeeded: 11,
      failed: 3,
      retries: 2,
      verified_completion_rate: 0.917,
      tasks_finished: 14,
      by_role: [
        { role: 'developer', attempts: 8, succeeded: 7, failed: 1, retries: 1, verified_completion_rate: 0.875 },
        { role: 'reviewer', attempts: 6, succeeded: 4, failed: 2, retries: 1, verified_completion_rate: null },
      ],
    },
    gates: [
      { gate_type: 'UNIT', total: 7, passed: 5, passed_verified: 4, passed_unverified: 1, failed: 1, errored: 0, skipped: 1, pass_rate: 0.833, avg_duration_seconds: 20 },
      { gate_type: 'SAST', total: 3, passed: 1, passed_verified: 0, passed_unverified: 1, failed: 0, errored: 1, skipped: 1, pass_rate: 0.5, avg_duration_seconds: 40 },
    ],
    providers: [
      {
        provider: 'codex',
        executions: 12,
        collected: 11,
        errors: 1,
        rate_limited: 0,
        auth_required: 0,
        cancelled: 0,
        in_progress: 1,
        success_rate: 0.917,
        avg_duration_seconds: 60,
        failures_by_category: {},
        tokens: { total: null, source: 'not_exposed_by_cli', executions_reported: 0 },
      },
    ],
    review_loops: { review_requests: 5, fix_requests: 2, review_chains: 5, chains_with_fixes: 2, avg_fix_rounds: 1, max_fix_rounds: 1, loops_exhausted: 0 },
  };
}

const HEALTH: S['SystemHealth'] = {
  status: 'degraded',
  checked_at: '2026-11-08T08:59:00Z',
  cache_ttl_seconds: 10,
  components: [
    { name: 'api', status: 'ok', checked_at: '2026-11-08T08:59:00Z', latency_ms: 1, measurements: {}, detail: null },
    { name: 'database', status: 'ok', checked_at: '2026-11-08T08:59:00Z', latency_ms: 4, measurements: { revision: '0018' }, detail: null },
    { name: 'workers', status: 'degraded', checked_at: '2026-11-08T08:59:00Z', latency_ms: null, measurements: {}, detail: 'нет живого воркера verify' },
    { name: 'backup', status: 'unknown', checked_at: '2026-11-08T08:59:00Z', latency_ms: null, measurements: {}, detail: 'файл статуса бэкапа не смонтирован' },
  ],
  worker_hosts: [],
  worker_kinds: [
    { kind: 'exec', status: 'ok', expected: true, hosts: 1, active_hosts: 1, freshest_heartbeat_age_seconds: 12, heartbeat_interval_seconds: 30 },
    { kind: 'verify', status: 'down', expected: true, hosts: 1, active_hosts: 0, freshest_heartbeat_age_seconds: 600, heartbeat_interval_seconds: 30 },
    { kind: 'index', status: 'unknown', expected: false, hosts: 0, active_hosts: 0, freshest_heartbeat_age_seconds: null, heartbeat_interval_seconds: null },
  ],
  queues: [{ queue: 'aicd.exec', ready: 2, delayed: 0, dead_letters: 0 }],
  backups: null,
};

function activityBody(runId: string, taskId: string | null = null): S['WorkspaceActivityPage'] {
  return {
    items: [
      {
        id: '01900000-0000-7000-f000-000000000003',
        run_id: runId,
        task_id: taskId,
        seq: 3,
        event_type: 'gate.completed',
        payload: { gate_execution_id: '01900000-0000-7000-f100-000000000001', gate_type: 'UNIT', status: 'PASS', required: true, commit_sha: 'a1b2c3d', evidence_id: null },
        occurred_at: '2026-11-08T08:40:00Z',
      },
      {
        id: '01900000-0000-7000-f000-000000000002',
        run_id: runId,
        task_id: taskId,
        seq: 2,
        event_type: 'task.state_changed',
        payload: { task_id: taskId, title: 'Спроектировать экспорт', agent_role: 'architect', from_status: 'VERIFYING', to_status: 'SUCCEEDED', task_version: 3 },
        occurred_at: '2026-11-08T08:35:00Z',
      },
      {
        id: '01900000-0000-7000-f000-000000000001',
        run_id: runId,
        seq: 1,
        event_type: 'run.state_changed',
        payload: { from_status: 'PLANNED', to_status: 'RUNNING', run_version: 2 },
        occurred_at: '2026-11-08T08:01:00Z',
      },
    ],
    next_cursor: null,
  };
}

/** Workspace с данными: проект, активный run с задачами, исполняющиеся задачи, находки, approvals, события. */
function seed(api: MockApi, role: S['WorkspaceRole'] = 'owner') {
  api.pageSize = 50;
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const pay = api.addProject(ws.id, 'Платёжный шлюз');
  api.addProject(ws.id, 'CRM');
  const run = api.addRun(pay, { objective: 'Экспорт отчётов в CSV', status: 'RUNNING', created_at: '2026-11-08T08:00:00Z' });
  api.addRun(pay, { objective: 'Обновить зависимости', status: 'COMPLETED', created_at: '2026-11-05T08:00:00Z' });
  const t1 = api.addTask(run, 'Спроектировать экспорт', { agent_role: 'architect', status: 'SUCCEEDED' });
  api.addTask(run, 'Реализовать экспорт', { agent_role: 'developer', status: 'RUNNING', current_provider: 'codex', attempt_count: 1 });
  api.addTask(run, 'Ревью', { agent_role: 'reviewer', status: 'PENDING' });
  api.addTask(run, 'Тесты', { agent_role: 'qa', status: 'SUCCEEDED' });
  const running = (api.tasks.get(run.id) ?? []).filter((t) => t.status === 'RUNNING' || t.status === 'LEASED');

  api.route('GET', '/api/v1/tasks', 'listTasks', ({ url }) => {
    const statuses = url.searchParams.getAll('status');
    const items: S['WorkspaceTask'][] = running
      .filter((t) => statuses.length === 0 || statuses.includes(t.status))
      .map((t) => ({
        ...t,
        project_id: pay.id,
        run_status: run.status,
        created_at: '2026-11-08T08:01:00Z',
        current_attempt: { id: '01900000-0000-7000-c000-000000000001', attempt_no: 1, status: 'RUNNING', provider: 'codex', started_at: '2026-11-08T08:02:00Z', finished_at: null },
      }));
    return { status: 200, body: { items, next_cursor: null } };
  });
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', () => ({ status: 200, body: analytics() }));
  api.route('GET', '/api/v1/findings', 'listFindings', () => ({
    status: 200,
    body: {
      items: [
        { id: '01900000-0000-7000-d000-000000000001', severity: 'critical', rule_id: 'sql-injection', title: 'SQL-инъекция', status: 'OPEN', run_id: run.id },
        { id: '01900000-0000-7000-d000-000000000002', severity: 'high', rule_id: 'xss', title: 'XSS', status: 'OPEN', run_id: run.id },
        { id: '01900000-0000-7000-d000-000000000003', severity: 'medium', rule_id: 'weak-hash', title: 'Слабый хэш', status: 'OPEN', run_id: run.id },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/approvals', 'listApprovals', () => ({
    status: 200,
    body: {
      items: [
        {
          id: '01900000-0000-7000-e000-000000000001',
          workspace_id: ws.id,
          run_id: run.id,
          action_type: 'MERGE',
          status: 'PENDING',
          risk_level: 'high',
          summary: 'Слияние ветки экспорта',
          requested_by: { actor_type: 'agent', role: 'devops' },
          can_decide: true,
          created_at: '2026-11-08T08:30:00Z',
          version: 0,
        },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({ status: 200, body: { items: [provider('codex', 'AUTHENTICATED'), provider('claude', 'AUTH_REQUIRED')] } }));
  api.route('GET', '/api/v1/activity', 'listActivity', () => ({ status: 200, body: activityBody(run.id, t1.id) }));
  api.route('GET', '/api/v1/system/health', 'getSystemHealth', () => ({ status: 200, body: HEALTH }));
  return { ws, pay, run, t1 };
}

test('happy path: hero, 8 KPI from real operations with sources, sections, D-6/D-8, axe light/dark', async ({ page, api }) => {
  await page.setViewportSize({ width: 1920, height: 1200 });
  const { ws, pay, run } = seed(api);
  await login(page, api);
  await expect(page).toHaveURL(`/w/${ws.id}/dashboard`);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Команда Альфа' })).toBeFocused();
  await expect(page.getByTestId('hero-title')).toHaveText('Welcome to AiCodingDev');
  await expect(page.getByText('Your AI-powered software engineering command center.')).toHaveAttribute('lang', 'en');

  await expect(page.getByTestId('metric-projects')).toContainText('2');
  await expect(page.getByRole('region', { name: 'Активные проекты' })).toContainText('С незавершённым запуском: 1');
  const agents = page.getByRole('region', { name: 'Работающие AI-агенты' });
  await expect(agents.getByTestId('metric-agents')).toContainText('1');
  await expect(agents).toContainText('Codex CLI: 1');
  await expect(page.getByTestId('metric-completed')).toContainText('11');
  const quality = page.getByRole('region', { name: 'Качество (измеренные доли)' });
  await expect(quality.getByTestId('metric-verified-rate')).toContainText('91,7%');
  // Гейты (D-8): «Пройдено» — PASS с VERIFIED evidence, 4 из 8 исполненных (SKIPPED не в знаменателе) = 50 %.
  await expect(quality.getByTestId('metric-gate-pass-verified')).toContainText('50%');
  await expect(quality.getByTestId('metric-gate-pass-verified')).toContainText('недостаточно доказательств: 2');
  await expect(quality).toContainText('Интегральная оценка качества не вычисляется');
  await expect(page.getByText(/quality score/i)).toHaveCount(0);
  const security = page.getByRole('region', { name: 'Находки безопасности' });
  await expect(security.getByTestId('metric-findings')).toContainText('3');
  await expect(security.locator('[data-severity-count="critical"]')).toContainText('1');
  const approvals = page.getByRole('region', { name: 'Ожидают одобрения' });
  await expect(approvals.getByTestId('metric-approvals')).toContainText('1');
  await expect(approvals).toContainText('Высокий или критический риск: 1');
  const providers = page.getByRole('region', { name: 'Состояние CLI-провайдеров' });
  await expect(providers.getByTestId('metric-providers')).toContainText('1 из 2');
  await expect(providers.locator('[data-provider-row="claude"]')).toContainText('требуется вход');
  await expect(providers.locator('[data-provider-row="codex"]')).toContainText('Успешных исполнений: 91,7% (12 исп.)');
  const success = page.getByRole('region', { name: 'Успешность процессов' });
  await expect(success.getByTestId('metric-run-success')).toContainText('80%');
  await expect(success.getByRole('img', { name: /Успешно завершённые запуски по дням: за 3 дней — 4/ })).toBeVisible();
  // У каждой KPI-плитки есть подпись источника/окна.
  for (const tile of await page.getByTestId('kpi-grid').locator(':scope > section').all()) await expect(tile.locator('p.text-fg-subtle')).toHaveCount(1);

  // Лента активности: события журнала; PASS без evidence — «Недостаточно доказательств» (D-8).
  const feed = page.getByRole('feed', { name: 'События рабочего пространства' });
  await expect(feed.getByRole('article')).toHaveCount(3);
  await expect(feed.locator('[data-event-type="gate.completed"] [data-verification]')).toHaveAttribute('data-verification', 'INSUFFICIENT_EVIDENCE');
  const activityRequest = api.requests.find((r) => r.operationId === 'listActivity');
  expect(activityRequest?.headers['x-workspace-id']).toBe(ws.id);

  // Активные процессы: прогресс только по SUCCEEDED (2 из 4).
  const workflows = page.getByRole('region', { name: 'Активные процессы' });
  await expect(workflows.getByTestId('run-progress')).toContainText('2 из 4');
  await expect(workflows.getByRole('link', { name: 'Экспорт отчётов в CSV' })).toHaveAttribute('href', `/w/${ws.id}/projects/${pay.id}/runs/${run.id}`);

  // Результаты агентов и обзор качества.
  const agentsSection = page.getByRole('region', { name: 'Результаты агентов' });
  await expect(agentsSection.getByRole('table', { name: 'Результаты по ролям' }).getByRole('row')).toHaveCount(3);
  await expect(agentsSection.getByTestId('data-unavailable')).toContainText('Циклы ревью по ролям');
  const qualitySection = page.getByRole('region', { name: 'Обзор качества' });
  const outcomes = qualitySection.getByRole('list', { name: 'Исходы гейтов' });
  await expect(outcomes.locator('[data-segment="PASS"]')).toContainText('4');
  await expect(outcomes.locator('[data-segment="INSUFFICIENT_EVIDENCE"]')).toContainText('2');
  await expect(outcomes.locator('[data-segment="FAIL"]')).toContainText('1');
  await expect(outcomes.locator('[data-segment="BLOCKED"]')).toContainText('1');
  await expect(outcomes.locator('[data-segment="NOT_RUN"]')).toContainText('2');
  await expect(qualitySection.locator('[data-gate-type="UNIT"]')).toContainText('пройдено 4 из 6 исп., недостаточно доказательств 1');

  // Состояние системы (owner): компоненты, воркеры по видам, «неизвестно» остаётся «неизвестно».
  const health = page.getByRole('region', { name: 'Состояние системы' });
  await expect(health.locator('[data-component="backup"] [data-health]')).toHaveAttribute('data-health', 'unknown');
  await expect(health.locator('[data-worker-kind="verify"]')).toContainText('недоступен');
  await expect(health.locator('[data-worker-kind="index"]')).toContainText('не ожидается в этой установке');
  await expect(health).toContainText('не измерено');

  for (const scheme of ['dark', 'light'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `dashboard (${scheme})`);
    await expectNoHorizontalScroll(page);
    await shot(page, `dashboard-${scheme}-1920`);
  }
  for (const width of [1366, 375]) {
    await page.setViewportSize({ width, height: 900 });
    for (const scheme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
      await shot(page, `dashboard-${scheme}-${width}`);
    }
  }
});

test('empty workspace: onboarding CTA, real zeros and «no data» instead of fake numbers; 375/768/1366/1920 without overflow', async ({ page, api }) => {
  api.addWorkspace('Новая команда', 'new', 'admin');
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Новая команда' })).toBeFocused();
  const empty = page.getByTestId('empty-state');
  await expect(empty).toContainText('В рабочем пространстве пока нет проектов');
  await expect(empty.getByRole('link', { name: 'Создать проект' })).toBeVisible();
  await expect(page.getByTestId('metric-projects')).toHaveAttribute('data-value', '0');
  await expect(page.getByTestId('metric-verified-rate')).toContainText('нет данных');
  await expect(page.getByTestId('metric-run-success')).toContainText('нет завершённых запусков в окне');
  await expect(page.getByTestId('workflows-empty')).toBeVisible();
  await expect(page.getByTestId('activity-empty')).toBeVisible();
  await expect(page.getByTestId('quality-empty')).toBeVisible();
  // Системное состояние без измерений — «неизвестно», не «работает».
  await expect(page.getByRole('region', { name: 'Состояние системы' }).locator('[data-health]').first()).toHaveAttribute('data-health', 'unknown');
  for (const width of [375, 768, 1366, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const scheme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
    }
  }
  await expectNoSeriousA11yViolations(page, 'dashboard empty');
});

test('partial API errors: failing tiles show retry, other tiles keep working', async ({ page, api }) => {
  // Первый зарегистрированный обработчик операции выигрывает — переопределения до seed().
  let findingsFail = true;
  api.route('GET', '/api/v1/findings', 'listFindings', () => (findingsFail ? problem(500, 'internal_error') : { status: 200, body: { items: [], next_cursor: null } }));
  api.route('GET', '/api/v1/system/health', 'getSystemHealth', () => problem(503, 'service_unavailable'));
  api.route('GET', '/api/v1/activity', 'listActivity', () => problem(500, 'internal_error'));
  seed(api);
  await login(page, api);
  const security = page.getByRole('region', { name: 'Находки безопасности' });
  await expect(security.getByRole('alert')).toContainText('Не удалось загрузить данные');
  await expect(page.getByRole('region', { name: 'Состояние системы' }).getByRole('alert')).toBeVisible();
  await expect(page.getByRole('region', { name: 'Лента активности AI' }).getByRole('alert')).toBeVisible();
  // Остальные плитки — с данными.
  await expect(page.getByTestId('metric-projects')).toContainText('2');
  await expect(page.getByTestId('metric-verified-rate')).toContainText('91,7%');
  await expect(page.getByTestId('metric-approvals')).toContainText('1');
  await expectNoSeriousA11yViolations(page, 'dashboard partial errors');
  findingsFail = false;
  await security.getByRole('button', { name: 'Повторить' }).click();
  await expect(security.getByTestId('metric-findings')).toContainText('0');
  await expect(security.getByRole('alert')).toHaveCount(0);
});

test('viewer without system:health: no System Health section and no request', async ({ page, api }) => {
  seed(api, 'viewer');
  await login(page, api);
  await expect(page.getByTestId('metric-verified-rate')).toContainText('91,7%');
  await expect(page.getByRole('region', { name: 'Результаты агентов' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Состояние системы' })).toHaveCount(0);
  expect(api.requests.some((r) => r.operationId === 'getSystemHealth')).toBe(false);
  await expect(page.getByTestId('empty-state')).toHaveCount(0);
});

test('live activity via workspace SSE: new events appear in the feed, tiles refresh; filters by type', async ({ page, api }) => {
  const activityTypes: string[][] = [];
  let realActivity: ((url: URL) => unknown) | null = null;
  api.route('GET', '/api/v1/activity', 'listActivity', ({ url }) => {
    activityTypes.push(url.searchParams.getAll('event_type'));
    return { status: 200, body: realActivity ? realActivity(url) : { items: [], next_cursor: null } };
  });
  const { ws, run, t1 } = seed(api);
  // Те же события, что у seed() (его обработчик зарегистрирован вторым и не вызывается), с фильтром event_type как у сервера.
  realActivity = (url) => {
    const types = url.searchParams.getAll('event_type');
    const page = activityBody(run.id, t1.id);
    return { ...page, items: page.items.filter((e) => types.length === 0 || types.includes(e.event_type)) };
  };
  await login(page, api);
  const feed = page.getByRole('feed', { name: 'События рабочего пространства' });
  await expect(feed.getByRole('article')).toHaveCount(3);
  await expect(page.getByTestId('live-state').first()).toHaveAttribute('data-state', /open|reconnecting|connecting/);

  const approvalsBefore = api.requests.filter((r) => r.operationId === 'listApprovals').length;
  api.addWorkspaceEvent(
    ws.id,
    'approval.requested',
    {
      approval_id: '01900000-0000-7000-e000-000000000009',
      action_type: 'PRODUCTION_DEPLOY',
      summary: 'Деплой экспорта в production',
      required_role: 'admin',
      expires_at: '2026-11-09T09:00:00Z',
    },
    run.id,
  );
  const liveItem = feed.locator('[data-event-type="approval.requested"]');
  await expect(liveItem).toContainText('Деплой экспорта в production');
  await expect(liveItem).toContainText('новое');
  await expect(feed.getByRole('article')).toHaveCount(4);
  // Событие approval.* инвалидирует плитку одобрений (повторный listApprovals).
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'listApprovals').length).toBeGreaterThan(approvalsBefore);

  // Фильтр по типу: только «Проверки качества» → запрос с event_type=gate.completed, события потока других типов скрыты.
  const filters = page.getByRole('group', { name: 'Типы событий' });
  for (const name of ['Запуски', 'Задачи', 'Агенты', 'Одобрения', 'Директивы', 'Чат']) await filters.getByRole('button', { name, exact: true }).click();
  await expect(filters.getByRole('button', { name: 'Проверки качества' })).toHaveAttribute('aria-pressed', 'true');
  await expect(filters.getByRole('button', { name: 'Одобрения' })).toHaveAttribute('aria-pressed', 'false');
  await expect(feed.locator('[data-event-type="approval.requested"]')).toHaveCount(0);
  await expect.poll(() => activityTypes.at(-1)).toEqual(['gate.completed']);
  // По умолчанию вывод CLI не запрашивается.
  expect(activityTypes[0]).not.toContain('cli.output');
  await expectNoSeriousA11yViolations(page, 'dashboard live + filter');
});

test('reduced motion: tiles do not move on hover; transitions are instant', async ({ page, api }) => {
  seed(api);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await login(page, api);
  const tile = page.getByRole('region', { name: 'Активные проекты' });
  await expect(tile.getByTestId('metric-projects')).toContainText('2');
  await tile.hover();
  const style = await tile.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { transform: cs.transform, duration: cs.transitionDuration };
  });
  expect(style.transform).toBe('none');
  // Глобальный сброс index.css: transition-duration 0.01ms (фактически мгновенно).
  expect(style.duration.split(',').every((d) => (d.trim().endsWith('ms') ? parseFloat(d) : parseFloat(d) * 1000) <= 0.01)).toBe(true);

  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.mouse.move(0, 0);
  await tile.hover();
  await expect.poll(() => tile.evaluate((el) => getComputedStyle(el).transform)).not.toBe('none');
});
