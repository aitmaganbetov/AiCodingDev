import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Run, TaskSummary } from '../api/client';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../test/utils';

const PROJECT = project(1);
const RUN_ID = '01900000-0000-7000-8000-00000000a001';
const runPath = `/w/${WS_ID}/projects/${PROJECT.id}/runs`;

function run(overrides: Partial<Run> = {}): Run {
  return {
    id: RUN_ID,
    project_id: PROJECT.id,
    workspace_id: WS_ID,
    mode: 'DEVELOP',
    objective: 'Экспорт в CSV',
    status: 'BLOCKED',
    blocked_reason: 'APPROVAL_REQUIRED',
    requested_by: { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна' },
    quality_profile: 'strict',
    version: 3,
    created_at: '2026-11-08T08:00:00Z',
    pending_approvals: 2,
    task_counts: { RUNNING: 1 },
    ...overrides,
  };
}

function task(n: number, overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    id: `01900000-0000-7000-8000-00000000b00${n}`,
    run_id: RUN_ID,
    agent_role: 'developer',
    title: `Задача ${n}`,
    status: 'RUNNING',
    complexity: 'L2',
    risk_level: 'low',
    attempt_count: 1,
    max_attempts: 3,
    depends_on: [],
    version: 0,
    updated_at: '2026-11-08T08:00:00Z',
    ...overrides,
  };
}

function sse(body: string): Response {
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

const completed = `id: 1\nevent: run.completed\ndata: ${JSON.stringify({
  event_id: '01900000-0000-7000-a000-000000000001',
  run_id: RUN_ID,
  seq: 1,
  type: 'run.completed',
  timestamp: '2026-11-08T09:00:00Z',
  schema_version: '1.0',
  payload: { final_status: 'COMPLETED', required_gates_total: 3, required_gates_passed: 3 },
})}\n\n`;

describe('run page', () => {
  it('shows header (status, blocked reason, pending approvals), tasks with dependencies and the event feed', async () => {
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(200, run()),
      [`GET /api/v1/runs/${RUN_ID}/tasks`]: () =>
        json(200, {
          run_id: RUN_ID,
          plan_version: 2,
          tasks: [task(1, { status: 'SUCCEEDED' }), task(2, { depends_on: [task(1).id] })],
          edges: [{ task_id: task(2).id, depends_on_task_id: task(1).id, dependency_type: 'blocks' }],
          next_cursor: null,
        }),
      [`GET /api/v1/runs/${RUN_ID}/events`]: () => sse(`retry: 10\n\n${completed}`),
    });
    renderApp(`${runPath}/${RUN_ID}`);
    expect(await screen.findByText('Требуется одобрение')).toBeInTheDocument();
    expect(screen.getByTestId('run-pending-approvals')).toHaveTextContent('2');
    const table = await screen.findByRole('table', { name: 'Задачи run (план v2)' });
    const row2 = within(table).getByRole('row', { name: /^Задача 2/ });
    expect(row2).toHaveTextContent('Задача 1');
    expect(row2).toHaveTextContent('1 из 3');
    // BLOCKED: можно приостановить и отменить, но не возобновить.
    expect(screen.getByRole('button', { name: 'Приостановить' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Возобновить' })).toBeNull();

    const feed = await screen.findByRole('list', { name: 'События run' });
    expect(within(feed).getByText('Итог: Завершён; обязательные проверки 3 из 3')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('stream-state')).toHaveAttribute('data-state', 'closed'));
    expect(calls.filter((c) => c.url.pathname.endsWith('/events'))).toHaveLength(1);
  });

  it('shows a fatal stream error with a manual reconnect button', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      [`GET /api/v1/runs/${RUN_ID}`]: () => json(200, run({ status: 'RUNNING', blocked_reason: null })),
      [`GET /api/v1/runs/${RUN_ID}/tasks`]: () => json(200, { run_id: RUN_ID, plan_version: 1, tasks: [], edges: [], next_cursor: null }),
      [`GET /api/v1/runs/${RUN_ID}/events`]: () => problem(403, 'forbidden'),
    });
    renderApp(`${runPath}/${RUN_ID}`);
    expect(await screen.findByText('Недостаточно прав для этого действия.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Переподключиться' })).toBeInTheDocument();
    // viewer: нет run:control
    expect(screen.queryByRole('button', { name: 'Приостановить' })).toBeNull();
  });
});

describe('start run form', () => {
  it('reports 404 (unknown or foreign project) as a form-level error', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('developer')),
      [`GET /api/v1/projects/${PROJECT.id}`]: () => json(200, PROJECT),
      'GET /api/v1/runs': () => json(200, { items: [], next_cursor: null }),
      'POST /api/v1/runs': () => problem(404, 'not_found'),
    });
    renderApp(runPath);
    await user.type(await screen.findByLabelText('Цель'), 'Сделать что-то');
    await user.click(screen.getByRole('button', { name: 'Запустить' }));
    expect(await screen.findByTestId('form-alert')).toHaveTextContent('Проект не найден или у вас нет к нему доступа');
    const list = calls.find((c) => c.method === 'GET' && c.url.pathname === '/api/v1/runs');
    expect(list?.headers.get('X-Workspace-Id')).toBe(WS_ID);
    expect(list?.url.searchParams.get('project_id')).toBe(PROJECT.id);
    const post = calls.find((c) => c.method === 'POST' && c.url.pathname === '/api/v1/runs');
    expect(post?.headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
  });
});
