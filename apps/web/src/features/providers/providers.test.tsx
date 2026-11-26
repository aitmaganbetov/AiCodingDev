import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Provider } from '../../api/providers';
import { json, mockFetch, renderApp, session, WS_ID } from '../../test/utils';
import { executionStatusFilter } from './ExecutionHistory';
import { providerStats, recentWindow, tokensText } from './ProviderUsage';
import { buildSettingsPatch, draftFrom } from './settingsForm';
import { latestLimits, untilReset, windowLabel } from './SubscriptionLimits';

const SETTINGS: Provider['workspace_settings'] = {
  enabled: true,
  max_parallel_sessions: 2,
  default_timeout_seconds: 1800,
  allowed_roles: ['developer', 'reviewer'],
  model: null,
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
        capabilities: { json_output: true, available_models: ['gpt-6.1-sol', 'gpt-6-luna', '--flag', 7] },
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

  it('sends the chosen model, clears it with null, and refuses anything that could become a CLI flag', () => {
    const draft = draftFrom(SETTINGS);
    expect(draft.model).toBe('');
    expect(buildSettingsPatch(SETTINGS, { ...draft, model: ' opus ' }).patch).toEqual({ model: 'opus' });
    const chosen = { ...SETTINGS, model: 'opus' };
    expect(buildSettingsPatch(chosen, draftFrom(chosen)).patch).toBeNull();
    // Пустое поле — «модель по умолчанию CLI»: именно null, а не пустая строка.
    expect(buildSettingsPatch(chosen, { ...draftFrom(chosen), model: '' }).patch).toEqual({ model: null });
    for (const bad of ['--dangerously-skip-permissions', 'opus --verbose', 'a/b', 'мо́дель']) {
      const result = buildSettingsPatch(SETTINGS, { ...draft, model: bad });
      expect(result.errors.model, bad).toBe('invalid');
      expect(result.patch).toBeNull();
    }
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

  it('hourly and weekly windows end at the next minute and span exactly the period', () => {
    const now = Date.parse('2026-11-11T06:30:20Z');
    expect(recentWindow(1, now)).toEqual({ from: '2026-11-11T05:31:00.000Z', to: '2026-11-11T06:31:00.000Z' });
    expect(recentWindow(24 * 7, now)).toEqual({ from: '2026-11-04T06:31:00.000Z', to: '2026-11-11T06:31:00.000Z' });
    // В пределах минуты окно (и ключ кэша) не меняется.
    expect(recentWindow(1, now + 30_000)).toEqual(recentWindow(1, now));
  });

  it('shows tokens spent in the last hour and in the last 7 days per provider', async () => {
    const summary = (total: number | null, executions: number) => ({
      providers: [{ ...stats, executions, tokens: { total, source: total === null ? 'not_exposed_by_cli' : 'cli_reported', executions_reported: executions } }],
    });
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/providers': () => json(200, { items: [provider()] }),
      'GET /api/v1/analytics/summary': (req) => {
        const hours = (Date.parse(req.url.searchParams.get('to') ?? '') - Date.parse(req.url.searchParams.get('from') ?? '')) / 3_600_000;
        return json(200, hours === 1 ? summary(1500, 2) : hours === 168 ? summary(98000, 31) : { providers: [] });
      },
    });
    renderApp(`/w/${WS_ID}/providers`);
    const block = await screen.findByTestId('token-windows-codex');
    await waitFor(() => expect(block.querySelector('[data-window="1"] [data-metric="tokens"]')?.textContent).toMatch(/1\s500/));
    expect(block.querySelector('[data-window="1"]')?.textContent).toContain('2 исполнения');
    expect(block.querySelector('[data-window="168"] [data-metric="tokens"]')?.textContent).toMatch(/98\s000/);
    // Честная подпись: это измеренный расход, а не остаток лимита подписки.
    expect(within(block).getByText(/Запросы вне платформы сюда не входят/)).toBeInTheDocument();
  });

  it('execution status filter maps to repeated status values', () => {
    expect(executionStatusFilter('')).toBeUndefined();
    expect(executionStatusFilter('failures')).toEqual(['TIMED_OUT', 'CRASHED', 'AUTH_REQUIRED', 'RATE_LIMITED']);
    expect(executionStatusFilter('COLLECTED')).toEqual(['COLLECTED']);
    expect(executionStatusFilter('nope')).toBeUndefined();
  });
});

describe('subscription limits', () => {
  const t = ((key: string, o?: Record<string, unknown>) => `${key}${o ? JSON.stringify(o) : ''}`) as never;
  const NOW = Date.parse('2026-11-11T07:02:00Z');
  const limits = {
    provider: 'claude',
    observed_at: '2026-11-11T07:00:00Z',
    windows: [
      { kind: 'five_hour', used: 0.25, resets_at: '2026-11-11T09:20:00Z', status: 'allowed' },
      { kind: 'seven_day', used: 0.31, resets_at: '2026-11-15T23:00:00Z', status: 'allowed' },
      { kind: 'seven_day_fable', used: 0.17, resets_at: null, status: null },
    ],
    credits: null,
  };

  it('names windows the way the CLI reports them and counts time to reset', () => {
    expect(windowLabel(t, 'five_hour')).toBe('providers.limits.window.five_hour');
    expect(windowLabel(t, 'seven_day_fable')).toContain('"model":"Fable"');
    expect(windowLabel(t, 'monthly')).toBe('monthly');
    expect(untilReset(t, '2026-11-11T09:20:00Z', NOW)).toContain('"hours":2,"minutes":18');
    expect(untilReset(t, '2026-11-15T23:00:00Z', NOW)).toContain('"days":4,"hours":15');
    expect(untilReset(t, '2026-11-11T07:30:00Z', NOW)).toContain('"minutes":28');
    expect(untilReset(t, '2026-11-11T06:00:00Z', NOW)).toBeNull();
    expect(untilReset(t, null, NOW)).toBeNull();
  });

  it('takes the freshest observation among installations', () => {
    const older = { ...limits, observed_at: '2026-11-10T00:00:00Z' };
    expect(latestLimits({ installations: [] })).toBeNull();
    expect(
      latestLimits({
        installations: [
          { ...provider().installations[0]!, subscription_limits: older },
          { ...provider().installations[0]!, subscription_limits: limits },
          { ...provider().installations[0]!, subscription_limits: null },
        ],
      })?.observed_at,
    ).toBe('2026-11-11T07:00:00Z');
  });

  it('shows what the CLI reported, says so when a CLI reports nothing, and refreshes on demand', async () => {
    const user = userEvent.setup();
    const claude = {
      ...provider(),
      provider_id: '01900000-0000-7000-b000-000000000002',
      code: 'claude' as const,
      display_name: 'Claude Code CLI',
      installations: [{ ...provider().installations[0]!, subscription_limits: limits }],
    };
    const codexLimits = {
      provider: 'codex',
      observed_at: '2026-11-11T07:00:00Z',
      windows: [{ kind: 'seven_day', used: 0.04, resets_at: '2026-11-18T04:27:31Z', status: null }],
      credits: { balance: '0', unlimited: false },
    };
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/providers': () => json(200, { items: [{ ...provider(), installations: [{ ...provider().installations[0]!, subscription_limits: codexLimits }] }, claude] }),
      'POST /api/v1/providers/01900000-0000-7000-b000-000000000002/limits/refresh': () =>
        json(202, { provider_id: claude.provider_id, probe_request_id: 'p-1', accepted_at: '2026-11-11T07:00:00Z', status_url: '/x' }),
      'GET /api/v1/providers/01900000-0000-7000-b000-000000000002/probes/p-1': () =>
        json(200, { probe_request_id: 'p-1', provider_id: claude.provider_id, status: 'COMPLETED', requested_at: '2026-11-11T07:00:00Z', completed_at: '2026-11-11T07:00:05Z', installation: null, error_category: null }),
    });
    renderApp(`/w/${WS_ID}/providers`);
    const block = await screen.findByTestId('limits-claude');
    expect(block.querySelector('[data-window="five_hour"] [data-metric="used"]')?.textContent).toBe('25%');
    expect(block.querySelector('[data-window="five_hour"]')?.textContent).toContain('осталось 75%');
    expect(block.querySelector('[data-window="seven_day"] [data-metric="used"]')?.textContent).toBe('31%');
    expect(block.querySelector('[data-window="seven_day_fable"]')?.textContent).toContain('Неделя · Fable');
    expect(within(block).getAllByRole('progressbar')).toHaveLength(3);
    // Codex: недельное окно и кредиты, как их сообщил его app-server; обновление подписку не расходует.
    const codex = screen.getByTestId('limits-codex');
    expect(codex.querySelector('[data-window="seven_day"] [data-metric="used"]')?.textContent).toBe('4%');
    expect(codex.querySelector('[data-window="seven_day"]')?.textContent).toContain('осталось 96%');
    expect(screen.getByTestId('credits-codex').textContent).toBe('Кредиты0');
    expect(codex.textContent).toContain('подписку не расходует');
    expect(block.textContent).toContain('немного расходует подписку');

    await user.click(within(block).getByRole('button', { name: 'Обновить' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.pathname.endsWith('/limits/refresh'))).toBe(true));
    const post = calls.find((c) => c.url.pathname.endsWith('/limits/refresh'));
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    // После итога проверки список провайдеров перечитывается — в нём новое наблюдение.
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET' && c.url.pathname === '/api/v1/providers').length).toBeGreaterThan(1));
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
      expect(input.getAttribute('id') ?? '').toMatch(/parallel|timeout|model/);
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

  it('lets the operator choose the CLI model for the provider', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/providers': () => json(200, { items: [provider()] }),
      'PATCH /api/v1/providers/01900000-0000-7000-b000-000000000001/settings': (req) =>
        json(200, { ...SETTINGS, ...(req.body as object), settings_version: 4 }),
    });
    renderApp(`/w/${WS_ID}/providers`);
    const form = await screen.findByRole('form', { name: 'Настройки провайдера Codex CLI' });
    // Выпадающий список: «по умолчанию», модели из каталога CLI (probe) и «Другая модель…».
    const model = within(form).getByLabelText('Модель');
    expect(model.tagName).toBe('SELECT');
    expect(model).toHaveValue('');
    expect(within(model).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'По умолчанию (решает сам CLI)',
      'gpt-6.1-sol',
      'gpt-6-luna',
      'Другая модель…',
    ]);
    await user.selectOptions(model, 'gpt-6-luna');
    await user.click(within(form).getByRole('button', { name: 'Сохранить настройки' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')?.body).toEqual({ model: 'gpt-6-luna' }));
    await waitFor(() => expect(within(form).getByLabelText('Модель')).toHaveValue('gpt-6-luna'));
  });

  it('still accepts a model the CLI did not list', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/providers': () => json(200, { items: [provider()] }),
      'PATCH /api/v1/providers/01900000-0000-7000-b000-000000000001/settings': (req) =>
        json(200, { ...SETTINGS, ...(req.body as object), settings_version: 4 }),
    });
    renderApp(`/w/${WS_ID}/providers`);
    const form = await screen.findByRole('form', { name: 'Настройки провайдера Codex CLI' });
    await user.selectOptions(within(form).getByLabelText('Модель'), 'Другая модель…');
    await user.type(within(form).getByLabelText('Имя модели'), 'my-private-model');
    await user.click(within(form).getByRole('button', { name: 'Сохранить настройки' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'PATCH')?.body).toEqual({ model: 'my-private-model' }));
    // Сохранённая модель не из каталога остаётся в списке выбранной, а не теряется.
    await waitFor(() => expect(within(form).getByLabelText('Модель')).toHaveValue('my-private-model'));
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
