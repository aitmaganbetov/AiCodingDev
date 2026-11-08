import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Provider } from '../../api/providers';
import { json, mockFetch, renderApp, session, WS_ID } from '../../test/utils';
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
        capabilities: { json_output: true } as unknown as Provider['installations'][number]['capabilities'],
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

describe('ProvidersPage', () => {
  it('shows status only: no credential fields, login guidance for AUTH_REQUIRED', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/providers': () => json(200, { items: [provider('AUTH_REQUIRED')] }),
    });
    const { container } = renderApp(`/w/${WS_ID}/providers`);
    expect(await screen.findByRole('heading', { level: 2, name: 'Codex CLI' })).toBeInTheDocument();
    expect(screen.getByText('Требуется вход в CLI')).toBeInTheDocument();
    expect(screen.getByText(/Хосты без действующей авторизации: worker-exec-1/)).toBeInTheDocument();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    for (const input of container.querySelectorAll('input:not([type="checkbox"])')) {
      expect(input.getAttribute('id') ?? '').toMatch(/parallel|timeout/);
    }
    expect(screen.queryByLabelText(/токен|ключ|пароль/i)).toBeNull();
  });

  it('probe posts with Idempotency-Key and X-Workspace-Id; settings PATCH sends If-Match and changed fields only', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/providers': () => json(200, { items: [provider()] }),
      'POST /api/v1/providers/01900000-0000-7000-b000-000000000001/probe': () =>
        json(202, {
          provider_id: '01900000-0000-7000-b000-000000000001',
          probe_request_id: '01900000-0000-7000-b000-0000000000ff',
          accepted_at: '2026-11-08T09:00:00Z',
          status_url: '/api/v1/providers',
        }),
      'PATCH /api/v1/providers/01900000-0000-7000-b000-000000000001/settings': (req) =>
        json(200, { ...SETTINGS, ...(req.body as object), settings_version: 4 }),
    });
    renderApp(`/w/${WS_ID}/providers`);
    await user.click(await screen.findByRole('button', { name: 'Перепроверить' }));
    expect(await screen.findByText(/Перепроверка «Codex CLI» запрошена/, { selector: 'p' })).toBeInTheDocument();
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
    expect(await screen.findByRole('heading', { level: 2, name: 'Codex CLI' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Перепроверить' })).toBeNull();
    expect(screen.queryByRole('form')).toBeNull();
    expect(screen.getByText('Разработчик, Ревьюер')).toBeInTheDocument();
  });
});
