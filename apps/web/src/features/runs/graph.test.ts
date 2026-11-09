import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { runKeys, type TaskSummary } from '../../api/runs';
import { envelope } from '../../test/sse';
import { layoutDag } from './graphLayout';
import { applyTaskEvents } from './useRunTasks';

describe('layoutDag', () => {
  it('places each task one layer after its deepest dependency', () => {
    const r = layoutDag([
      { id: 'a', dependsOn: [] },
      { id: 'b', dependsOn: ['a'] },
      { id: 'c', dependsOn: ['a', 'b'] },
      { id: 'd', dependsOn: [] },
    ]);
    expect(Object.fromEntries(r.layer)).toEqual({ a: 0, b: 1, c: 2, d: 0 });
    expect(r.layers).toEqual([['a', 'd'], ['b'], ['c']]);
    expect(r.position.get('d')!.y).toBeGreaterThan(r.position.get('a')!.y);
    expect(r.position.get('c')!.x).toBeGreaterThan(r.position.get('b')!.x);
  });

  it('ignores unknown dependencies and survives cycles', () => {
    const r = layoutDag([
      { id: 'a', dependsOn: ['b', 'ghost'] },
      { id: 'b', dependsOn: ['a'] },
    ]);
    expect(r.layer.size).toBe(2);
    expect([...r.layer.values()].sort()).toEqual([0, 1]);
  });
});

function task(id: string, init: Partial<TaskSummary> = {}): TaskSummary {
  return {
    id,
    run_id: 'r',
    agent_role: 'developer',
    title: id,
    status: 'READY',
    complexity: 'L2',
    risk_level: 'low',
    attempt_count: 0,
    max_attempts: 3,
    depends_on: [],
    version: 1,
    updated_at: '2026-11-08T08:00:00Z',
    ...init,
  };
}

describe('applyTaskEvents', () => {
  it('patches status, executor and attempts; older task_version is ignored', () => {
    const qc = new QueryClient();
    qc.setQueryData(runKeys.tasks('r'), {
      pages: [{ run_id: 'r', plan_version: 1, tasks: [task('t1'), task('t2')], edges: [], next_cursor: null }],
      pageParams: [null],
    });
    const running = envelope(1);
    running.payload = { task_id: 't1', agent_role: 'developer', from_status: 'READY', to_status: 'RUNNING', task_version: 2, attempt_no: 1, provider: 'codex' };
    const stale = envelope(2);
    stale.payload = { task_id: 't1', agent_role: 'developer', from_status: null, to_status: 'PENDING', task_version: 0 };
    expect(applyTaskEvents(qc, 'r', [running, stale])).toBe(true);
    const data = qc.getQueryData<{ pages: { tasks: TaskSummary[] }[] }>(runKeys.tasks('r'));
    const t1 = data!.pages[0]!.tasks[0]!;
    expect(t1).toMatchObject({ status: 'RUNNING', current_provider: 'codex', attempt_count: 1, version: 2 });
    expect(data!.pages[0]!.tasks[1]!.status).toBe('READY');
    expect(applyTaskEvents(qc, 'r', [envelope(3, 'agent.message')])).toBe(false);
  });
});
