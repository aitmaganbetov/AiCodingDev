import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { AnalyticsSummary } from '../../api/analytics';
import i18n from '../../i18n';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../../test/utils';
import { barPercent, formatBytes, formatDuration, formatRate, paramsFromSearch, queryFromParams, searchFromParams } from './format';

const t = i18n.t.bind(i18n);
const P1 = project(1);

function summary(overrides: Partial<AnalyticsSummary> = {}): AnalyticsSummary {
  const stats = { p50: 120, p90: 600, avg: 200 };
  return {
    window: { from: '2026-10-09T13:00:00Z', to: '2026-11-08T13:00:00Z' },
    project_id: null,
    runs: {
      total: 4,
      finished: 3,
      completed: 2,
      success_rate: 2 / 3,
      by_status: { COMPLETED: 2, FAILED: 1, RUNNING: 1 },
      duration_seconds: stats,
      by_mode: [{ mode: 'DEVELOP', total: 4, finished: 3, completed: 2, success_rate: 2 / 3, avg_duration_seconds: 300 }],
    },
    daily: [
      { date: '2026-11-06', runs_created: 2, runs_completed: 1, runs_failed: 1, runs_cancelled: 0 },
      { date: '2026-11-07', runs_created: 2, runs_completed: 1, runs_failed: 0, runs_cancelled: 0 },
    ],
    tasks: {
      attempts: 6,
      succeeded: 4,
      failed: 2,
      retries: 2,
      verified_completion_rate: 0.8,
      tasks_finished: 5,
      latency_seconds: stats,
      by_role: [{ role: 'developer', attempts: 4, succeeded: 3, failed: 1, retries: 1, verified_completion_rate: 0.75 }],
    },
    gates: [
      { gate_type: 'UNIT', total: 4, passed: 3, passed_verified: 3, passed_unverified: 0, failed: 1, errored: 0, skipped: 1, pass_rate: 0.75, avg_duration_seconds: 12 },
      { gate_type: 'SAST', total: 1, passed: 0, passed_verified: 0, passed_unverified: 0, failed: 0, errored: 0, skipped: 1, pass_rate: null, avg_duration_seconds: null },
    ],
    providers: [
      {
        provider: 'codex',
        executions: 5,
        collected: 4,
        errors: 1,
        rate_limited: 1,
        auth_required: 0,
        cancelled: 0,
        in_progress: 0,
        success_rate: 0.8,
        avg_duration_seconds: 95,
        failures_by_category: { RATE_LIMITED: 1 },
        tokens: { total: null, source: 'not_exposed_by_cli', executions_reported: 0 },
      },
    ],
    review_loops: {
      review_requests: 3,
      fix_requests: 2,
      review_chains: 2,
      chains_with_fixes: 1,
      avg_fix_rounds: 1,
      max_fix_rounds: 2,
      loops_exhausted: 0,
    },
    agent_messages: { total: 9, by_type: { REVIEW_REQUEST: 3, FIX_REQUEST: 2, TASK_COMPLETED: 4 } },
    approvals: { requested: 2, pending: 1, by_status: { APPROVED: 1, PENDING: 1 }, wait_seconds: { p50: 90, p90: 4000, avg: 2000 } },
    context: {
      packs: 4,
      avg_size_bytes: 20480,
      max_size_bytes: 40000,
      fragments: 40,
      stale_fragment_ratio: 0.1,
      usefulness: { rate: 0.25, packs_evaluated: 3, fragments_referenced: 6, fragments_total: 24, truncated: false },
    },
    ...overrides,
  };
}

describe('analytics format', () => {
  it('formats rates, durations and sizes; null is "no data", not zero', () => {
    expect(formatRate(t, 2 / 3)).toBe('66,7%');
    expect(formatRate(t, null)).toBe('нет данных');
    expect(formatDuration(t, 42)).toBe('42 с');
    expect(formatDuration(t, 200)).toBe('3 мин 20 с');
    expect(formatDuration(t, 7500)).toBe('2 ч 5 мин');
    expect(formatDuration(t, 90_000)).toBe('1 д 1 ч');
    expect(formatDuration(t, null)).toBe('нет данных');
    expect(formatBytes(t, 512)).toBe('512 Б');
    expect(formatBytes(t, 20480)).toBe('20 КБ');
    expect(formatBytes(t, 3 * 1024 * 1024)).toBe('3 МБ');
    expect(barPercent(1.5)).toBe(100);
    expect(barPercent(null)).toBe(0);
  });

  it('round-trips URL params and builds an hour-aligned window', () => {
    const params = paramsFromSearch(new URLSearchParams(`period=7d&project=${P1.id}`));
    expect(params).toEqual({ period: '7d', projectId: P1.id });
    expect(paramsFromSearch(new URLSearchParams('period=bogus&project=../x'))).toEqual({ period: '30d', projectId: null });
    expect(searchFromParams(params).toString()).toBe(`period=7d&project=${P1.id}`);
    expect(searchFromParams({ period: '30d', projectId: null }).toString()).toBe('');
    const q = queryFromParams(params, new Date('2026-11-08T12:34:56Z'));
    expect(q).toEqual({ from: '2026-11-01T13:00:00.000Z', to: '2026-11-08T13:00:00.000Z', project_id: P1.id });
  });
});

describe('AnalyticsPage', () => {
  it('renders KPIs, chart with table alternative, gates, providers (unknown tokens) and filters in the URL', async () => {
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
      'GET /api/v1/analytics/summary': () => json(200, summary()),
    });
    renderApp(`/w/${WS_ID}/analytics`);
    expect(await screen.findByRole('heading', { level: 1, name: /Аналитика/ })).toBeInTheDocument();
    expect(await screen.findByTestId('kpi-success')).toHaveTextContent('66,7%');
    expect(screen.getByTestId('kpi-vcr')).toHaveTextContent('80%');
    expect(screen.getByTestId('kpi-approval')).toHaveTextContent('1 мин 30 с');
    expect(screen.getByRole('img', { name: /всего 4, успешно 2, с ошибкой 1/ })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: /Запуски по дням/ })).toBeInTheDocument();
    const gates = screen.getByRole('list', { name: 'Доля пройденных проверок по типам' });
    expect(within(gates).getAllByRole('listitem')).toHaveLength(2);
    expect(within(gates).getByText('нет данных')).toBeInTheDocument();
    const providers = screen.getByRole('table', { name: /CLI-провайдеров/ });
    expect(within(providers).getByText('неизвестно (CLI не сообщает)')).toBeInTheDocument();
    const call = calls.find((c) => c.url.pathname === '/api/v1/analytics/summary');
    expect(call?.headers.get('x-workspace-id')).toBe(WS_ID);
    expect(call?.url.searchParams.get('from')).toMatch(/Z$/);

    await userEvent.selectOptions(screen.getByLabelText('Период'), '7d');
    await userEvent.selectOptions(screen.getByLabelText('Проект'), P1.id);
    await waitFor(() => {
      const last = calls.filter((c) => c.url.pathname === '/api/v1/analytics/summary').at(-1);
      expect(last?.url.searchParams.get('project_id')).toBe(P1.id);
    });
  });

  it('shows empty and error states', async () => {
    let fail = true;
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/analytics/summary': () =>
        fail
          ? problem(500, 'internal_error')
          : json(200, summary({ runs: { ...summary().runs, total: 0, finished: 0, completed: 0, success_rate: null, by_status: {}, by_mode: [] }, providers: [], daily: [] })),
    });
    renderApp(`/w/${WS_ID}/analytics`);
    const error = await screen.findByTestId('error-state');
    expect(error).toHaveTextContent('Не удалось загрузить аналитику');
    fail = false;
    await userEvent.click(within(error).getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('За период нет запусков')).toBeInTheDocument();
  });
});
