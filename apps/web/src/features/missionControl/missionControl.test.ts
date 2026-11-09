import { describe, expect, it } from 'vitest';
import type { RunEvent } from '../../api/runEvents';
import type { Evidence, GateExecution } from '../../api/tasks';
import { needsPreConfirm, QUICK_ACTION_MAP, QUICK_ACTIONS } from './CommandCenter';
import { cliLines, durationParts, elapsedMs, filterEvents, taskIndicators, waitingOn } from './feedModel';
import { readMcState, sinceCutoff, writeMcState } from './mcParams';

function ev(seq: number, type: string, payload: Record<string, unknown>, timestamp = `2026-11-08T09:00:${String(seq).padStart(2, '0')}Z`): RunEvent {
  return { event_id: `e-${seq}`, run_id: 'r-1', seq, type, timestamp, schema_version: '1.0', payload };
}

function gate(id: string, task: string, status: GateExecution['status'], evidence: string | null, type = 'UNIT_TESTS'): GateExecution {
  return { id, gate_id: `g-${id}`, gate_type: type, required: true, task_id: task, status, evidence_id: evidence };
}

function evidence(id: string, task: string, status: Evidence['verification_status']): Evidence {
  return {
    id,
    run_id: 'r-1',
    task_id: task,
    kind: 'test_report',
    producer: 'gate:unit',
    verification_status: status,
    artifact: { id: `a-${id}`, kind: 'test_report', sha256: 'x', size_bytes: 1 } as Evidence['artifact'],
    created_at: '2026-11-08T09:00:00Z',
  } as Evidence;
}

describe('mission control URL state', () => {
  it('round-trips run/task/view/nav/pane, chat filters (ctask) and feed filters', () => {
    const p = new URLSearchParams('run=r-1&task=t-1&view=cli&nav=list&pane=inspector&stream=task&ctask=t-2&frole=qa&ftask=t-3&since=1h&ftypes=state,cli&fq=error&fstatus=VERIFIED');
    const s = readMcState(p);
    expect(s).toMatchObject({ runId: 'r-1', taskId: 't-1', view: 'cli', nav: 'list', pane: 'inspector' });
    expect(s.chat).toEqual({ stream: 'task', role: undefined, task_id: 't-2', thread_id: undefined, q: undefined, item_type: undefined });
    expect(s.feed).toEqual({ role: 'qa', task: 't-3', since: '1h', groups: ['state', 'cli'], q: 'error', evidenceStatus: 'VERIFIED' });
    expect(readMcState(writeMcState(s))).toEqual(s);
  });

  it('defaults and invalid values', () => {
    const s = readMcState(new URLSearchParams('view=bogus&nav=x&pane=y&frole=hacker&since=5y&ftypes=nope&fstatus=OK'));
    expect(s).toMatchObject({ runId: null, taskId: null, view: 'conversation', nav: 'graph', pane: null });
    expect(s.chat.stream).toBe('all_agents');
    expect(s.feed).toEqual({ role: undefined, task: undefined, since: undefined, groups: undefined, q: undefined, evidenceStatus: undefined });
    expect(writeMcState(s).toString()).toBe('');
  });

  it('since → cutoff', () => {
    expect(sinceCutoff(undefined, 1_000_000)).toBeNull();
    expect(sinceCutoff('15m', 1_000_000)).toBe(1_000_000 - 900_000);
  });
});

describe('feed model', () => {
  const events = [
    ev(1, 'run.state_changed', { from_status: 'PLANNED', to_status: 'RUNNING' }),
    ev(2, 'task.state_changed', { task_id: 't-1', agent_role: 'developer', to_status: 'RUNNING' }),
    ev(3, 'cli.output', { task_id: 't-1', agent_role: 'developer', provider: 'codex', cli_event_type: 'output', stream: 'stdout', text: 'npm test', redactions: 2, truncated: true }),
    ev(4, 'cli.output', { task_id: 't-2', agent_role: 'qa', provider: 'claude', cli_event_type: 'output', stream: 'stderr', text: 'ERROR boom', dropped_chunks: 3 }),
    ev(5, 'agent.message', { from: { role: 'developer' }, to: { role: 'reviewer' }, summary: 'review' }),
    ev(6, 'gate.completed', { task_id: 't-1', gate_type: 'UNIT_TESTS', status: 'PASS' }),
  ];

  it('system journal: all events by default; groups, role, task, time narrow it', () => {
    expect(filterEvents(events, {}, null).map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(filterEvents(events, { groups: ['state', 'quality'] }, null).map((e) => e.seq)).toEqual([1, 2, 6]);
    expect(filterEvents(events, { role: 'developer' }, null).map((e) => e.seq)).toEqual([2, 3, 5]);
    expect(filterEvents(events, { task: 't-1' }, null).map((e) => e.seq)).toEqual([2, 3, 6]);
    expect(filterEvents(events, {}, Date.parse('2026-11-08T09:00:04Z')).map((e) => e.seq)).toEqual([4, 5, 6]);
  });

  it('CLI lines: only cli.output, redaction/truncation/drop markers, filters and search', () => {
    const lines = cliLines(events, {}, null);
    expect(lines.map((l) => l.seq)).toEqual([3, 4]);
    expect(lines[0]).toMatchObject({ provider: 'codex', role: 'developer', stream: 'stdout', text: 'npm test', truncated: true, redactions: 2, dropped: 0 });
    expect(lines[1]).toMatchObject({ stream: 'stderr', dropped: 3 });
    expect(cliLines(events, { role: 'qa' }, null).map((l) => l.seq)).toEqual([4]);
    expect(cliLines(events, { q: 'error' }, null).map((l) => l.seq)).toEqual([4]);
  });

  it('node indicators follow D-8: PASS counts only with VERIFIED evidence; no quality data → no indicators', () => {
    const tasks = [
      { id: 't-1', status: 'RUNNING' as const },
      { id: 't-2', status: 'BLOCKED' as const },
    ];
    const gates = [
      gate('1', 't-1', 'PASS', 'ev-1'),
      gate('2', 't-1', 'PASS', 'ev-2', 'LINT'),
      gate('3', 't-1', 'FAIL', null, 'SAST'),
      gate('4', 't-2', 'SKIPPED', null),
      gate('5', 't-2', 'ERROR', null),
    ];
    const ind = taskIndicators(tasks, gates, [evidence('ev-1', 't-1', 'VERIFIED'), evidence('ev-2', 't-1', 'UNVERIFIED')]);
    expect(ind.get('t-1')).toEqual({ verified: 1, gates: 3, failed: 1, blocked: 0, tests: { total: 1, verified: 1 }, evidence: { total: 2, verified: 1 } });
    // SKIPPED у заблокированной задачи — «Заблокировано», ERROR — тоже BLOCKED; ничего не «пройдено».
    expect(ind.get('t-2')).toMatchObject({ verified: 0, gates: 2, blocked: 2, failed: 0 });
    // Evidence не загружены: PASS без подтверждения не засчитывается.
    expect(taskIndicators(tasks, gates, null).get('t-1')).toMatchObject({ verified: 0, evidence: null });
    expect(taskIndicators(tasks, null, null).size).toBe(0);
  });

  it('waiting on dependencies, elapsed time', () => {
    const status = new Map([
      ['a', 'SUCCEEDED'],
      ['b', 'RUNNING'],
    ]);
    expect(waitingOn({ status: 'PENDING', depends_on: ['a', 'b'] }, status)).toEqual(['b']);
    expect(waitingOn({ status: 'READY', depends_on: ['b'] }, status)).toEqual([]);
    expect(elapsedMs('2026-11-08T09:00:00Z', '2026-11-08T09:03:20Z', 0)).toBe(200_000);
    expect(elapsedMs(null, null, 0)).toBeNull();
    expect(durationParts(3_725_000)).toEqual({ h: 1, m: 2, s: 5 });
  });
});

describe('command center quick actions', () => {
  it('map onto existing directive types; critical ones need confirmation before sending', () => {
    expect(QUICK_ACTIONS.map((a) => QUICK_ACTION_MAP[a].type)).toEqual(['ASK', 'NEW_TASK', 'CHANGE_REQUEST', 'REVIEW', 'PAUSE', 'RESUME', 'CHANGE_REQUEST']);
    expect(QUICK_ACTION_MAP.rework.target).toBe('task');
    expect(needsPreConfirm('PAUSE', true)).toBe(true);
    expect(needsPreConfirm('STOP', true)).toBe(true);
    expect(needsPreConfirm('REJECT', true)).toBe(true);
    // С impact preview подтверждение — по результатам анализа (confirmDirective), без — сразу.
    expect(needsPreConfirm('CHANGE_REQUEST', true)).toBe(false);
    expect(needsPreConfirm('CHANGE_REQUEST', false)).toBe(true);
    expect(needsPreConfirm('ASK', false)).toBe(false);
    expect(needsPreConfirm('RESUME', false)).toBe(false);
  });
});
