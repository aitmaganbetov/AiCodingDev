/**
 * Модель экрана «Агенты» (F5, план §3.4, §5.7; D-2). Роли — логические профили (AgentRole), а не
 * постоянно работающие сервисы: «активна» = есть задача роли в статусе LEASED/RUNNING (B1 `GET /tasks`).
 * Никаких «онлайн»-статусов, нулей вместо отсутствующих данных и выдуманных значений.
 */
import type { AgentProfile } from '../../api/agents';
import type { AnalyticsSummary } from '../../api/analytics';
import type { Provider } from '../../api/providers';
import type { CliProvider } from '../../api/runs';
import type { WorkspaceTask, WorkspaceTaskPage } from '../../api/tasks';
import { AGENT_ROLE_ORDER, isAgentRole, type AgentRole } from '../../lib/agentRoles';

export const ACTIVE_TASK_STATUSES = ['LEASED', 'RUNNING'] as const;
/** Одна страница выполняемых задач на весь каталог (максимум контракта — 200). */
export const ACTIVE_TASKS_LIMIT = 200;
export const ROLE_HISTORY_PAGE = 25;
/** Сколько текущих задач показывать в карточке роли. */
export const CARD_TASKS_SHOWN = 2;

/* ── Вид каталога: сетка / список (URL `view`, иначе сохранённый выбор устройства) ───────────── */

export type AgentsView = 'grid' | 'list';
/** Префикс `aicd.layout.` — «Сбросить раскладку» (Настройки › Внешний вид) очищает и этот выбор. */
export const AGENTS_VIEW_KEY = 'aicd.layout.agents.view';

export function parseView(value: string | null | undefined): AgentsView | null {
  return value === 'grid' || value === 'list' ? value : null;
}

export function readStoredView(): AgentsView | null {
  try {
    return parseView(window.localStorage.getItem(AGENTS_VIEW_KEY));
  } catch {
    return null;
  }
}

export function storeView(view: AgentsView): void {
  try {
    window.localStorage.setItem(AGENTS_VIEW_KEY, view);
  } catch {
    /* хранилище недоступно — выбор действует до перезагрузки (он есть в URL) */
  }
}

/** URL приоритетнее сохранённого выбора; по умолчанию — сетка. */
export function resolveView(param: string | null, stored: AgentsView | null): AgentsView {
  return parseView(param) ?? stored ?? 'grid';
}

/* ── Активность ролей ───────────────────────────────────────────────────────────────────────── */

export interface RoleActivity {
  /** Выполняемые задачи роли (из загруженной выборки). */
  tasks: WorkspaceTask[];
  /** Выборка неполная (есть next_cursor) → счётчик «N+». */
  more: boolean;
}

/** Группировка выполняемых задач по 12 ролям; значения вне AgentRole («Система») не учитываются. */
export function activityByRole(page: WorkspaceTaskPage | undefined): Map<AgentRole, RoleActivity> | null {
  if (!page) return null;
  const more = page.next_cursor !== null;
  const out = new Map<AgentRole, RoleActivity>(AGENT_ROLE_ORDER.map((r) => [r, { tasks: [], more }]));
  for (const task of page.items) {
    if (!isAgentRole(task.agent_role)) continue;
    if (!(ACTIVE_TASK_STATUSES as readonly string[]).includes(task.status)) continue;
    out.get(task.agent_role)?.tasks.push(task);
  }
  return out;
}

export type RoleState = 'loading' | 'unknown' | 'active' | 'idle';

/**
 * Состояние роли только по реальным задачам: нет данных (загрузка/ошибка) ≠ «не активна».
 * При неполной выборке без задач роли — «неизвестно» (задача могла не попасть в выборку).
 */
export function roleState(activity: RoleActivity | undefined, { pending, error }: { pending: boolean; error: boolean }): RoleState {
  if (pending) return 'loading';
  if (error || !activity) return 'unknown';
  if (activity.tasks.length > 0) return 'active';
  return activity.more ? 'unknown' : 'idle';
}

/** Сколько ролей активно; `more` — нижняя оценка (выборка неполная). */
export function activeRoleCount(activity: Map<AgentRole, RoleActivity> | null): { count: number; more: boolean } | null {
  if (!activity) return null;
  let count = 0;
  let more = false;
  for (const a of activity.values()) {
    if (a.tasks.length > 0) count += 1;
    more = more || a.more;
  }
  return { count, more };
}

/* ── Результаты по роли (analytics.tasks.by_role) ──────────────────────────────────────────── */

export type RoleStats = AnalyticsSummary['tasks']['by_role'][number];

/** Строка by_role роли; отсутствие строки — «нет попыток в окне» (null), а не нули. */
export function roleStats(summary: AnalyticsSummary | undefined, role: AgentRole): RoleStats | null {
  return summary?.tasks.by_role.find((r) => r.role === role) ?? null;
}

/* ── Провайдеры для роли ───────────────────────────────────────────────────────────────────── */

export const PROVIDER_CODES: readonly CliProvider[] = ['codex', 'claude'];

export interface EligibilityRow {
  code: CliProvider;
  displayName: string;
  enabledGlobally: boolean;
  enabledInWorkspace: boolean;
  /** Роль в `workspace_settings.allowed_roles`. */
  allowed: boolean;
  /** Итог сервера (`AgentProfile.eligible_providers`); null — профиль не загружен. */
  eligible: boolean | null;
}

/** Строки «провайдер × роль» по настройкам workspace; итог — из профиля (сервер — источник истины). */
export function eligibilityRows(role: AgentRole, providers: readonly Provider[], profile: AgentProfile | undefined): EligibilityRow[] {
  return providers.map((p) => ({
    code: p.code,
    displayName: p.display_name,
    enabledGlobally: p.enabled_globally,
    enabledInWorkspace: p.workspace_settings.enabled,
    allowed: p.workspace_settings.allowed_roles.includes(role),
    eligible: profile ? profile.eligible_providers.includes(p.code) : null,
  }));
}

/** Профили по роли (порядок ответа не важен; лишние/неизвестные роли игнорируются). */
export function profilesByRole(items: readonly AgentProfile[] | undefined): Map<AgentRole, AgentProfile> {
  const out = new Map<AgentRole, AgentProfile>();
  for (const p of items ?? []) if (isAgentRole(p.role)) out.set(p.role, p);
  return out;
}

/** Ссылки задачи workspace: Task Detail (нужен проект) и Mission Control с выбранной задачей. */
export function taskLinks(workspaceId: string, task: Pick<WorkspaceTask, 'id' | 'run_id' | 'project_id'>): { detail: string; mission: string } {
  return {
    detail: `/w/${workspaceId}/projects/${task.project_id}/runs/${task.run_id}/tasks/${task.id}`,
    mission: `/w/${workspaceId}/mission-control?run=${encodeURIComponent(task.run_id)}&task=${encodeURIComponent(task.id)}`,
  };
}
