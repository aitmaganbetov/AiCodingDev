import { screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AnalyticsSummary } from '../../api/analytics';
import type { Run, RunSummary } from '../../api/runs';
import type { SystemHealth } from '../../api/system';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../../test/utils';

const P1 = project(1);
const RUN_ID = '01900000-0000-7000-a000-000000000001';

function run(status: RunSummary['status'], extra: Partial<Run> = {}): Run {
  return {
    id: RUN_ID,
    project_id: P1.id,
    workspace_id: WS_ID,
    mode: 'DEVELOP',
    objective: 'Экспорт отчётов в CSV',
    status,
    blocked_reason: null,
    requested_by: { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна' },
    quality_profile: 'strict',
    version: 1,
    created_at: '2026-11-08T10:00:00Z',
    started_at: null,
    finished_at: null,
    ...extra,
  };
}

function analyticsBody(over: Partial<AnalyticsSummary> = {}): AnalyticsSummary {
  const none = { p50: null, p90: null, avg: null };
  return {
    window: { from: '2026-10-10T11:00:00Z', to: '2026-11-09T11:00:00Z' },
    project_id: null,
    runs: { total: 5, finished: 4, completed: 3, success_rate: 0.75, by_status: {}, duration_seconds: none, by_mode: [] },
    daily: [
      { date: '2026-11-07', runs_created: 2, runs_completed: 1, runs_failed: 0, runs_cancelled: 0 },
      { date: '2026-11-08', runs_created: 3, runs_completed: 2, runs_failed: 1, runs_cancelled: 0 },
    ],
    tasks: {
      attempts: 12,
      succeeded: 9,
      failed: 3,
      retries: 2,
      verified_completion_rate: 0.9,
      tasks_finished: 12,
      latency_seconds: none,
      by_role: [{ role: 'developer', attempts: 8, succeeded: 6, failed: 2, retries: 1, verified_completion_rate: 0.857 }],
    },
    gates: [{ gate_type: 'UNIT', total: 6, passed: 4, passed_verified: 3, passed_unverified: 1, failed: 1, errored: 0, skipped: 1, pass_rate: 0.8, avg_duration_seconds: 12 }],
    providers: [
      {
        provider: 'codex',
        executions: 10,
        collected: 9,
        errors: 1,
        rate_limited: 0,
        auth_required: 0,
        cancelled: 0,
        in_progress: 0,
        success_rate: 0.9,
        avg_duration_seconds: 30,
        failures_by_category: {},
        tokens: { total: null, source: 'not_exposed_by_cli', executions_reported: 0 },
      },
    ],
    review_loops: { review_requests: 4, fix_requests: 2, review_chains: 4, chains_with_fixes: 1, avg_fix_rounds: 1.5, max_fix_rounds: 2, loops_exhausted: 0 },
    agent_messages: { total: 0, by_type: {} },
    approvals: { requested: 1, pending: 1, by_status: {}, wait_seconds: none },
    context: {
      packs: 0,
      avg_size_bytes: null,
      max_size_bytes: null,
      fragments: 0,
      stale_fragment_ratio: null,
      usefulness: { rate: null, packs_evaluated: 0, fragments_referenced: 0, fragments_total: 0, truncated: false },
    },
    ...over,
  };
}

const HEALTH: SystemHealth = {
  status: 'degraded',
  checked_at: '2026-11-09T09:00:00Z',
  cache_ttl_seconds: 10,
  components: [
    { name: 'database', status: 'ok', checked_at: '2026-11-09T09:00:00Z', latency_ms: 3, measurements: {}, detail: null },
    { name: 'backup', status: 'unknown', checked_at: '2026-11-09T09:00:00Z', latency_ms: null, measurements: {}, detail: 'файл статуса не смонтирован' },
  ],
  worker_hosts: [],
  worker_kinds: [
    { kind: 'exec', status: 'down', expected: true, hosts: 1, active_hosts: 0, freshest_heartbeat_age_seconds: 400, heartbeat_interval_seconds: 30 },
  ],
  queues: null,
  backups: null,
};

function routes(role: 'owner' | 'viewer', overrides: Record<string, () => Response> = {}) {
  return {
    'GET /api/v1/auth/session': () => json(200, session(role)),
    'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
    'GET /api/v1/runs': () => json(200, { items: [run('RUNNING')], next_cursor: null }),
    [`GET /api/v1/runs/${RUN_ID}`]: () => json(200, run('RUNNING', { task_counts: { SUCCEEDED: 2, RUNNING: 1, PENDING: 1 } })),
    'GET /api/v1/tasks': () => json(200, { items: [], next_cursor: 'more' }),
    'GET /api/v1/analytics/summary': () => json(200, analyticsBody()),
    'GET /api/v1/findings': () =>
      json(200, {
        items: [
          { id: 'f1', severity: 'critical', rule_id: 'r', status: 'OPEN' },
          { id: 'f2', severity: 'high', rule_id: 'r', status: 'OPEN' },
        ],
        next_cursor: null,
      }),
    'GET /api/v1/approvals': () => json(200, { items: [], next_cursor: null }),
    'GET /api/v1/providers': () => json(200, { items: [] }),
    'GET /api/v1/activity': () =>
      json(200, {
        items: [
          {
            id: '01900000-0000-7000-b000-000000000001',
            run_id: RUN_ID,
            seq: 3,
            event_type: 'gate.completed',
            payload: { gate_type: 'UNIT', status: 'PASS', evidence_id: null, required: true, commit_sha: 'abc', gate_execution_id: 'g' },
            occurred_at: '2026-11-09T08:00:00Z',
          },
        ],
        next_cursor: null,
      }),
    'GET /api/v1/system/health': () => json(200, HEALTH),
    ...overrides,
  };
}

describe('Bento Dashboard', () => {
  // jsdom не считает раскладку: окно ленты 400 px (как в тестах VirtualList).
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(function (this: HTMLElement) {
      return this.dataset.testid === 'virtual-list' ? 400 : 40;
    });
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(600);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(400);
  });
  afterEach(() => vi.restoreAllMocks());

  it('KPI из реальных операций, подписи источника, D-6/D-8, состояние системы для owner', async () => {
    const { calls } = mockFetch(routes('owner'));
    renderApp(`/w/${WS_ID}/dashboard`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Обзор · Команда 1' })).toBeInTheDocument();
    expect(screen.getByText('Welcome to AiCodingDev')).toHaveAttribute('lang', 'en');

    const agents = await screen.findByRole('region', { name: 'Работающие AI-агенты' });
    expect(await within(agents).findByTestId('metric-agents')).toHaveTextContent('0+');
    const tasksCall = calls.find((c) => c.url.pathname === '/api/v1/tasks');
    expect(tasksCall?.url.searchParams.getAll('status')).toEqual(['LEASED', 'RUNNING']);

    const quality = screen.getByRole('region', { name: 'Качество (измеренные доли)' });
    expect(await within(quality).findByTestId('metric-verified-rate')).toHaveTextContent('90%');
    // D-8: «Пройдено» — только PASS с VERIFIED evidence: 3 из 5 исполненных (PASS без него не засчитан).
    expect(within(quality).getByTestId('metric-gate-pass-verified')).toHaveTextContent('60%');
    expect(within(quality).getByTestId('metric-gate-pass-verified')).toHaveTextContent('недостаточно доказательств: 1');
    expect(quality).not.toHaveTextContent(/score|оценка качества:/i);

    const security = screen.getByRole('region', { name: 'Находки безопасности' });
    expect(await within(security).findByTestId('metric-findings')).toHaveTextContent('2');
    expect(calls.find((c) => c.url.pathname === '/api/v1/findings')?.url.searchParams.getAll('status')).toEqual(['OPEN']);

    const workflows = screen.getByRole('region', { name: 'Активные процессы' });
    expect(await within(workflows).findByTestId('run-progress')).toHaveTextContent('2 из 4');

    const qOverview = screen.getByRole('region', { name: 'Обзор качества' });
    const outcomes = await within(qOverview).findByRole('list', { name: 'Исходы гейтов' });
    expect(within(outcomes).getByText('Пройдено').closest('li')).toHaveTextContent('3');
    expect(within(outcomes).getByText('Недостаточно доказательств').closest('li')).toHaveTextContent('1');
    expect(qOverview).not.toHaveTextContent('по сводке не определяется');

    const feed = await screen.findByRole('feed', { name: 'События рабочего пространства' });
    expect(await within(feed).findByText('Проверка качества')).toBeInTheDocument();
    // PASS без evidence в событии гейта — «Недостаточно доказательств» (D-8).
    expect(feed.querySelector('[data-verification]')).toHaveAttribute('data-verification', 'INSUFFICIENT_EVIDENCE');
    expect(calls.find((c) => c.url.pathname === '/api/v1/activity')?.url.searchParams.getAll('event_type')).not.toContain('cli.output');

    const health = await screen.findByRole('region', { name: 'Состояние системы' });
    expect(await within(health).findByText('База данных')).toBeInTheDocument();
    expect(within(health).getByText('файл статуса не смонтирован')).toBeInTheDocument();
    expect(health.querySelector('[data-component="backup"] [data-health]')).toHaveAttribute('data-health', 'unknown');
    expect(within(health).getAllByText('не измерено')).toHaveLength(2);
  });

  it('ошибка одной операции не ломает остальные плитки; «Повторить» перезапрашивает', async () => {
    let fail = true;
    mockFetch(
      routes('owner', {
        'GET /api/v1/findings': () => (fail ? problem(500, 'internal_error') : json(200, { items: [], next_cursor: null })),
      }),
    );
    renderApp(`/w/${WS_ID}/dashboard`);
    const security = await screen.findByRole('region', { name: 'Находки безопасности' });
    const alert = await within(security).findByRole('alert');
    expect(alert).toHaveTextContent('Не удалось загрузить данные');
    expect(await screen.findByTestId('metric-verified-rate')).toHaveTextContent('90%');
    fail = false;
    within(alert).getByRole('button', { name: 'Повторить' }).click();
    expect(await within(security).findByTestId('metric-findings')).toHaveTextContent('0');
  });

  it('viewer: без права system:health запрос не выполняется и раздела нет', async () => {
    const { calls } = mockFetch(routes('viewer'));
    renderApp(`/w/${WS_ID}/dashboard`);
    expect(await screen.findByTestId('metric-verified-rate')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Состояние системы' })).toBeNull();
    expect(calls.some((c) => c.url.pathname === '/api/v1/system/health')).toBe(false);
  });

  it('нет завершённых runs в окне — «нет завершённых запусков», не 0 %', async () => {
    mockFetch(
      routes('owner', {
        'GET /api/v1/analytics/summary': () =>
          json(200, analyticsBody({ runs: { total: 0, finished: 0, completed: 0, success_rate: null, by_status: {}, duration_seconds: { p50: null, p90: null, avg: null }, by_mode: [] } })),
      }),
    );
    renderApp(`/w/${WS_ID}/dashboard`);
    expect(await screen.findByTestId('metric-run-success')).toHaveTextContent('нет завершённых запусков в окне');
  });
});
