/**
 * Чистые модели AI Mission Control: строки вида «Вывод CLI», фильтры журнала событий и измеренные
 * индикаторы узлов DAG. Только данные API — никаких оценок: прогресс задачи = доля проверок,
 * подтверждённых VERIFIED evidence (D-8: PASS без VERIFIED evidence ≠ «пройдено»); нет данных — null.
 */
import type { AgentRole, TaskSummary } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import type { Evidence, GateExecution } from '../../api/tasks';
import { gateVerificationState } from '../../lib/verification';
import { EVENT_GROUP_TYPES, type FeedFilters } from './mcParams';

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

/** Роль события (task/cli — agent_role, ACP — from.role). */
export function eventRole(e: RunEvent): string | null {
  const p = e.payload;
  if (str(p.agent_role)) return str(p.agent_role);
  if (typeof p.from === 'object' && p.from !== null) return str((p.from as Record<string, unknown>).role);
  return str(p.sender_role);
}

function eventTime(e: RunEvent): number {
  const ts = Date.parse(e.timestamp);
  return Number.isFinite(ts) ? ts : 0;
}

/** Фильтр журнала событий run по группам типов, роли, задаче и времени. */
export function filterEvents(events: readonly RunEvent[], f: FeedFilters, cutoff: number | null): RunEvent[] {
  const types = f.groups && f.groups.length > 0 ? new Set(f.groups.flatMap((g) => EVENT_GROUP_TYPES[g])) : null;
  return events.filter(
    (e) =>
      (!types || types.has(e.type)) &&
      (!f.role || eventRole(e) === f.role) &&
      (!f.task || e.payload.task_id === f.task) &&
      (cutoff === null || eventTime(e) >= cutoff),
  );
}

export interface CliLine {
  id: string;
  seq: number;
  timestamp: string;
  taskId: string | null;
  provider: string | null;
  role: AgentRole | string | null;
  /** stdout | stderr | agent_message; null — служебное событие CLI (started/finished/…). */
  stream: string | null;
  eventType: string;
  text: string;
  truncated: boolean;
  redactions: number;
  dropped: number;
}

/** Строки вида «Вывод CLI»: санитизированные `cli.output` (сервер уже редактирует секреты). */
export function cliLines(events: readonly RunEvent[], f: FeedFilters, cutoff: number | null): CliLine[] {
  const q = f.q?.toLocaleLowerCase('ru');
  const out: CliLine[] = [];
  for (const e of events) {
    if (e.type !== 'cli.output') continue;
    const p = e.payload;
    const role = str(p.agent_role);
    const taskId = str(p.task_id);
    if (f.role && role !== f.role) continue;
    if (f.task && taskId !== f.task) continue;
    if (cutoff !== null && eventTime(e) < cutoff) continue;
    const eventType = str(p.cli_event_type) ?? 'output';
    const text = typeof p.text === 'string' ? p.text : '';
    if (q && !text.toLocaleLowerCase('ru').includes(q)) continue;
    out.push({
      id: e.event_id,
      seq: e.seq,
      timestamp: e.timestamp,
      taskId,
      provider: str(p.provider),
      role,
      stream: str(p.stream),
      eventType,
      text,
      truncated: p.truncated === true,
      redactions: typeof p.redactions === 'number' ? p.redactions : 0,
      dropped: typeof p.dropped_chunks === 'number' ? p.dropped_chunks : 0,
    });
  }
  return out;
}

export interface TaskIndicators {
  /** Проверки задачи (gate executions), подтверждённые VERIFIED evidence. */
  verified: number;
  /** Все проверки задачи в отчёте качества. */
  gates: number;
  failed: number;
  blocked: number;
  /** Проверки-тесты (gate_type содержит TEST): всего / подтверждено. */
  tests: { total: number; verified: number };
  /** Evidence задачи в выборке run (null — evidence не загружались / нет права). */
  evidence: { total: number; verified: number } | null;
}

/**
 * Измеренные индикаторы задач по отчёту качества run и evidence run. `gates === null` (нет отчёта,
 * нет права quality:read) — индикаторы не строятся (UI их не показывает, а не рисует нули).
 */
export function taskIndicators(
  tasks: readonly Pick<TaskSummary, 'id' | 'status'>[],
  gates: readonly GateExecution[] | null,
  evidence: readonly Evidence[] | null,
): Map<string, TaskIndicators> {
  const out = new Map<string, TaskIndicators>();
  if (!gates) return out;
  const evidenceStatus = new Map((evidence ?? []).map((e) => [e.id, e.verification_status]));
  for (const task of tasks) {
    const mine = gates.filter((g) => g.task_id === task.id);
    const states = mine.map((g) => ({
      g,
      state: gateVerificationState(g, g.evidence_id ? (evidenceStatus.get(g.evidence_id) ?? null) : null, {
        blockedUpstream: task.status === 'BLOCKED',
      }).state,
    }));
    const tests = states.filter(({ g }) => /TEST/i.test(g.gate_type));
    const ev = evidence ? evidence.filter((e) => e.task_id === task.id) : null;
    out.set(task.id, {
      verified: states.filter((s) => s.state === 'PASS').length,
      gates: states.length,
      failed: states.filter((s) => s.state === 'FAIL').length,
      blocked: states.filter((s) => s.state === 'BLOCKED').length,
      tests: { total: tests.length, verified: tests.filter((s) => s.state === 'PASS').length },
      evidence: ev ? { total: ev.length, verified: ev.filter((e) => e.verification_status === 'VERIFIED').length } : null,
    });
  }
  return out;
}

/** Очередь/ожидание задачи по DAG: PENDING с незавершёнными зависимостями — «ждёт зависимости». */
export function waitingOn(task: Pick<TaskSummary, 'status' | 'depends_on'>, statusById: ReadonlyMap<string, string>): string[] {
  if (task.status !== 'PENDING') return [];
  return task.depends_on.filter((id) => statusById.get(id) !== 'SUCCEEDED');
}

/** Длительность в мс между началом и концом (или сейчас); null — начала нет. */
export function elapsedMs(startedAt: string | null | undefined, finishedAt: string | null | undefined, now: number): number | null {
  if (!startedAt) return null;
  const start = Date.parse(startedAt);
  const end = finishedAt ? Date.parse(finishedAt) : now;
  return Number.isFinite(start) && Number.isFinite(end) ? Math.max(0, end - start) : null;
}

/** Части длительности для подписи (строки — i18n `missionControl.duration.*`). */
export function durationParts(ms: number): { h: number; m: number; s: number } {
  const total = Math.floor(ms / 1000);
  return { h: Math.floor(total / 3600), m: Math.floor((total % 3600) / 60), s: total % 60 };
}
