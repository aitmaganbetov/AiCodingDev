import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { RunSummary } from '../../api/runs';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../../test/utils';
import { summarizeRuns } from './summary';

const P1 = project(1);

function run(n: number, status: RunSummary['status'], extra: Partial<RunSummary> = {}): RunSummary {
  return {
    id: `01900000-0000-7000-a000-${String(n).padStart(12, '0')}`,
    project_id: P1.id,
    workspace_id: WS_ID,
    mode: 'DEVELOP',
    objective: `Цель ${n}`,
    status,
    blocked_reason: null,
    requested_by: { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна' },
    quality_profile: 'strict',
    version: 1,
    created_at: `2026-11-0${n}T10:00:00Z`,
    started_at: null,
    finished_at: null,
    ...extra,
  };
}

describe('summarizeRuns', () => {
  it('groups runs into active / attention and orders by newest', () => {
    const s = summarizeRuns([run(1, 'RUNNING'), run(2, 'AWAITING_APPROVAL'), run(3, 'FAILED'), run(4, 'BLOCKED'), run(5, 'COMPLETED')], 3);
    expect(s.active.map((r) => r.objective)).toEqual(['Цель 1']);
    expect(s.attention.map((r) => r.objective)).toEqual(['Цель 4', 'Цель 2']);
    expect(s.recent.map((r) => r.objective)).toEqual(['Цель 5', 'Цель 4', 'Цель 3']);
    expect(s.failed).toBe(1);
    expect(s.completed).toBe(1);
  });
});

describe('DashboardPage', () => {
  it('shows stats, runs needing attention with contextual links and recent runs', async () => {
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
      'GET /api/v1/runs': () =>
        json(200, {
          items: [run(1, 'RUNNING'), run(2, 'AWAITING_APPROVAL'), run(3, 'BLOCKED', { blocked_reason: 'AUTH_REQUIRED' })],
          next_cursor: null,
        }),
    });
    renderApp(`/w/${WS_ID}/dashboard`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Обзор · Команда 1' })).toBeInTheDocument();
    const attention = await screen.findByRole('list', { name: 'Требуют внимания' });
    expect(within(attention).getAllByRole('listitem')).toHaveLength(2);
    expect(within(attention).getByRole('link', { name: 'К одобрениям' })).toHaveAttribute('href', `/w/${WS_ID}/approvals`);
    expect(within(attention).getByText('Требуется аутентификация провайдера')).toBeInTheDocument();
    expect(within(attention).getByRole('link', { name: 'К CLI-провайдерам' })).toHaveAttribute('href', `/w/${WS_ID}/providers`);
    const active = screen.getByRole('list', { name: 'Идут сейчас' });
    expect(within(active).getByRole('link', { name: /Открыть run/ })).toHaveAttribute('href', `/w/${WS_ID}/projects/${P1.id}/runs/${run(1, 'RUNNING').id}`);
    expect(screen.getByRole('table', { name: /Последние запуски/ })).toBeInTheDocument();
    const runsCall = calls.find((c) => c.url.pathname === '/api/v1/runs');
    expect(runsCall?.headers.get('X-Workspace-Id')).toBe(WS_ID);
    expect(runsCall?.url.searchParams.get('limit')).toBe('50');
  });

  it('shows an empty state with a create link when there are no projects', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/runs': () => json(200, { items: [], next_cursor: null }),
    });
    renderApp(`/w/${WS_ID}/dashboard`);
    const empty = await screen.findByTestId('empty-state');
    expect(within(empty).getByRole('link', { name: 'Создать проект' })).toHaveAttribute('href', `/w/${WS_ID}/projects/new`);
  });

  it('shows an error state for runs while keeping the project count', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: 'more' }),
      'GET /api/v1/runs': () => problem(500, 'internal_error'),
    });
    renderApp(`/w/${WS_ID}/dashboard`);
    expect(await screen.findByTestId('error-state')).toHaveTextContent('Не удалось загрузить запуски');
    expect(screen.getByText('1+')).toBeInTheDocument();
  });
});
