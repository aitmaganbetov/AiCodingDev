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

interface Reply {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}

function problem(status: number, code: S['Problem']['code'], extra: Partial<S['Problem']> = {}): Reply {
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
];

export class MockApi {
  readonly violations: string[] = [];
  readonly requests: { operationId: string; headers: Record<string, string>; body: unknown }[] = [];
  user: MockUser = { email: 'anna@example.com', display_name: 'Анна Петрова' };
  loggedIn = false;
  workspaces: S['Workspace'][] = [];
  projects = new Map<string, S['Project'][]>();
  /**
   * Сколько следующих GET /projects ответить ошибкой. Используется 429: 500/503 в
   * контракте для listProjects не объявлены (см. README, «Замечания к контракту»).
   */
  failProjectsTimes = 0;
  /** Следующий запрос (кроме auth) ответит 401 session_expired. */
  expireSessionOnNextRequest = false;
  pageSize = 3;
  /** Slug, занятые workspace других пользователей (→ 409 conflict). */
  readonly takenSlugs = new Set<string>();
  private readonly idempotency = new Map<string, { body: string; reply: Reply }>();

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
    const match = ROUTES.find((r) => r.method === method && r.path === url.pathname);
    if (!match) {
      this.violations.push(`unexpected request ${method} ${url.pathname}`);
      await route.fulfill({ status: 404, contentType: 'application/problem+json', body: JSON.stringify(problem(404, 'not_found').body) });
      return;
    }
    const { operationId } = match;
    const headers = await req.allHeaders();
    const raw = req.postData();
    const body: unknown = raw ? JSON.parse(raw) : undefined;
    this.requests.push({ operationId, headers, body });

    const reply = this.dispatch(operationId, url, headers, body);

    const responseError = validateResponse(operationId, reply.status, reply.body);
    if (responseError) this.violations.push(`mock ${responseError}`);
    const contentType = responseMediaType(operationId, reply.status) ?? 'application/json';
    await route.fulfill({
      status: reply.status,
      headers: { ...(reply.body === undefined ? {} : { 'content-type': contentType }), ...reply.headers },
      ...(reply.body === undefined ? {} : { body: JSON.stringify(reply.body) }),
    });
  }

  private dispatch(operationId: string, url: URL, headers: Record<string, string>, body: unknown): Reply {
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
      const reply = this.execute(operationId, url, headers, body);
      if (reply.status < 500) this.idempotency.set(scoped, { body: JSON.stringify(body), reply });
      return reply;
    }
    return this.execute(operationId, url, headers, body);
  }

  private execute(operationId: string, url: URL, headers: Record<string, string>, body: unknown): Reply {
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
          return { ...problem(429, 'rate_limited', { retry_after_seconds: 1 }), headers: { 'Retry-After': '1' } };
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
      default:
        return problem(404, 'not_found');
    }
  }
}
