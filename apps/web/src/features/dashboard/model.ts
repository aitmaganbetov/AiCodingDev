/**
 * Bento Dashboard (F3, план §3.2): чистые функции сопоставления ответов API с плитками.
 * Правила: только реальные данные; `null` ≠ 0 (нет данных — отдельное состояние); неполная выборка
 * (есть next_cursor) — «N+»; никакого «Quality Score» (D-6) — только измеренные доли с подписью;
 * состояния гейтов — по отображению D-8 (lib/verification.ts).
 */
import type { AnalyticsQuery, AnalyticsSummary } from '../../api/analytics';
import type { Approval } from '../../api/approvals';
import type { Finding, FindingSeverity } from '../../api/findings';
import type { Provider } from '../../api/providers';
import type { ActivityEvent, ActivityEventType, CliProvider, Run, RunStatus, RunSummary } from '../../api/runs';
import type { StreamEvent } from '../../api/sse';
import type { SystemHealthStatus } from '../../api/system';
import type { WorkspaceTask } from '../../api/tasks';
import type { Tone } from '../../components/ui/Badge';
import { providerChipModel, type ProviderChipState } from '../../layout/providerStatus';
import { gateAggregateOutcomes, type GateAggregateOutcome } from '../../lib/verification';

/* ── Окно аналитики ─────────────────────────────────────────────────────────────────────────── */

export const DASHBOARD_WINDOW_DAYS = 30;
const DAY_MS = 86_400_000;

/**
 * Окно GET /analytics/summary: последние 30 дней, конец — начало следующего часа UTC (стабильный
 * ключ кэша в пределах часа; так же считает экран Analytics).
 */
export function dashboardWindow(now: Date = new Date()): Required<Pick<AnalyticsQuery, 'from' | 'to'>> {
  const end = new Date(now);
  end.setUTCMinutes(0, 0, 0);
  end.setUTCHours(end.getUTCHours() + 1);
  const start = new Date(end.getTime() - DASHBOARD_WINDOW_DAYS * DAY_MS);
  return { from: start.toISOString(), to: end.toISOString() };
}

/* ── Счётчики по страницам ──────────────────────────────────────────────────────────────────── */

export interface Count {
  value: number;
  /** Выборка неполная (есть следующая страница) → показывать «N+». */
  more: boolean;
}

export function pageCount(page: { items: readonly unknown[]; next_cursor: string | null }): Count {
  return { value: page.items.length, more: page.next_cursor !== null };
}

/* ── Активные запуски и проекты ─────────────────────────────────────────────────────────────── */

/** Нетерминальные статусы run: идут сейчас или ждут человека. */
export const ACTIVE_RUN_STATUSES: readonly RunStatus[] = [
  'CREATED',
  'DISCOVERY',
  'PLANNED',
  'RUNNING',
  'VERIFYING',
  'AWAITING_APPROVAL',
  'BLOCKED',
  'PAUSED',
];
/** Сколько последних активных runs запрашивает Dashboard (одна страница listRuns). */
export const ACTIVE_RUNS_LIMIT = 50;
/** Для скольких из них загружается getRun (task_counts) — N+1 ограничен. */
export const WORKFLOW_DETAILS_LIMIT = 6;

/** Проекты с активным run среди выборки активных runs. */
export function projectsWithActiveRuns(runs: readonly Pick<RunSummary, 'project_id'>[]): number {
  return new Set(runs.map((r) => r.project_id)).size;
}

export interface RunProgress {
  /** Задачи SUCCEEDED (подтверждены платформой после обязательных gates). */
  verified: number;
  total: number;
  ratio: number;
  /** Задачи в работе (LEASED/RUNNING/VERIFYING) — информативно, в прогресс не входят. */
  inProgress: number;
  failed: number;
  blocked: number;
}

/**
 * Прогресс run по task_counts: только подтверждённые шаги (SUCCEEDED) / все задачи плана.
 * Нет task_counts или плана (0 задач) → null (а не 0 %).
 */
export function runProgress(run: Pick<Run, 'task_counts'> | undefined): RunProgress | null {
  const counts = run?.task_counts;
  if (!counts) return null;
  const total = Object.values(counts).reduce((a, v) => a + (Number.isFinite(v) ? v : 0), 0);
  if (total <= 0) return null;
  const n = (k: string) => counts[k] ?? 0;
  const verified = n('SUCCEEDED');
  return {
    verified,
    total,
    ratio: verified / total,
    inProgress: n('LEASED') + n('RUNNING') + n('VERIFYING'),
    failed: n('FAILED'),
    blocked: n('BLOCKED'),
  };
}

/* ── Исполняющиеся задачи (Running AI Agents) ───────────────────────────────────────────────── */

export const RUNNING_TASK_STATUSES = ['LEASED', 'RUNNING'] as const;
export const RUNNING_TASKS_LIMIT = 50;

export type ProviderKey = CliProvider | 'unknown';

/** Провайдер исполнения: последней попытки, иначе назначенный (`current_provider`), иначе — неизвестен. */
export function taskProvider(task: Pick<WorkspaceTask, 'current_attempt' | 'current_provider'>): ProviderKey {
  return task.current_attempt?.provider ?? task.current_provider ?? 'unknown';
}

export interface Breakdown<K extends string> {
  key: K;
  count: number;
}

function breakdown<K extends string>(keys: readonly K[]): Breakdown<K>[] {
  const map = new Map<K, number>();
  for (const k of keys) map.set(k, (map.get(k) ?? 0) + 1);
  return [...map.entries()].map(([key, count]) => ({ key, count })).sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

export function runningAgents(tasks: readonly WorkspaceTask[]) {
  return {
    byProvider: breakdown(tasks.map(taskProvider)),
    byRole: breakdown(tasks.map((t) => t.agent_role)),
  };
}

/* ── Находки (Security Findings) ────────────────────────────────────────────────────────────── */

export const SEVERITY_ORDER: readonly FindingSeverity[] = ['critical', 'high', 'medium', 'low', 'info'];
export const FINDINGS_LIMIT = 200;

export interface SeverityCount {
  severity: FindingSeverity;
  /** null — не подсчитано (выборка оборвалась на более высокой серьёзности). */
  count: number | null;
  /** Нижняя граница (выборка оборвалась на этой серьёзности) → «N+». */
  more: boolean;
}

/**
 * Открытые находки по серьёзности из одной страницы GET /findings (порядок контракта — severity desc).
 * Если есть следующая страница: последняя увиденная серьёзность — «N+», более низкие — не подсчитаны.
 */
export function findingsBySeverity(page: { items: readonly Pick<Finding, 'severity'>[]; next_cursor: string | null }): SeverityCount[] {
  const counts = new Map<FindingSeverity, number>();
  for (const f of page.items) counts.set(f.severity, (counts.get(f.severity) ?? 0) + 1);
  if (page.next_cursor === null) return SEVERITY_ORDER.map((severity) => ({ severity, count: counts.get(severity) ?? 0, more: false }));
  const last = page.items.at(-1)?.severity;
  const cut = last ? SEVERITY_ORDER.indexOf(last) : -1;
  return SEVERITY_ORDER.map((severity, i) => {
    if (cut < 0 || i > cut) return { severity, count: null, more: false };
    return { severity, count: counts.get(severity) ?? 0, more: i === cut };
  });
}

/* ── Approvals ──────────────────────────────────────────────────────────────────────────────── */

export const APPROVALS_LIMIT = 50;

export function approvalsSummary(page: { items: readonly Pick<Approval, 'status' | 'can_decide' | 'risk_level'>[]; next_cursor: string | null }) {
  const pending = page.items.filter((a) => a.status === 'PENDING');
  return {
    total: { value: pending.length, more: page.next_cursor !== null } satisfies Count,
    mine: pending.filter((a) => a.can_decide).length,
    highRisk: pending.filter((a) => a.risk_level === 'high' || a.risk_level === 'critical').length,
  };
}

/* ── Качество (D-6, D-8) ────────────────────────────────────────────────────────────────────── */

type Gates = AnalyticsSummary['gates'];

export interface GateTotals {
  total: number;
  /** Статус контракта PASS (без учёта evidence). */
  passStatus: number;
  /** PASS с evidence VERIFIED — «Пройдено» (D-8). */
  passVerified: number;
  /** PASS без VERIFIED evidence — «Недостаточно доказательств» (D-8). */
  passUnverified: number;
  failed: number;
  errored: number;
  /** SKIPPED и NOT_APPLICABLE (контракт объединяет их; не входят в знаменатель). */
  skipped: number;
  /** Исполненные проверки: PASS + FAIL + ERROR (знаменатель долей). */
  executed: number;
  /** Доля «Пройдено» (PASS + VERIFIED evidence) среди исполненных; null — исполненных нет. */
  verifiedRate: number | null;
}

type GateCountKey = 'total' | 'passed' | 'passed_verified' | 'passed_unverified' | 'failed' | 'errored' | 'skipped';

export function gateTotals(gates: Gates): GateTotals {
  const sum = (k: GateCountKey) => gates.reduce((a, g) => a + g[k], 0);
  const passStatus = sum('passed');
  const passVerified = sum('passed_verified');
  const failed = sum('failed');
  const errored = sum('errored');
  const executed = passStatus + failed + errored;
  return {
    total: sum('total'),
    passStatus,
    passVerified,
    passUnverified: sum('passed_unverified'),
    failed,
    errored,
    skipped: sum('skipped'),
    executed,
    verifiedRate: executed > 0 ? passVerified / executed : null,
  };
}

/** Доля «Пройдено» (PASS + VERIFIED evidence) одного типа гейтов среди исполненных; null — исполненных нет. */
export function gateVerifiedRate(g: Gates[number]): number | null {
  const executed = g.passed + g.failed + g.errored;
  return executed > 0 ? g.passed_verified / executed : null;
}

export type GateOutcome = GateAggregateOutcome;

/**
 * Пять состояний верификации по агрегату analytics.gates (D-8, `lib/verification`): PASS делится сервером
 * по статусу evidence (`passed_verified` → «Пройдено», `passed_unverified` → «Недостаточно доказательств»),
 * FAIL ← FAIL, BLOCKED ← ERROR, NOT RUN ← SKIPPED/NOT_APPLICABLE.
 */
export function gateOutcomes(gates: Gates): GateOutcome[] {
  const t = gateTotals(gates);
  return gateAggregateOutcomes({
    passed_verified: t.passVerified,
    passed_unverified: t.passUnverified,
    failed: t.failed,
    errored: t.errored,
    skipped: t.skipped,
  });
}

/* ── CLI-провайдеры ─────────────────────────────────────────────────────────────────────────── */

export interface ProviderHealthRow {
  code: string;
  name: string;
  state: ProviderChipState;
  checkedAt: string | null;
  hosts: number;
  /** Из analytics.providers за окно; null — нет исполнений/данных. */
  executions: number | null;
  successRate: number | null;
}

/** Строки «здоровья» провайдеров: состояние — из listProviders (как чипы оболочки), доли — из analytics. */
export function providerHealthRows(providers: readonly Provider[], usage: AnalyticsSummary['providers'] | undefined): ProviderHealthRow[] {
  return providers.map((p) => {
    const model = providerChipModel(p);
    const u = usage?.find((x) => x.provider === p.code);
    return {
      code: p.code,
      name: p.display_name,
      state: model.state,
      checkedAt: model.checkedAt,
      hosts: p.installations.length,
      executions: u ? u.executions : null,
      successRate: u ? u.success_rate : null,
    };
  });
}

export function readyProviders(rows: readonly ProviderHealthRow[]): number {
  return rows.filter((r) => r.state === 'ready').length;
}

/* ── System Health (B2) ─────────────────────────────────────────────────────────────────────── */

export const HEALTH_TONE: Record<SystemHealthStatus, Tone> = { ok: 'success', degraded: 'warning', down: 'danger', unknown: 'neutral' };

/** Неизвестный статус (расширение enum) — «неизвестно», никогда не «ok». */
export function healthStatus(status: string): SystemHealthStatus {
  return status === 'ok' || status === 'degraded' || status === 'down' ? status : 'unknown';
}

/* ── Лента активности ───────────────────────────────────────────────────────────────────────── */

export const ACTIVITY_CATEGORIES = {
  runs: ['run.state_changed', 'run.completed'],
  tasks: ['task.state_changed'],
  agents: ['agent.message'],
  approvals: ['approval.requested', 'approval.decided'],
  quality: ['gate.completed'],
  directives: ['directive.impact_ready', 'directive.state_changed'],
  chat: ['chat.message'],
  cli: ['cli.output'],
} as const satisfies Record<string, readonly ActivityEventType[]>;

export type ActivityCategory = keyof typeof ACTIVITY_CATEGORIES;
export const ALL_CATEGORIES = Object.keys(ACTIVITY_CATEGORIES) as ActivityCategory[];
/** По умолчанию — всё, кроме вывода CLI (его много; поток CLI — экран run / Mission Control). */
export const DEFAULT_CATEGORIES: readonly ActivityCategory[] = ALL_CATEGORIES.filter((c) => c !== 'cli');
export const ACTIVITY_LIMIT = 100;

export function eventCategory(type: string): ActivityCategory | null {
  for (const c of ALL_CATEGORIES) if ((ACTIVITY_CATEGORIES[c] as readonly string[]).includes(type)) return c;
  return null;
}

/** Параметр `event_type` запроса; все категории — без фильтра (undefined). */
export function eventTypesFor(categories: readonly ActivityCategory[]): ActivityEventType[] | undefined {
  if (ALL_CATEGORIES.every((c) => categories.includes(c))) return undefined;
  return ALL_CATEGORIES.filter((c) => categories.includes(c)).flatMap((c) => [...ACTIVITY_CATEGORIES[c]]);
}

export interface TimelineItem {
  id: string;
  runId: string;
  taskId: string | null;
  type: string;
  occurredAt: string;
  payload: Record<string, unknown>;
  /** Получено по SSE после загрузки страницы. */
  live: boolean;
}

export function fromActivity(e: ActivityEvent): TimelineItem {
  return { id: e.id, runId: e.run_id, taskId: e.task_id ?? null, type: e.event_type, occurredAt: e.occurred_at, payload: e.payload, live: false };
}

export function fromStream(e: StreamEvent): TimelineItem {
  const taskId = typeof e.payload.task_id === 'string' ? e.payload.task_id : null;
  return { id: e.event_id, runId: e.run_id, taskId, type: e.type, occurredAt: e.timestamp, payload: e.payload, live: true };
}

/**
 * Лента: события REST + события SSE (только выбранных категорий), без дублей (id события = event_id),
 * новые сверху. Событие из REST вытесняет такое же из SSE (без пометки «новое»).
 */
export function mergeTimeline(live: readonly TimelineItem[], page: readonly TimelineItem[], categories: readonly ActivityCategory[]): TimelineItem[] {
  const byId = new Map<string, TimelineItem>();
  for (const item of live) {
    const c = eventCategory(item.type);
    if (c && categories.includes(c)) byId.set(item.id, item);
  }
  for (const item of page) byId.set(item.id, item);
  return [...byId.values()].sort((a, b) => Date.parse(b.occurredAt) - Date.parse(a.occurredAt) || b.id.localeCompare(a.id));
}

/** Строковое поле payload (или null). */
export function str(payload: Record<string, unknown>, key: string): string | null {
  const v = payload[key];
  return typeof v === 'string' && v.length > 0 ? v : null;
}
