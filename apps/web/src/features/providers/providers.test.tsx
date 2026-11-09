import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Provider } from '../../api/providers';
import { json, mockFetch, renderApp, session, WS_ID } from '../../test/utils';
import { executionStatusFilter } from './ExecutionHistory';
import { providerStats, tokensText } from './ProviderUsage';
import { buildSettingsPatch, draftFrom } from './settingsForm';

const SETTINGS: Provider['workspace_settings'] = {
  enabled: true,
  max_parallel_sessions: 2,
  default_timeout_seconds: 1800,
  allowed_roles: ['developer', 'reviewer'],
  settings_version: 3,
};

function provider(auth: Provider['installations'][number]['auth_status'] = 'AUTHENTICATED'): Provider {
  return {
    provider_id: '01900000-0000-7000-b000-000000000001',
    code: 'codex',
    display_name: 'Codex CLI',
    enabled_globally: true,
    installations: [
      {
        installation_id: '01900000-0000-7000-b000-000000000011',
        host: 'worker-exec-1',
        detected_version: '0.42.0',
        compatibility: 'SUPPORTED',
        auth_status: auth,
        availability: 'AVAILABLE',
        capabilities: { json_output: true },
        last_error_category: null,
        checked_at: '2026-11-08T08:00:00Z',
      },
    ],
    workspace_settings: SETTINGS,
  };
}

describe('buildSettingsPatch', () => {
  it('returns only changed fields, validates ranges, and null for no changes', () => {
    const draft = draftFrom(SETTINGS);
    expect(buildSettingsPatch(SETTINGS, draft).patch).toBeNull();
    expect(buildSettingsPatch(SETTINGS, { ...draft, maxParallel: '4', roles: ['reviewer', 'developer', 'qa'] }).patch).toEqual({
      max_parallel_sessions: 4,
      allowed_roles: ['developer', 'reviewer', 'qa'],
    });
    expect(buildSettingsPatch(SETTINGS, { ...draft, maxParallel: '65', timeout: '59' }).errors).toEqual({
      max_parallel_sessions: 'range',
      default_timeout_seconds: 'range',
    });
    expect(buildSettingsPatch(SETTINGS, { ...draft, maxParallel: '1.5' }).errors.max_parallel_sessions).toBe('range');
  });
});

describe('provider usage and history (F5)', () => {
  const stats = {
    provider: 'codex',
    executions: 10,
    collected: 8,
    errors: 1,
    rate_limited: 1,
    auth_required: 0,
    cancelled: 0,
    in_progress: 0,
    success_rate: 0.8,
    avg_duration_seconds: 42,
    failures_by_category: {},
    tokens: { total: null, source: 'not_exposed_by_cli' as const, executions_reported: 0 },
  };
  const t = (key: string, o?: Record<string, unknown>) => `${key}${o ? JSON.stringify(o) : ''}`;

  it('tokens only when reported by CLI; never invented', () => {
    expect(tokensText(t, stats)).toBe('providers.usage.tokensNotExposed');
    expect(tokensText(t, { ...stats, tokens: { total: 1200, source: 'cli_reported', executions_reported: 10 } })).toContain('tokensReported');
    expect(tokensText(t, { ...stats, tokens: { total: 600, source: 'mixed', executions_reported: 5 } })).toContain('"reported":5');
    expect(providerStats(undefined, 'codex')).toBeNull();
  });

  it('execution status filter maps to repeated status values', () => {
    expect(executionStatusFilter('')).toBeUndefined();
    expect(executionStatusFilter('failures')).toEqual(['TIMED_OUT', 'CRASHED', 'AUTH_REQUIRED', 'RATE_LIMITED']);
    expect(executionStatusFilter('COLLECTED')).toEqual(['COLLECTED']);
    expect(executionStatusFilter('nope')).toBeUndefined();
  });
});

describe('ProvidersPage', () => {
  it('shows status only: no credential fields, login guidance for AUTH_REQUIRED', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/providers': () => json(200, { items: [provider('AUTH_REQUIRED')] }),
    });
    const { container } = renderApp(`/w/${WS_ID}/providers`);
    // Первый рендер приложения под параллельной нагрузкой vitest может занять > 1 с.
    expect(await screen.findByRole('heading', { level: 2, name: 'Codex CLI' })).toBeInTheDocument();
    expect(screen.getByText('Требуется вход в CLI')).toBeInTheDocument();
    expect(screen.getByText(/Хосты без действующей авторизации: worker-exec-1/)).toBeInTheDocument();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    // Поля экрана (в main); поиск оболочки в шапке — не поле провайдера.
    for (const input of container.querySelectorAll('main input:not([type="checkbox"])')) {
      expect(input.getAttribute('id') ?? '').toMatch(/parallel|timeout/);
    }
    expect(screen.queryByLabelText(/токен|ключ|пароль/i)).toBeNull();
  });

  it('probe posts with Idempotency-Key and X-Workspace-Id, polls the status URL; settings PATCH sends If-Match and changed fields only', async () => {
    const user = userEvent.setup();
    let statusCalls = 0;
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/providers': () => json(200, { items: [provider()] }),
      'POST /api/v1/providers/01900000-0000-7000-b000-000000000001/probe': () =>
        json(202, {
          provider_id: '01900000-0000-7000-b000-000000000001',
          probe_request_id: '01900000-0000-7000-b000-0000000000ff',
          accepted_at: '2026-11-08T09:00:00Z',
          status_url: '/api/v1/providers/01900000-0000-7000-b000-000000000001/probes/01900000-0000-7000-b000-0000000000ff',
        }),
      'GET /api/v1/providers/01900000-0000-7000-b000-000000000001/probes/01900000-0000-7000-b000-0000000000ff': () => {
        statusCalls += 1;
        return json(200, {
          probe_request_id: '01900000-0000-7000-b000-0000000000ff',
          provider_id: '01900000-0000-7000-b000-000000000001',
          status: statusCalls > 1 ? 'COMPLETED' : 'PENDING',
          requested_at: '2026-11-08T09:00:00Z',
          completed_at: statusCalls > 1 ? '2026-11-08T09:00:05Z' : null,
          installation: statusCalls > 1 ? { ...provider().installations[0], auth_status: 'AUTHENTICATED' } : null,
          error_category: null,
        });
      },
      'PATCH /api/v1/providers/01900000-0000-7000-b000-000000000001/settings': (req) =>
        json(200, { ...SETTINGS, ...(req.body as object), settings_version: 4 }),
    });
    renderApp(`/w/${WS_ID}/providers`);
    await user.click(await screen.findByRole('button', { name: 'Перепроверить' }));
    expect(await screen.findByText(/Перепроверка «Codex CLI» выполняется/, { selector: 'p' })).toBeInTheDocument();
    // OQ-45 (c): статус опрашивается по status_url до финального состояния.
    expect(await screen.findByText(/Проверка завершена: хост worker-exec-1, авторизация — Выполнен вход/)).toBeInTheDocument();
    expect(calls.filter((c) => c.url.pathname.includes('/probes/')).every((c) => c.headers.get('X-Workspace-Id') === WS_ID)).toBe(true);
    const probe = calls.find((c) => c.method === 'POST');
    expect(probe?.headers.get('Idempotency-Key')).toBeTruthy();
    expect(probe?.headers.get('X-Workspace-Id')).toBe(WS_ID);
    expect(probe?.body).toBeUndefined();

    const form = screen.getByRole('form', { name: 'Настройки провайдера Codex CLI' });
    const parallel = within(form).getByLabelText('Параллельных сессий (максимум)');
    await user.clear(parallel);
    await user.type(parallel, '5');
    await user.click(within(form).getByRole('button', { name: 'Сохранить настройки' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true));
    const patch = calls.find((c) => c.method === 'PATCH');
    expect(patch?.headers.get('If-Match')).toBe('"v3"');
    expect(patch?.body).toEqual({ max_parallel_sessions: 5 });
  });

  it('viewer sees read-only settings and no probe button', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/providers': () => json(200, { items: [provider()] }),
    });
    renderApp(`/w/${WS_ID}/providers`);
    // Первый рендер приложения под параллельной нагрузкой vitest может занять > 1 с.
    expect(await screen.findByRole('heading', { level: 2, name: 'Codex CLI' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Перепроверить' })).toBeNull();
    expect(screen.queryByRole('form')).toBeNull();
    expect(screen.getByText('Разработчик, Ревьюер')).toBeInTheDocument();
  });
});
