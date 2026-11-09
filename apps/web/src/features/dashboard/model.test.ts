import { describe, expect, it } from 'vitest';
import type { AnalyticsSummary } from '../../api/analytics';
import type { Provider } from '../../api/providers';
import type { StreamEvent } from '../../api/sse';
import type { WorkspaceTask } from '../../api/tasks';
import { formatPercent } from './format';
import {
  ALL_CATEGORIES,
  DEFAULT_CATEGORIES,
  approvalsSummary,
  dashboardWindow,
  eventCategory,
  eventTypesFor,
  findingsBySeverity,
  fromActivity,
  fromStream,
  gateOutcomes,
  gateTotals,
  gateVerifiedRate,
  healthStatus,
  mergeTimeline,
  pageCount,
  projectsWithActiveRuns,
  providerHealthRows,
  readyProviders,
  runProgress,
  runningAgents,
  taskProvider,
} from './model';

type Gate = AnalyticsSummary['gates'][number];
/** `unverified` — сколько из `passed` без VERIFIED evidence (D-8). */
const gate = (gate_type: string, passed: number, failed: number, errored: number, skipped: number, unverified = 0): Gate => ({
  gate_type,
  total: passed + failed + errored + skipped,
  passed,
  passed_verified: passed - unverified,
  passed_unverified: unverified,
  failed,
  errored,
  skipped,
  pass_rate: passed + failed + errored > 0 ? passed / (passed + failed + errored) : null,
  avg_duration_seconds: null,
});

describe('dashboardWindow', () => {
  it('30 дней, конец — начало следующего часа UTC', () => {
    const w = dashboardWindow(new Date('2026-11-09T10:17:42Z'));
    expect(w).toEqual({ from: '2026-10-10T11:00:00.000Z', to: '2026-11-09T11:00:00.000Z' });
  });
});

describe('counts', () => {
  it('pageCount: next_cursor → «N+»', () => {
    expect(pageCount({ items: [1, 2], next_cursor: null })).toEqual({ value: 2, more: false });
    expect(pageCount({ items: [1, 2], next_cursor: 'c' })).toEqual({ value: 2, more: true });
    expect(pageCount({ items: [], next_cursor: null })).toEqual({ value: 0, more: false });
  });

  it('projectsWithActiveRuns — уникальные проекты', () => {
    expect(projectsWithActiveRuns([{ project_id: 'a' }, { project_id: 'b' }, { project_id: 'a' }])).toBe(2);
    expect(projectsWithActiveRuns([])).toBe(0);
  });
});

describe('runProgress (только подтверждённые шаги)', () => {
  it('SUCCEEDED / все задачи плана; в работе и ошибки — отдельно', () => {
    expect(runProgress({ task_counts: { SUCCEEDED: 3, RUNNING: 1, LEASED: 1, FAILED: 1, BLOCKED: 2, PENDING: 2 } })).toEqual({
      verified: 3,
      total: 10,
      ratio: 0.3,
      inProgress: 2,
      failed: 1,
      blocked: 2,
    });
  });

  it('нет плана или task_counts → null, а не 0 %', () => {
    expect(runProgress({ task_counts: {} })).toBeNull();
    expect(runProgress({})).toBeNull();
    expect(runProgress(undefined)).toBeNull();
  });

  it('VERIFYING не считается подтверждённым', () => {
    expect(runProgress({ task_counts: { VERIFYING: 4 } })?.verified).toBe(0);
  });
});

describe('running agents', () => {
  const task = (role: WorkspaceTask['agent_role'], attempt: 'codex' | 'claude' | null, current: 'codex' | 'claude' | null = null) =>
    ({
      agent_role: role,
      current_provider: current,
      current_attempt: attempt === null && current === null ? null : { id: 'a', attempt_no: 1, status: 'RUNNING', provider: attempt, started_at: '', finished_at: null },
    }) as unknown as WorkspaceTask;

  it('провайдер — последней попытки, затем назначенный, иначе unknown', () => {
    expect(taskProvider(task('developer', 'claude', 'codex'))).toBe('claude');
    expect(taskProvider(task('developer', null, 'codex'))).toBe('codex');
    expect(taskProvider(task('developer', null))).toBe('unknown');
  });

  it('разбивка по провайдерам и ролям (по убыванию)', () => {
    const r = runningAgents([task('developer', 'codex'), task('reviewer', 'claude'), task('developer', 'codex'), task('qa', null)]);
    expect(r.byProvider).toEqual([
      { key: 'codex', count: 2 },
      { key: 'claude', count: 1 },
      { key: 'unknown', count: 1 },
    ]);
    expect(r.byRole[0]).toEqual({ key: 'developer', count: 2 });
  });
});

describe('findingsBySeverity', () => {
  const f = (severity: 'critical' | 'high' | 'medium' | 'low' | 'info') => ({ severity });

  it('полная выборка — точные числа (включая 0)', () => {
    expect(findingsBySeverity({ items: [f('critical'), f('high'), f('high'), f('low')], next_cursor: null })).toEqual([
      { severity: 'critical', count: 1, more: false },
      { severity: 'high', count: 2, more: false },
      { severity: 'medium', count: 0, more: false },
      { severity: 'low', count: 1, more: false },
      { severity: 'info', count: 0, more: false },
    ]);
  });

  it('неполная выборка: последняя серьёзность — «N+», ниже — не подсчитано', () => {
    expect(findingsBySeverity({ items: [f('critical'), f('high'), f('high')], next_cursor: 'c' })).toEqual([
      { severity: 'critical', count: 1, more: false },
      { severity: 'high', count: 2, more: true },
      { severity: 'medium', count: null, more: false },
      { severity: 'low', count: null, more: false },
      { severity: 'info', count: null, more: false },
    ]);
  });
});

describe('approvalsSummary', () => {
  it('только PENDING; моё решение и высокий риск', () => {
    const s = approvalsSummary({
      items: [
        { status: 'PENDING', can_decide: true, risk_level: 'critical' },
        { status: 'PENDING', can_decide: false, risk_level: 'low' },
        { status: 'APPROVED', can_decide: false, risk_level: 'high' },
      ],
      next_cursor: null,
    });
    expect(s).toEqual({ total: { value: 2, more: false }, mine: 1, highRisk: 1 });
  });
});

describe('качество: D-6 и D-8', () => {
  it('gateTotals: доля «Пройдено» (PASS + VERIFIED) среди исполненных, SKIPPED не в знаменателе', () => {
    const t = gateTotals([gate('UNIT', 6, 1, 1, 2, 2), gate('SAST', 2, 0, 0, 3)]);
    expect(t).toEqual({
      total: 15,
      passStatus: 8,
      passVerified: 6,
      passUnverified: 2,
      failed: 1,
      errored: 1,
      skipped: 5,
      executed: 10,
      verifiedRate: 0.6,
    });
  });

  it('gateTotals: только SKIPPED → доля null (не 0 и не 100 %)', () => {
    expect(gateTotals([gate('UNIT', 0, 0, 0, 4)]).verifiedRate).toBeNull();
    expect(gateTotals([]).verifiedRate).toBeNull();
  });

  it('gateVerifiedRate: PASS без VERIFIED evidence не входит в числитель', () => {
    expect(gateVerifiedRate(gate('UNIT', 4, 1, 0, 2, 1))).toBe(0.6);
    expect(gateVerifiedRate(gate('UNIT', 0, 0, 0, 2))).toBeNull();
  });

  it('gateOutcomes: passed_verified → «Пройдено», passed_unverified → «Недостаточно доказательств», ERROR → BLOCKED, SKIPPED → NOT RUN', () => {
    const o = gateOutcomes([gate('UNIT', 5, 2, 1, 3, 2), gate('SAST', 1, 0, 0, 0, 1)]);
    expect(o).toEqual([
      { state: 'PASS', count: 3, source: 'PASS + VERIFIED evidence' },
      { state: 'INSUFFICIENT_EVIDENCE', count: 3, source: 'PASS' },
      { state: 'FAIL', count: 2, source: 'FAIL' },
      { state: 'BLOCKED', count: 1, source: 'ERROR' },
      { state: 'NOT_RUN', count: 3, source: 'SKIPPED / NOT_APPLICABLE' },
    ]);
    // PASS без проверенного evidence никогда не становится зелёным «Пройдено».
    expect(gateOutcomes([gate('X', 100, 0, 0, 0, 100)]).find((x) => x.state === 'PASS')?.count).toBe(0);
  });

  it('formatPercent: null → null (нет данных), а не 0 %', () => {
    expect(formatPercent(null)).toBeNull();
    expect(formatPercent(Number.NaN)).toBeNull();
    expect(formatPercent(0)).toBe('0%');
    expect(formatPercent(0.875)).toBe('87,5%');
  });
});

describe('providerHealthRows', () => {
  const provider = (code: 'codex' | 'claude', auth: 'AUTHENTICATED' | 'AUTH_REQUIRED', checked: string | null): Provider => ({
    provider_id: `p-${code}`,
    code,
    display_name: code === 'codex' ? 'Codex CLI' : 'Claude Code CLI',
    enabled_globally: true,
    installations: [
      {
        installation_id: `i-${code}`,
        host: 'worker-1',
        detected_version: '1.0.0',
        compatibility: 'SUPPORTED',
        auth_status: auth,
        availability: 'AVAILABLE',
        capabilities: {},
        last_error_category: null,
        checked_at: checked,
      },
    ],
    workspace_settings: { enabled: true } as Provider['workspace_settings'],
  });

  it('состояние — из listProviders, доли — из analytics; без исполнений — null', () => {
    const usage = [{ provider: 'codex', executions: 10, success_rate: 0.9 }] as AnalyticsSummary['providers'];
    const rows = providerHealthRows([provider('codex', 'AUTHENTICATED', '2026-11-09T09:00:00Z'), provider('claude', 'AUTH_REQUIRED', '2026-11-09T09:00:00Z')], usage);
    expect(rows.map((r) => [r.code, r.state, r.executions, r.successRate])).toEqual([
      ['codex', 'ready', 10, 0.9],
      ['claude', 'auth_required', null, null],
    ]);
    expect(readyProviders(rows)).toBe(1);
  });

  it('без проверки — «неизвестно», а не «готов»', () => {
    expect(providerHealthRows([provider('codex', 'AUTHENTICATED', null)], undefined)[0]?.state).toBe('unknown');
  });
});

describe('healthStatus', () => {
  it('неизвестное значение — unknown, никогда не ok', () => {
    expect(healthStatus('ok')).toBe('ok');
    expect(healthStatus('down')).toBe('down');
    expect(healthStatus('maintenance')).toBe('unknown');
    expect(healthStatus('unknown')).toBe('unknown');
  });
});

describe('лента активности', () => {
  it('категории и параметр event_type', () => {
    expect(eventCategory('gate.completed')).toBe('quality');
    expect(eventCategory('approval.decided')).toBe('approvals');
    expect(eventCategory('something.new')).toBeNull();
    expect(eventTypesFor(ALL_CATEGORIES)).toBeUndefined();
    expect(eventTypesFor(['runs', 'approvals'])).toEqual(['run.state_changed', 'run.completed', 'approval.requested', 'approval.decided']);
    expect(DEFAULT_CATEGORIES).not.toContain('cli');
    expect(eventTypesFor(DEFAULT_CATEGORIES)).not.toContain('cli.output');
  });

  it('mergeTimeline: без дублей (id = event_id), новые сверху, SSE — только выбранные категории', () => {
    const rest = fromActivity({ id: 'e1', run_id: 'r1', seq: 1, event_type: 'run.state_changed', payload: {}, occurred_at: '2026-11-09T09:00:00Z' });
    const sse = (id: string, type: string, ts: string): StreamEvent => ({ event_id: id, run_id: 'r1', seq: 2, type, timestamp: ts, schema_version: '1.0', payload: { task_id: 't1' } });
    const live = [sse('e1', 'run.state_changed', '2026-11-09T09:00:00Z'), sse('e2', 'approval.requested', '2026-11-09T09:05:00Z'), sse('e3', 'run.completed', '2026-11-09T09:06:00Z')].map(fromStream);
    expect(live[0]?.taskId).toBe('t1');
    const merged = mergeTimeline(live, [rest], ['runs']);
    expect(merged.map((i) => [i.id, i.live])).toEqual([
      ['e3', true],
      ['e1', false],
    ]);
  });
});
