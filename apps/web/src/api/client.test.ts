import { describe, expect, it, vi } from 'vitest';
import { problem, json, mockFetch, WS_ID } from '../test/utils';
import {
  ApiError,
  IdempotentIntent,
  parseProblem,
  readCsrfCookie,
  setCsrfToken,
  setUnauthorizedHandler,
} from './client';
import { authApi } from './auth';
import { projectsApi } from './projects';
import { workspacesApi } from './workspaces';

const workspace = {
  id: WS_ID,
  name: 'Команда',
  slug: 'team',
  role: 'owner',
  created_at: '2026-11-07T10:00:00Z',
};

describe('api client: CSRF', () => {
  it('sends X-CSRF-Token from the session on unsafe methods only, same-origin credentials, /api/v1 base', async () => {
    const { fn, calls } = mockFetch({
      'GET /api/v1/workspaces': () => json(200, { items: [], next_cursor: null }),
      'POST /api/v1/workspaces': () => json(201, workspace),
    });
    setCsrfToken('tok-123');
    await workspacesApi.list();
    await workspacesApi.create({ name: 'Команда', slug: 'team' }, 'key-00000001');

    expect(calls[0]?.url.pathname).toBe('/api/v1/workspaces');
    expect(calls[0]?.headers.get('X-CSRF-Token')).toBeNull();
    expect(calls[1]?.headers.get('X-CSRF-Token')).toBe('tok-123');
    expect(calls[1]?.headers.get('Content-Type')).toBe('application/json');
    for (const call of fn.mock.calls) expect(call[1]?.credentials).toBe('same-origin');
  });

  it('falls back to the aicd_csrf cookie when no session token is known', async () => {
    const { calls } = mockFetch({ 'POST /api/v1/auth/logout': () => new Response(null, { status: 204 }) });
    document.cookie = 'other=1';
    document.cookie = 'aicd_csrf=cookie-tok%3D';
    await authApi.logout();
    expect(calls[0]?.headers.get('X-CSRF-Token')).toBe('cookie-tok=');
  });

  it('does not send X-CSRF-Token on login (no session yet)', async () => {
    const { calls } = mockFetch({ 'POST /api/v1/auth/login': () => problem(401, 'unauthenticated') });
    setCsrfToken('stale');
    await expect(authApi.login({ email: 'a@b.c', password: 'x' })).rejects.toBeInstanceOf(ApiError);
    expect(calls[0]?.headers.get('X-CSRF-Token')).toBeNull();
  });

  it('readCsrfCookie parses the cookie string', () => {
    expect(readCsrfCookie('a=1; aicd_csrf=xyz; b=2')).toBe('xyz');
    expect(readCsrfCookie('a=1')).toBeNull();
  });
});

describe('api client: Idempotency-Key and tenant header', () => {
  it('reuses the same Idempotency-Key when retrying after a network error', async () => {
    let n = 0;
    const { calls } = mockFetch({
      'POST /api/v1/projects': () => {
        n += 1;
        if (n === 1) throw new TypeError('Failed to fetch');
        return json(201, {});
      },
    });
    await projectsApi.create(WS_ID, { name: 'P' }, 'intent-key-1');
    expect(calls).toHaveLength(2);
    expect(calls[0]?.headers.get('Idempotency-Key')).toBe('intent-key-1');
    expect(calls[1]?.headers.get('Idempotency-Key')).toBe('intent-key-1');
    expect(calls[1]?.headers.get('X-Workspace-Id')).toBe(WS_ID);
  });

  it('retries 409 idempotency_key_in_flight with the same key', async () => {
    let n = 0;
    const { calls } = mockFetch({
      'POST /api/v1/workspaces': () => {
        n += 1;
        return n === 1 ? problem(409, 'idempotency_key_in_flight', { retry_after_seconds: 0 }) : json(201, workspace);
      },
    });
    await expect(workspacesApi.create({ name: 'Команда', slug: 'team' }, 'k-12345678')).resolves.toMatchObject({ id: WS_ID });
    expect(calls.map((c) => c.headers.get('Idempotency-Key'))).toEqual(['k-12345678', 'k-12345678']);
  });

  it('does not retry POST without an Idempotency-Key (logout)', async () => {
    const { calls } = mockFetch({
      'POST /api/v1/auth/logout': () => {
        throw new TypeError('offline');
      },
    });
    await expect(authApi.logout()).rejects.toMatchObject({ code: 'network_error', status: 0 });
    expect(calls).toHaveLength(1);
  });

  it('IdempotentIntent: one key per user intent, new key for changed body or after success', () => {
    const intent = new IdempotentIntent();
    const k1 = intent.keyFor({ name: 'a', slug: 'b' });
    expect(k1).toMatch(/^[A-Za-z0-9._:-]{8,255}$/);
    expect(intent.keyFor({ slug: 'b', name: 'a' })).toBe(k1);
    const k2 = intent.keyFor({ name: 'a2', slug: 'b' });
    expect(k2).not.toBe(k1);
    intent.complete();
    expect(intent.keyFor({ name: 'a2', slug: 'b' })).not.toBe(k2);
  });

  it('sends X-Workspace-Id and cursor/limit for listProjects', async () => {
    const { calls } = mockFetch({ 'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }) });
    await projectsApi.list(WS_ID, { cursor: 'abc', limit: 20 });
    expect(calls[0]?.headers.get('X-Workspace-Id')).toBe(WS_ID);
    expect(calls[0]?.url.searchParams.get('cursor')).toBe('abc');
    expect(calls[0]?.url.searchParams.get('limit')).toBe('20');
  });
});

describe('api client: problem+json', () => {
  it('parses RFC 7807 problem into a typed ApiError', async () => {
    mockFetch({
      'POST /api/v1/workspaces': () =>
        problem(422, 'validation_error', {
          detail: 'Ошибка валидации',
          errors: [{ pointer: '/slug', code: 'pattern', message: 'bad slug' }, { bogus: true }],
        }),
    });
    const err = await workspacesApi.create({ name: 'x', slug: 'y' }, 'k-12345678').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(422);
    expect(e.code).toBe('validation_error');
    expect(e.detail).toBe('Ошибка валидации');
    expect(e.requestId).toBe('req-1');
    expect(e.errors).toEqual([{ pointer: '/slug', code: 'pattern', message: 'bad slug' }]);
  });

  it('maps a non-problem error body to unknown_error with the HTTP status', () => {
    const e = parseProblem(502, undefined, '7');
    expect(e.code).toBe('unknown_error');
    expect(e.status).toBe(502);
    expect(e.retryAfterSeconds).toBe(7);
  });

  it('keeps unknown problem codes (forward compatibility)', () => {
    expect(parseProblem(400, { code: 'brand_new_code', status: 400 }).code).toBe('brand_new_code');
  });
});

describe('api client: 401 handling', () => {
  it('calls the unauthorized handler on 401 for regular endpoints, not for getSession/login', async () => {
    const handler = vi.fn();
    const restore = setUnauthorizedHandler(handler);
    mockFetch({
      'GET /api/v1/workspaces': () => problem(401, 'session_expired'),
      'GET /api/v1/auth/session': () => problem(401, 'unauthenticated'),
    });
    await expect(workspacesApi.list()).rejects.toMatchObject({ code: 'session_expired' });
    expect(handler).toHaveBeenCalledTimes(1);
    await expect(authApi.getSession()).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
    restore();
  });
});
