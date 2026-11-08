import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { vi } from 'vitest';
import { App } from '../App';
import type { Project, SessionInfo, WorkspaceRole } from '../api/client';
import { createQueryClient } from '../queryClient';

export interface RecordedRequest {
  method: string;
  url: URL;
  headers: Headers;
  body: unknown;
}

type Handler = (req: RecordedRequest) => Response | Promise<Response>;

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}

export function problem(status: number, code: string, extra: Record<string, unknown> = {}): Response {
  return new Response(
    JSON.stringify({ type: `urn:aicd:problem:${code}`, title: code, status, code, request_id: 'req-1', ...extra }),
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  );
}

/** Подменяет fetch: маршруты вида "GET /api/v1/auth/session" (без query). */
export function mockFetch(routes: Record<string, Handler>) {
  const calls: RecordedRequest[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const method = (init?.method ?? 'GET').toUpperCase();
    const req: RecordedRequest = {
      method,
      url,
      headers: new Headers(init?.headers),
      body: typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined,
    };
    calls.push(req);
    const handler = routes[`${method} ${url.pathname}`];
    if (!handler) return problem(404, 'not_found');
    return handler(req);
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

export const WS_ID = '01900000-0000-7000-8000-000000000001';

export function session(role: WorkspaceRole = 'owner', workspaces = 1): SessionInfo {
  return {
    user: { id: '01900000-0000-7000-8000-0000000000aa', email: 'anna@example.com', display_name: 'Анна' },
    csrf_token: 'csrf-from-session',
    expires_at: '2026-11-08T09:00:00Z',
    idle_expires_at: '2026-11-07T21:30:00Z',
    workspaces: Array.from({ length: workspaces }, (_, i) => ({
      workspace_id: i === 0 ? WS_ID : `01900000-0000-7000-8000-00000000000${i + 1}`,
      slug: `team-${i + 1}`,
      name: `Команда ${i + 1}`,
      role,
    })),
  };
}

export function project(n: number, overrides: Partial<Project> = {}): Project {
  return {
    id: `01900000-0000-7000-9000-${String(n).padStart(12, '0')}`,
    workspace_id: WS_ID,
    name: `Проект ${n}`,
    description: null,
    status: 'ACTIVE',
    settings: { documentation_mode: 'SMART', quality_profile: 'strict', settings_version: 0 },
    version: 0,
    created_at: '2026-11-07T10:00:00Z',
    updated_at: '2026-11-07T10:00:00Z',
    ...overrides,
  };
}

export function renderApp(path: string) {
  const queryClient = createQueryClient();
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App queryClient={queryClient} />
    </MemoryRouter>,
  );
}
