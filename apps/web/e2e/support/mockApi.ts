/**
 * In-browser мок REST API v1 для E2E (page.route). Каждый запрос UI проверяется
 * на соответствие контракту (тело, обязательные заголовки, CSRF), каждый ответ
 * мока — по схеме ответа операции. Нарушения копятся в `violations`.
 */
import type { Page, Route } from '@playwright/test';
import type { components } from '../../src/api/schema';
import {
  requiredHeaders,
  requiresCsrf,
  responseMediaType,
  validateHeader,
  validateRequest,
  validateResponse,
  validateNotificationEvent,
  validateSseEvent,
} from './contract';

type S = components['schemas'];

export const CSRF = 'csrf-e2e-token-0123456789abcdef';
export const PASSWORD = 'correct horse battery staple';

let seq = 0;
function uuid(): string {
  seq += 1;
  return `01900000-0000-7000-8000-${String(seq).padStart(12, '0')}`;
}

export interface MockUser {
  email: string;
  display_name: string;
}

export interface Reply {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** Сырой текст ответа (text/event-stream) вместо JSON. */
  raw?: string;
}

/** Конверт SSE (schemas/events/1.0/sse-event.schema.json). */
export interface MockRunEvent {
  event_id: string;
  run_id: string;
  seq: number;
  type: string;
  timestamp: string;
  schema_version: '1.0';
  payload: Record<string, unknown>;
}

/**
 * Сценарий одного SSE-подключения (по порядку подключений). По умолчанию мок ведёт себя
 * как сервер: события с seq > Last-Event-ID (или after_seq) и закрытие потока.
 *  - fromSeq — начать с этого seq (≤ Last-Event-ID имитирует повторную доставку);
 *  - toSeq   — отдать события только до этого seq;
 *  - cut     — после них отдать половину следующего кадра и оборвать соединение;
 *  - status  — ответить ошибкой (например 410 replay_window_exceeded).
 */
export interface SseConnectionScript {
  fromSeq?: number;
  toSeq?: number;
  cut?: boolean;
  status?: 410;
}

/** Кадр `notification.created` потока workspace (SseNotificationCreated: seq = null). */
export interface MockNotificationFrame {
  event_id: string;
  run_id: string | null;
  seq: null;
  type: 'notification.created';
  timestamp: string;
  schema_version: '1.0';
  payload: S['Notification'];
}

export function sseFrame(e: MockRunEvent): string {
  return `id: ${e.seq}\nevent: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`;
}

export function problem(status: number, code: S['Problem']['code'], extra: Partial<S['Problem']> = {}): Reply {
  return {
    status,
    body: {
      type: `urn:aicd:problem:${code}`,
      title: code,
      status,
      code,
      request_id: `0af7651916cd43dd8448eb211c80319c`,
      ...extra,
    } satisfies S['Problem'],
  };
}

const ROUTES: { method: string; path: string; operationId: string }[] = [
  { method: 'POST', path: '/api/v1/auth/login', operationId: 'login' },
  { method: 'POST', path: '/api/v1/auth/logout', operationId: 'logout' },
  { method: 'GET', path: '/api/v1/auth/session', operationId: 'getSession' },
  { method: 'GET', path: '/api/v1/workspaces', operationId: 'listWorkspaces' },
  { method: 'POST', path: '/api/v1/workspaces', operationId: 'createWorkspace' },
  { method: 'GET', path: '/api/v1/projects', operationId: 'listProjects' },
  { method: 'POST', path: '/api/v1/projects', operationId: 'createProject' },
  { method: 'GET', path: '/api/v1/projects/{id}', operationId: 'getProject' },
  { method: 'GET', path: '/api/v1/runs', operationId: 'listRuns' },
  { method: 'POST', path: '/api/v1/runs', operationId: 'createRun' },
  { method: 'GET', path: '/api/v1/runs/{id}', operationId: 'getRun' },
  { method: 'GET', path: '/api/v1/runs/{id}/tasks', operationId: 'getRunTasks' },
  { method: 'GET', path: '/api/v1/runs/{id}/events', operationId: 'streamRunEvents' },
  { method: 'GET', path: '/api/v1/workspaces/{id}/events', operationId: 'streamWorkspaceEvents' },
  { method: 'GET', path: '/api/v1/workspaces/{id}', operationId: 'getWorkspace' },
  { method: 'POST', path: '/api/v1/runs/{id}/pause', operationId: 'pauseRun' },
  { method: 'POST', path: '/api/v1/runs/{id}/resume', operationId: 'resumeRun' },
  { method: 'POST', path: '/api/v1/runs/{id}/cancel', operationId: 'cancelRun' },
  // B8: персональные уведомления (оболочка запрашивает их на каждом экране workspace).
  { method: 'GET', path: '/api/v1/notifications', operationId: 'listNotifications' },
  { method: 'GET', path: '/api/v1/notifications/unread-count', operationId: 'getNotificationUnreadCount' },
  { method: 'POST', path: '/api/v1/notifications/mark-read', operationId: 'markNotificationsRead' },
];

/**
 * Операции, которые вызывает оболочка на каждом экране (F2: чипы CLI, счётчик approvals, поиск) и
 * стартовый экран Dashboard (F3). Ответ по умолчанию — пустой; spec переопределяет его через `api.route(...)` (приоритет у spec).
 */
const SHELL_DEFAULTS: { method: string; path: string; operationId: string; reply: (id: string | undefined) => Reply }[] = [
  { method: 'GET', path: '/api/v1/providers', operationId: 'listProviders', reply: () => ({ status: 200, body: { items: [] } }) },
  { method: 'GET', path: '/api/v1/approvals', operationId: 'listApprovals', reply: () => ({ status: 200, body: { items: [], next_cursor: null } }) },
  // F3 Bento Dashboard — стартовый экран после входа в каждом spec: пустые, но валидные ответы
  // (как у workspace без данных); dashboard.spec и другие spec переопределяют их через api.route.
  { method: 'GET', path: '/api/v1/tasks', operationId: 'listTasks', reply: () => ({ status: 200, body: { items: [], next_cursor: null } }) },
  { method: 'GET', path: '/api/v1/findings', operationId: 'listFindings', reply: () => ({ status: 200, body: { items: [], next_cursor: null } }) },
  { method: 'GET', path: '/api/v1/activity', operationId: 'listActivity', reply: () => ({ status: 200, body: { items: [], next_cursor: null } }) },
  { method: 'GET', path: '/api/v1/analytics/summary', operationId: 'getAnalyticsSummary', reply: () => ({ status: 200, body: emptyAnalyticsSummary() }) },
  {
    method: 'GET',
    path: '/api/v1/system/health',
    operationId: 'getSystemHealth',
    reply: () => ({
      status: 200,
      body: {
        status: 'unknown',
        checked_at: '2026-11-08T09:00:00Z',
        cache_ttl_seconds: 10,
        components: [],
        worker_hosts: null,
        worker_kinds: null,
        queues: null,
        backups: null,
      } satisfies S['SystemHealth'],
    }),
  },
  // F4 AI Mission Control — центральный экран run, на него ведут поиск, палитра, уведомления и старые
  // пути run/графа/чата: пустые валидные ответы (отчёт качества без gates, без evidence, пустая лента).
  // missionControl.spec и другие spec переопределяют их через api.route.
  {
    method: 'GET',
    path: '/api/v1/runs/{id}/quality',
    operationId: 'getRunQuality',
    reply: (id) => ({
      status: 200,
      body: { run_id: id ?? '', quality_profile: 'strict', verdict: 'PENDING', gate_executions: [], findings_summary: {}, findings: [], findings_total: 0 } satisfies S['QualityReport'],
    }),
  },
  { method: 'GET', path: '/api/v1/runs/{id}/evidence', operationId: 'listRunEvidence', reply: () => ({ status: 200, body: { items: [], next_cursor: null } }) },
  { method: 'GET', path: '/api/v1/runs/{id}/chat', operationId: 'getRunChat', reply: () => ({ status: 200, body: { items: [], threads: [], next_cursor: null } }) },
  // F5: экран «Агенты» (профили B7) и Настройки › CLI-провайдеры (история исполнений B1) — пустые валидные
  // ответы; agents.spec / providers.spec и другие spec переопределяют их через api.route.
  { method: 'GET', path: '/api/v1/agent-profiles', operationId: 'listAgentProfiles', reply: () => ({ status: 200, body: { items: [] } }) },
  // F8: Code Intelligence (раздел меню; на него ведут сочетание `g x` и палитра) — проект без репозиториев.
  // code-intelligence.spec и другие spec переопределяют ответ через api.route.
  {
    method: 'GET',
    path: '/api/v1/projects/{id}/repositories',
    operationId: 'listRepositories',
    reply: () => ({ status: 200, body: { items: [], next_cursor: null } }),
  },
  {
    method: 'GET',
    path: '/api/v1/providers/{id}/executions',
    operationId: 'listProviderExecutions',
    reply: () => ({ status: 200, body: { items: [], next_cursor: null } }),
  },
];

/** Сводка analytics workspace без данных за окно (нули и null-доли — как у сервера). */
export function emptyAnalyticsSummary(): S['AnalyticsSummary'] {
  const none = { p50: null, p90: null, avg: null };
  return {
    window: { from: '2026-10-09T09:00:00Z', to: '2026-11-08T09:00:00Z' },
    project_id: null,
    runs: { total: 0, finished: 0, completed: 0, success_rate: null, by_status: {}, duration_seconds: none, by_mode: [] },
    daily: [],
    tasks: { attempts: 0, succeeded: 0, failed: 0, retries: 0, verified_completion_rate: null, tasks_finished: 0, latency_seconds: none, by_role: [] },
    gates: [],
    providers: [],
    review_loops: { review_requests: 0, fix_requests: 0, review_chains: 0, chains_with_fixes: 0, avg_fix_rounds: null, max_fix_rounds: 0, loops_exhausted: 0 },
    agent_messages: { total: 0, by_type: {} },
    approvals: { requested: 0, pending: 0, by_status: {}, wait_seconds: none },
    context: {
      packs: 0,
      avg_size_bytes: null,
      max_size_bytes: null,
      fragments: 0,
      stale_fragment_ratio: null,
      usefulness: { rate: null, packs_evaluated: 0, fragments_referenced: 0, fragments_total: 0, truncated: false },
    },
  };
}

/** Запрос к дополнительной операции мока (MockApi.route). */
export interface MockRequest {
  url: URL;
  headers: Record<string, string>;
  body: unknown;
  /** Значение `{id}` из шаблона пути. */
  id: string | undefined;
  workspaceId: string | undefined;
}

function matchRoute(
  method: string,
  pathname: string,
  extra: readonly { method: string; path: string; operationId: string }[] = [],
): { operationId: string; id: string | undefined } | null {
  for (const r of [...ROUTES, ...extra, ...SHELL_DEFAULTS]) {
    if (r.method !== method) continue;
    const re = new RegExp(`^${r.path.replace('{id}', '([^/]+)')}$`);
    const m = re.exec(pathname);
    if (m) return { operationId: r.operationId, id: m[1] ? decodeURIComponent(m[1]) : undefined };
  }
  return null;
}

export class MockApi {
  readonly violations: string[] = [];
  readonly requests: { operationId: string; headers: Record<string, string>; body: unknown }[] = [];
  user: MockUser = { email: 'anna@example.com', display_name: 'Анна Петрова' };
  loggedIn = false;
  workspaces: S['Workspace'][] = [];
  projects = new Map<string, S['Project'][]>();
  /**
   * Сколько следующих GET /projects ответить ошибкой 500 internal_error (объявлена в контракте
   * для всех операций; клиент её не повторяет автоматически — повтор только кнопкой).
   */
  failProjectsTimes = 0;
  /** Следующий запрос (кроме auth) ответит 401 session_expired. */
  expireSessionOnNextRequest = false;
  pageSize = 3;
  /** Slug, занятые workspace других пользователей (→ 409 conflict). */
  readonly takenSlugs = new Set<string>();
  private readonly idempotency = new Map<string, { body: string; reply: Reply }>();
  runs: S['Run'][] = [];
  tasks = new Map<string, S['TaskSummary'][]>();
  /** Durable-лог событий run (run_event_log). */
  events = new Map<string, MockRunEvent[]>();
  /** Сценарии SSE-подключений по порядку; после исчерпания — поведение сервера. */
  sseScripts: SseConnectionScript[] = [];
  /** retry: (мс) в первом кадре потока; сервер шлёт 3000, тестам нужно быстрее. */
  sseRetryMs = 100;
  /** Сколько SSE-подключений было и с какими Last-Event-ID / after_seq. */
  readonly sseConnections: { lastEventId: string | undefined; afterSeq: string | null }[] = [];
  /**
   * «Сеть недоступна» (вместе с context.setOffline): запросы обрываются, удерживаемый
   * SSE-ответ не досылается — как при реальном обрыве (без «призрачных» подключений в журнале).
   */
  offline = false;
  /** Журнал потока уведомлений workspace: курсор (`id:`) → событие (OQ-45 (c)) или кадр `notification.created` (B8). */
  readonly workspaceEvents: { workspaceId: string; cursor: number; event: MockRunEvent | MockNotificationFrame }[] = [];
  /** Персональные уведомления текущего пользователя (B8, `notifications`), новые в конце. */
  readonly notifications: { workspaceId: string; item: S['Notification'] }[] = [];
  /** Подключения к потоку workspace с их Last-Event-ID. */
  readonly workspaceStreamConnections: { workspaceId: string; lastEventId: string | undefined }[] = [];
  private workspaceCursor = 1_762_590_000_000_000;
  private readonly extraRoutes: { method: string; path: string; operationId: string; handler: (req: MockRequest) => Reply }[] = [];

  /**
   * Дополнительная операция контракта для E2E экрана (вызывается из своего spec-файла,
   * этот файл менять не нужно). Путь — шаблон с `{id}`, ответ валидируется по контракту operationId.
   * Пример: `api.route('GET', '/api/v1/approvals', 'listApprovals', () => ({ status: 200, body: {...} }))`.
   */
  route(method: string, path: string, operationId: string, handler: (req: MockRequest) => Reply): void {
    this.extraRoutes.push({ method, path, operationId, handler });
  }

  addRun(project: S['Project'], init: Partial<S['Run']> = {}): S['Run'] {
    const run: S['Run'] = {
      id: uuid(),
      project_id: project.id,
      workspace_id: project.workspace_id,
      mode: 'DEVELOP',
      objective: 'Добавить экспорт отчётов в CSV',
      status: 'RUNNING',
      blocked_reason: null,
      requested_by: { id: '01900000-0000-7000-8000-0000000000aa', display_name: this.user.display_name },
      quality_profile: 'strict',
      version: 1,
      created_at: '2026-11-08T08:00:00Z',
      started_at: '2026-11-08T08:00:05Z',
      finished_at: null,
      plan_version: 1,
      pending_approvals: 0,
      task_counts: {},
      ...init,
    };
    this.runs.unshift(run);
    this.tasks.set(run.id, []);
    this.events.set(run.id, []);
    return run;
  }

  addTask(run: S['Run'], title: string, init: Partial<S['TaskSummary']> = {}): S['TaskSummary'] {
    const task: S['TaskSummary'] = {
      id: uuid(),
      run_id: run.id,
      agent_role: 'developer',
      title,
      status: 'PENDING',
      blocked_reason: null,
      complexity: 'L2',
      risk_level: 'low',
      current_provider: null,
      attempt_count: 0,
      max_attempts: 3,
      depends_on: [],
      version: 0,
      updated_at: '2026-11-08T08:00:10Z',
      ...init,
    };
    this.tasks.get(run.id)?.push(task);
    return task;
  }

  /** Добавляет событие в лог run; seq и event_id назначаются по порядку. */
  addEvent(run: S['Run'], type: string, payload: Record<string, unknown>): MockRunEvent {
    const log = this.events.get(run.id) ?? [];
    const seq = log.length + 1;
    const e: MockRunEvent = {
      event_id: `01900000-0000-7000-a000-${String(seq).padStart(12, '0')}`,
      run_id: run.id,
      seq,
      type,
      timestamp: `2026-11-08T09:00:${String(seq % 60).padStart(2, '0')}Z`,
      schema_version: '1.0',
      payload,
    };
    log.push(e);
    this.events.set(run.id, log);
    return e;
  }

  /** Эффект доставленного события на «состояние БД» мока (как будто REST видит то же). */
  private applyEvent(e: MockRunEvent): void {
    const run = this.runs.find((r) => r.id === e.run_id);
    if (!run) return;
    const p = e.payload;
    if (e.type === 'run.state_changed') {
      run.status = p.to_status as S['RunStatus'];
      run.blocked_reason = (p.blocked_reason as S['TaskBlockedReason'] | null | undefined) ?? null;
      run.version += 1;
    } else if (e.type === 'run.completed') {
      run.status = p.final_status as S['RunStatus'];
      run.finished_at = e.timestamp;
    } else if (e.type === 'approval.requested') {
      run.pending_approvals = (run.pending_approvals ?? 0) + 1;
    } else if (e.type === 'task.state_changed') {
      const task = this.tasks.get(run.id)?.find((x) => x.id === p.task_id);
      if (task) {
        task.status = p.to_status as S['TaskStatus'];
        if (typeof p.attempt_no === 'number') task.attempt_count = p.attempt_no;
        // Исполнитель попытки (payload.provider) — REST после события видит того же провайдера.
        if (typeof p.provider === 'string') task.current_provider = p.provider as S['CliProvider'];
        task.version += 1;
      }
    }
  }

  private runView(run: S['Run']): S['Run'] {
    const counts: Record<string, number> = {};
    for (const task of this.tasks.get(run.id) ?? []) counts[task.status] = (counts[task.status] ?? 0) + 1;
    return { ...run, task_counts: counts };
  }

  private streamEvents(run: S['Run'], headers: Record<string, string>, url: URL): Reply {
    const lastEventId = headers['last-event-id'];
    const afterSeq = url.searchParams.get('after_seq');
    this.sseConnections.push({ lastEventId, afterSeq });
    const script = this.sseScripts.shift() ?? {};
    const log = this.events.get(run.id) ?? [];
    if (script.status === 410) {
      return problem(410, 'replay_window_exceeded', {
        min_available_seq: log[0]?.seq ?? 1,
        last_seq: log.at(-1)?.seq ?? 0,
      } as Partial<S['Problem']>);
    }
    const start = script.fromSeq ?? Number(lastEventId ?? afterSeq ?? 0) + 1;
    const end = script.toSeq ?? Number.MAX_SAFE_INTEGER;
    const batch = log.filter((e) => e.seq >= start && e.seq <= end);
    let raw = `retry: ${this.sseRetryMs}\n\n`;
    for (const e of batch) {
      const error = validateSseEvent(e);
      if (error) this.violations.push(`mock ${error}`);
      this.applyEvent(e);
      raw += sseFrame(e);
    }
    if (script.cut) {
      const next = log.find((e) => e.seq > end);
      if (next) raw += sseFrame(next).slice(0, 40); // кадр оборван посередине: id без пустой строки
    }
    return { status: 200, raw, headers: { 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' } };
  }

  /** Событие потока уведомлений workspace (approval.*, run.state_changed, run.completed) с новым курсором. */
  addWorkspaceEvent(workspaceId: string, type: string, payload: Record<string, unknown>, runId?: string): MockRunEvent {
    this.workspaceCursor += 1000;
    const event: MockRunEvent = {
      event_id: uuid(),
      run_id: runId ?? this.runs.find((r) => r.workspace_id === workspaceId)?.id ?? uuid(),
      seq: this.workspaceEvents.length + 1,
      type,
      // Время записи растёт с курсором (как на сервере): «прочитано до» в уведомлениях сравнивает время.
      timestamp: new Date(Date.parse('2026-11-08T09:00:00Z') + this.workspaceEvents.length * 1000).toISOString().replace('.000Z', 'Z'),
      schema_version: '1.0',
      payload,
    };
    const error = validateSseEvent(event);
    if (error) this.violations.push(`mock ${error}`);
    this.workspaceEvents.push({ workspaceId, cursor: this.workspaceCursor, event });
    return event;
  }

  /**
   * Новое персональное уведомление (B8): строка `notifications` и, если `live`, кадр `notification.created`
   * в потоке workspace (как сервер: курсор = время создания). `created_at` растёт с порядком добавления.
   */
  addNotification(workspaceId: string, init: Partial<S['Notification']> & Pick<S['Notification'], 'kind' | 'title'>, live = true): S['Notification'] {
    const created = new Date(Date.parse('2026-11-08T09:00:00Z') + this.notifications.length * 60_000).toISOString().replace('.000Z', 'Z');
    const item: S['Notification'] = {
      id: uuid(),
      severity: 'info',
      body: '',
      project_id: null,
      run_id: null,
      task_id: null,
      resource_type: 'run',
      resource_id: uuid(),
      data: {},
      created_at: created,
      read_at: null,
      read: false,
      ...init,
    };
    this.notifications.push({ workspaceId, item });
    if (live) {
      this.workspaceCursor += 1000;
      const frame: MockNotificationFrame = {
        event_id: item.id,
        run_id: item.run_id,
        seq: null,
        type: 'notification.created',
        timestamp: item.created_at,
        schema_version: '1.0',
        payload: { ...item },
      };
      const error = validateNotificationEvent(frame);
      if (error) this.violations.push(`mock ${error}`);
      this.workspaceEvents.push({ workspaceId, cursor: this.workspaceCursor, event: frame });
    }
    return item;
  }

  private notificationsOf(workspaceId: string): S['Notification'][] {
    // Новые сверху: (created_at, id) desc.
    return this.notifications
      .filter((n) => n.workspaceId === workspaceId)
      .map((n) => n.item)
      .sort((a, b) => (a.created_at === b.created_at ? b.id.localeCompare(a.id) : b.created_at.localeCompare(a.created_at)));
  }

  private unreadNotifications(workspaceId: string): S['NotificationUnreadCount'] {
    const unread = this.notificationsOf(workspaceId).filter((n) => !n.read);
    return { unread_count: unread.length, latest_unread_at: unread[0]?.created_at ?? null };
  }

  addWorkspace(name: string, slug: string, role: S['WorkspaceRole'] = 'owner'): S['Workspace'] {
    const ws: S['Workspace'] = { id: uuid(), name, slug, role, created_at: '2026-11-01T09:00:00Z' };
    this.workspaces.push(ws);
    this.projects.set(ws.id, []);
    return ws;
  }

  addProject(workspaceId: string, name: string, description: string | null = null): S['Project'] {
    const p: S['Project'] = {
      id: uuid(),
      workspace_id: workspaceId,
      name,
      description,
      status: 'ACTIVE',
      settings: {
        documentation_mode: 'SMART',
        quality_profile: 'strict',
        routing_policy_id: null,
        security_profile: null,
        settings_version: 0,
      },
      version: 0,
      created_at: '2026-11-02T09:00:00Z',
      updated_at: '2026-11-02T09:00:00Z',
    };
    // Сортировка по умолчанию: (created_at, id) desc — новые первыми.
    this.projects.get(workspaceId)?.unshift(p);
    return p;
  }

  session(): S['SessionInfo'] {
    return {
      user: { id: '01900000-0000-7000-8000-0000000000aa', email: this.user.email, display_name: this.user.display_name },
      csrf_token: CSRF,
      expires_at: '2026-11-08T09:00:00Z',
      idle_expires_at: '2026-11-07T21:30:00Z',
      workspaces: this.workspaces.map((w) => ({ workspace_id: w.id, slug: w.slug, name: w.name, role: w.role })),
    };
  }

  async install(page: Page): Promise<void> {
    await page.route('**/api/v1/**', (route) => this.handle(route));
  }

  private page<T>(items: T[], cursor: string | null): { items: T[]; next_cursor: string | null } {
    const start = cursor ? Number(Buffer.from(cursor, 'base64url').toString('utf8')) : 0;
    const end = start + this.pageSize;
    return {
      items: items.slice(start, end),
      next_cursor: end < items.length ? Buffer.from(String(end)).toString('base64url') : null,
    };
  }

  private async handle(route: Route): Promise<void> {
    const req = route.request();
    const url = new URL(req.url());
    const method = req.method();
    const match = matchRoute(method, url.pathname, this.extraRoutes);
    if (!match) {
      this.violations.push(`unexpected request ${method} ${url.pathname}`);
      await route.fulfill({ status: 404, contentType: 'application/problem+json', body: JSON.stringify(problem(404, 'not_found').body) });
      return;
    }
    const { operationId } = match;
    if (this.offline) {
      await route.abort('internetdisconnected').catch(() => undefined);
      return;
    }
    const headers = await req.allHeaders();
    const raw = req.postData();
    const body: unknown = raw ? JSON.parse(raw) : undefined;
    this.requests.push({ operationId, headers, body });

    if (operationId === 'streamRunEvents' && this.sseScripts.length === 0 && match.id) {
      // Как live-сервер: при отсутствии новых событий держим соединение (до 1,5 с), а не закрываем сразу.
      const after = Number(headers['last-event-id'] ?? url.searchParams.get('after_seq') ?? 0);
      const runId = match.id;
      const hasNew = () => (this.events.get(runId) ?? []).some((e) => e.seq > after);
      for (let waited = 0; waited < 1500 && !hasNew() && !this.offline; waited += 50) await new Promise((r) => setTimeout(r, 50));
      if (this.offline) {
        await route.abort('internetdisconnected').catch(() => undefined);
        return;
      }
    }
    if (operationId === 'streamWorkspaceEvents' && match.id) {
      // Как сервер: держим соединение, пока нет событий после курсора (до 1,5 с), затем закрываем.
      const after = Number(headers['last-event-id'] ?? 0);
      const wsId = match.id;
      const hasNew = () => this.workspaceEvents.some((e) => e.workspaceId === wsId && e.cursor > after);
      for (let waited = 0; waited < 1500 && !hasNew() && !this.offline; waited += 50) await new Promise((r) => setTimeout(r, 50));
      if (this.offline) {
        await route.abort('internetdisconnected').catch(() => undefined);
        return;
      }
    }
    const reply = this.dispatch(operationId, url, headers, body, match.id);

    // Явный Content-Type ответа мока (content negotiation) проверяется по своей схеме представления.
    const explicitType = reply.headers?.['content-type']?.split(';')[0]?.trim();
    const responseError = validateResponse(operationId, reply.status, reply.raw ?? reply.body, explicitType);
    if (responseError) this.violations.push(`mock ${responseError}`);
    const contentType = explicitType ? (reply.headers?.['content-type'] ?? explicitType) : (responseMediaType(operationId, reply.status) ?? 'application/json');
    if (reply.raw !== undefined) {
      // Страница могла закрыться, пока поток «висел» в ожидании событий.
      await route
        .fulfill({ status: reply.status, headers: { 'content-type': contentType, ...reply.headers }, body: reply.raw })
        .catch(() => undefined);
      return;
    }
    await route.fulfill({
      status: reply.status,
      headers: { ...(reply.body === undefined ? {} : { 'content-type': contentType }), ...reply.headers },
      ...(reply.body === undefined ? {} : { body: JSON.stringify(reply.body) }),
    });
  }

  private dispatch(operationId: string, url: URL, headers: Record<string, string>, body: unknown, id?: string): Reply {
    // Порядок проверок как на сервере (API_DESIGN §6): 401 → CSRF → tenant → idempotency → схема.
    if (operationId !== 'login') {
      if (!this.loggedIn) return problem(401, 'unauthenticated');
      if (this.expireSessionOnNextRequest && operationId !== 'getSession') {
        this.expireSessionOnNextRequest = false;
        this.loggedIn = false;
        return problem(401, 'session_expired');
      }
    }
    if (requiresCsrf(operationId) && headers['x-csrf-token'] !== CSRF) {
      this.violations.push(`${operationId}: missing/invalid X-CSRF-Token`);
      return problem(403, 'csrf_failed');
    }
    for (const h of requiredHeaders(operationId)) {
      const value = headers[h.name.toLowerCase()];
      if (value === undefined || !validateHeader(h.schema, value)) {
        this.violations.push(`${operationId}: header ${h.name} missing/invalid (${String(value)})`);
        return h.name === 'Idempotency-Key' ? problem(400, 'idempotency_key_missing') : problem(404, 'not_found');
      }
    }
    const requestError = validateRequest(operationId, body);
    if (requestError) {
      this.violations.push(`UI ${requestError}`);
      return problem(422, 'validation_error', { errors: [{ pointer: '', code: 'schema', message: requestError }] });
    }

    const key = headers['idempotency-key'];
    if (key) {
      const scoped = `${operationId}:${headers['x-workspace-id'] ?? ''}:${key}`;
      const stored = this.idempotency.get(scoped);
      if (stored) {
        if (stored.body !== JSON.stringify(body)) return problem(422, 'idempotency_key_reuse');
        return { ...stored.reply, headers: { ...stored.reply.headers, 'Idempotency-Replayed': 'true' } };
      }
      const reply = this.execute(operationId, url, headers, body, id);
      // Как сервер: отказ step-up (403 step_up_required) не сохраняется — повтор после POST /auth/reauthenticate
      // идёт с тем же Idempotency-Key (src/auth/StepUp.tsx) и выполняется заново.
      const stepUp = (reply.body as { code?: string } | undefined)?.code === 'step_up_required';
      if (reply.status < 500 && !stepUp) this.idempotency.set(scoped, { body: JSON.stringify(body), reply });
      return reply;
    }
    return this.execute(operationId, url, headers, body, id);
  }

  private execute(operationId: string, url: URL, headers: Record<string, string>, body: unknown, id?: string): Reply {
    const workspaceId = headers['x-workspace-id'] ?? '';
    const ws = this.workspaces.find((w) => w.id === workspaceId);
    switch (operationId) {
      case 'login': {
        const b = body as S['LoginRequest'];
        if (b.email !== this.user.email || b.password !== PASSWORD) return problem(401, 'unauthenticated');
        this.loggedIn = true;
        return { status: 200, body: this.session() };
      }
      case 'logout':
        this.loggedIn = false;
        return { status: 204 };
      case 'getSession':
        return { status: 200, body: this.session() };
      case 'listWorkspaces':
        return { status: 200, body: this.page(this.workspaces, url.searchParams.get('cursor')) };
      case 'createWorkspace': {
        const b = body as S['WorkspaceCreateRequest'];
        if (this.takenSlugs.has(b.slug) || this.workspaces.some((w) => w.slug === b.slug)) {
          return problem(409, 'conflict', { errors: [{ pointer: '/slug', code: 'conflict', message: 'Slug уже занят' }] });
        }
        const created = this.addWorkspace(b.name, b.slug, 'owner');
        return { status: 201, body: created, headers: { Location: `/api/v1/workspaces/${created.id}`, ETag: '"v0"' } };
      }
      case 'listProjects': {
        if (!ws) return problem(404, 'not_found');
        if (this.failProjectsTimes > 0) {
          this.failProjectsTimes -= 1;
          return problem(500, 'internal_error');
        }
        return { status: 200, body: this.page(this.projects.get(ws.id) ?? [], url.searchParams.get('cursor')) };
      }
      case 'createProject': {
        if (!ws) return problem(404, 'not_found');
        if (ws.role !== 'owner' && ws.role !== 'admin') return problem(403, 'forbidden');
        const b = body as S['ProjectCreateRequest'];
        const p = this.addProject(ws.id, b.name, b.description ?? null);
        p.settings.documentation_mode = b.documentation_mode ?? 'SMART';
        p.settings.quality_profile = b.quality_profile ?? 'strict';
        return { status: 201, body: p, headers: { Location: `/api/v1/projects/${p.id}`, ETag: '"v0"' } };
      }
      case 'getProject': {
        const p = [...this.projects.values()].flat().find((x) => x.id === id);
        return p ? { status: 200, body: p } : problem(404, 'not_found');
      }
      case 'listRuns': {
        if (!ws) return problem(404, 'not_found');
        const projectId = url.searchParams.get('project_id');
        const statuses = url.searchParams.getAll('status');
        const mode = url.searchParams.get('mode');
        const items = this.runs
          .filter(
            (r) =>
              r.workspace_id === ws.id &&
              (!projectId || r.project_id === projectId) &&
              (statuses.length === 0 || statuses.includes(r.status)) &&
              (!mode || r.mode === mode),
          )
          .map((r) => {
            const summary: S['RunSummary'] = {
              id: r.id,
              project_id: r.project_id,
              workspace_id: r.workspace_id,
              mode: r.mode,
              objective: r.objective,
              status: r.status,
              blocked_reason: r.blocked_reason ?? null,
              requested_by: r.requested_by,
              quality_profile: r.quality_profile,
              version: r.version,
              created_at: r.created_at,
              started_at: r.started_at ?? null,
              finished_at: r.finished_at ?? null,
            };
            return summary;
          });
        return { status: 200, body: this.page(items, url.searchParams.get('cursor')) };
      }
      case 'createRun': {
        const b = body as S['RunCreateRequest'];
        const p = [...this.projects.values()].flat().find((x) => x.id === b.project_id);
        if (!p) return problem(404, 'not_found');
        const run = this.addRun(p, { mode: b.mode, objective: b.objective, status: 'CREATED', version: 0, started_at: null });
        return {
          status: 201,
          body: { run_id: run.id, status: run.status, event_stream: `/api/v1/runs/${run.id}/events` },
          headers: { Location: `/api/v1/runs/${run.id}`, ETag: '"v0"' },
        };
      }
      case 'getRun': {
        const run = this.runs.find((r) => r.id === id);
        return run ? { status: 200, body: this.runView(run), headers: { ETag: `"v${run.version}"` } } : problem(404, 'not_found');
      }
      case 'getRunTasks': {
        const run = this.runs.find((r) => r.id === id);
        if (!run) return problem(404, 'not_found');
        const tasks = this.tasks.get(run.id) ?? [];
        const edges = tasks.flatMap((x) =>
          x.depends_on.map((d) => ({ task_id: x.id, depends_on_task_id: d, dependency_type: 'blocks' })),
        );
        return {
          status: 200,
          body: { run_id: run.id, plan_version: run.plan_version ?? 1, tasks, edges, next_cursor: null },
        };
      }
      case 'streamRunEvents': {
        const run = this.runs.find((r) => r.id === id);
        return run ? this.streamEvents(run, headers, url) : problem(404, 'not_found');
      }
      case 'streamWorkspaceEvents': {
        const target = this.workspaces.find((w) => w.id === id);
        if (!target) return problem(404, 'not_found');
        const lastEventId = headers['last-event-id'];
        this.workspaceStreamConnections.push({ workspaceId: target.id, lastEventId });
        const after = Number(lastEventId ?? 0);
        const types = url.searchParams.getAll('types');
        let raw = `retry: ${this.sseRetryMs}\n\n`;
        for (const e of this.workspaceEvents) {
          if (e.workspaceId !== target.id || e.cursor <= after || (types.length > 0 && !types.includes(e.event.type))) continue;
          raw += `id: ${e.cursor}\nevent: ${e.event.type}\ndata: ${JSON.stringify(e.event)}\n\n`;
        }
        return { status: 200, raw, headers: { 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' } };
      }
      case 'listNotifications': {
        if (!ws) return problem(404, 'not_found');
        const unreadOnly = url.searchParams.get('unread_only') === 'true';
        const items = this.notificationsOf(ws.id).filter((n) => !unreadOnly || !n.read);
        return { status: 200, body: { ...this.page(items, url.searchParams.get('cursor')), unread_count: this.unreadNotifications(ws.id).unread_count } };
      }
      case 'getNotificationUnreadCount':
        return ws ? { status: 200, body: this.unreadNotifications(ws.id) } : problem(404, 'not_found');
      case 'markNotificationsRead': {
        if (!ws) return problem(404, 'not_found');
        const b = body as S['NotificationMarkReadRequest'];
        const list = this.notificationsOf(ws.id);
        let targets: S['Notification'][];
        if (b.notification_ids) targets = list.filter((n) => b.notification_ids?.includes(n.id));
        else if (b.up_to) {
          const index = list.findIndex((n) => n.id === b.up_to);
          if (index < 0) return problem(404, 'not_found');
          targets = list.slice(index);
        } else targets = list;
        const fresh = targets.filter((n) => !n.read);
        for (const n of fresh) {
          n.read = true;
          n.read_at = '2026-11-08T10:00:00Z';
        }
        return { status: 200, body: { marked: fresh.length, unread_count: this.unreadNotifications(ws.id).unread_count } };
      }
      case 'getWorkspace': {
        const target = this.workspaces.find((w) => w.id === id);
        if (!target) return problem(404, 'not_found');
        const version = target.version ?? 0;
        return { status: 200, body: { ...target, settings: target.settings ?? {}, version }, headers: { ETag: `"v${version}"` } };
      }
      case 'pauseRun':
      case 'resumeRun':
      case 'cancelRun': {
        const run = this.runs.find((r) => r.id === id);
        if (!run) return problem(404, 'not_found');
        const ifMatch = headers['if-match'];
        if (ifMatch && ifMatch !== `"v${run.version}"`) return problem(412, 'precondition_failed');
        const command = operationId.replace('Run', '') as 'pause' | 'resume' | 'cancel';
        const target = command === 'pause' ? 'PAUSED' : command === 'cancel' ? 'CANCELLED' : 'RUNNING';
        if (['COMPLETED', 'FAILED', 'CANCELLED'].includes(run.status)) {
          return problem(409, 'invalid_state_transition', { current_status: run.status });
        }
        // Переход публикуется событием (как на сервере): REST увидит его после доставки по SSE.
        this.addEvent(run, 'run.state_changed', { from_status: run.status, to_status: target, run_version: run.version + 1 });
        return {
          status: 202,
          body: { run_id: run.id, command, status: run.status, version: run.version, accepted_at: '2026-11-08T09:30:00Z' },
        };
      }
      default: {
        const extra = this.extraRoutes.find((r) => r.operationId === operationId);
        if (extra) return extra.handler({ url, headers, body, id, workspaceId: headers['x-workspace-id'] });
        const fallback = SHELL_DEFAULTS.find((r) => r.operationId === operationId);
        return fallback ? fallback.reply(id) : problem(404, 'not_found');
      }
    }
  }
}
