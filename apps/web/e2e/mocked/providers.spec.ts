import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/** P7-09 CLI Providers: статус/авторизация/лимиты; probe; безопасные настройки; без секретов. */
type S = components['schemas'];

function providers(): S['Provider'][] {
  return [
    {
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
          auth_status: 'AUTHENTICATED',
          availability: 'AVAILABLE',
          capabilities: { json_output: true, sandbox_modes: ['read-only', 'workspace-write'] } as unknown as S['ProviderInstallation']['capabilities'],
          last_error_category: null,
          checked_at: '2026-11-08T08:00:00Z',
        },
      ],
      workspace_settings: { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, allowed_roles: ['developer', 'qa'], settings_version: 3 },
    },
    {
      provider_id: '01900000-0000-7000-b000-000000000002',
      code: 'claude',
      display_name: 'Claude Code',
      enabled_globally: true,
      installations: [
        {
          installation_id: '01900000-0000-7000-b000-000000000021',
          host: 'worker-exec-2',
          detected_version: '2.0.1',
          compatibility: 'UNTESTED',
          auth_status: 'EXPIRED',
          availability: 'UNAVAILABLE',
          capabilities: {},
          last_error_category: 'AUTH_REQUIRED',
          checked_at: '2026-11-08T07:00:00Z',
        },
      ],
      workspace_settings: { enabled: false, max_parallel_sessions: 1, default_timeout_seconds: 3600, allowed_roles: [], settings_version: 0 },
    },
  ];
}

function mockProviders(api: MockApi, items: S['Provider'][], { probeLimited = false } = {}) {
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({ status: 200, body: { items } }));
  api.route('POST', '/api/v1/providers/{id}/probe', 'probeProvider', ({ id }) => {
    if (probeLimited) return { ...problem(429, 'rate_limited'), headers: { 'Retry-After': '600' } };
    return {
      status: 202,
      body: { provider_id: id, probe_request_id: '01900000-0000-7000-b000-0000000000ff', accepted_at: '2026-11-08T09:00:00Z', status_url: '/api/v1/providers' },
    };
  });
  api.route('PATCH', '/api/v1/providers/{id}/settings', 'updateProviderSettings', ({ id, body, headers }) => {
    const p = items.find((x) => x.provider_id === id);
    if (!p) return problem(404, 'not_found');
    if (headers['if-match'] !== `"v${p.workspace_settings.settings_version}"`) return problem(412, 'precondition_failed');
    p.workspace_settings = { ...p.workspace_settings, ...(body as object), settings_version: p.workspace_settings.settings_version + 1 };
    return { status: 200, body: p.workspace_settings, headers: { ETag: `"v${p.workspace_settings.settings_version}"` } };
  });
}

test('providers: status, login guidance without secrets, probe and safe settings (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'admin');
  const items = providers();
  mockProviders(api, items);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'CLI-провайдеры' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/providers`);
  await expect(page.getByRole('heading', { level: 1, name: 'CLI-провайдеры' })).toBeFocused();

  const codex = page.locator('#provider-codex');
  const claude = page.locator('#provider-claude');
  await expect(codex.getByRole('table')).toContainText('Выполнен вход');
  await expect(claude.getByRole('table')).toContainText('Вход истёк');
  await expect(claude).toContainText('Хосты без действующей авторизации: worker-exec-2');
  await expect(claude).toContainText('Не вводите ключи, токены или пароли CLI');
  await expect(codex).not.toContainText('Требуется вход в CLI');
  // Нет полей для секретов: только числовые поля лимитов и флажки.
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  await expect(page.locator('input[type="text"], input:not([type])')).toHaveCount(4);

  await codex.getByText('Возможности на хосте worker-exec-1').click();
  await expect(codex.getByText('"json_output": true')).toBeVisible();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `providers (${scheme})`);
  }

  await claude.getByRole('button', { name: 'Перепроверить' }).click();
  await expect(claude.getByRole('status').filter({ hasText: 'Перепроверка «Claude Code» запрошена' })).toBeVisible();
  const probe = api.requests.find((r) => r.operationId === 'probeProvider');
  expect(probe?.headers['idempotency-key']).toBeTruthy();
  expect(probe?.headers['x-workspace-id']).toBe(ws.id);
  expect(probe?.body).toBeUndefined();

  const form = page.getByRole('form', { name: 'Настройки провайдера Codex CLI' });
  await form.getByLabel('Параллельных сессий (максимум)').fill('99');
  await form.getByRole('button', { name: 'Сохранить настройки' }).click();
  await expect(form.getByLabel('Параллельных сессий (максимум)')).toHaveAttribute('aria-invalid', 'true');
  await expect(form.getByLabel('Параллельных сессий (максимум)')).toBeFocused();
  expect(api.requests.some((r) => r.operationId === 'updateProviderSettings')).toBe(false);
  await form.getByLabel('Параллельных сессий (максимум)').fill('4');
  await form.getByRole('checkbox', { name: 'Ревьюер' }).check();
  await form.getByRole('button', { name: 'Сохранить настройки' }).click();
  await expect(form.getByRole('status')).toHaveText('Настройки провайдера сохранены.');
  const patch = api.requests.find((r) => r.operationId === 'updateProviderSettings');
  expect(patch?.headers['if-match']).toBe('"v3"');
  expect(patch?.body).toEqual({ max_parallel_sessions: 4, allowed_roles: ['developer', 'reviewer', 'qa'] });

  // Повторное сохранение использует новую версию (If-Match "v4").
  await form.getByLabel('Тайм-аут по умолчанию, секунд').fill('900');
  await form.getByRole('button', { name: 'Сохранить настройки' }).click();
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'updateProviderSettings').length).toBe(2);
  expect(api.requests.filter((r) => r.operationId === 'updateProviderSettings')[1]?.headers['if-match']).toBe('"v4"');
  await expect(form.getByRole('status')).toHaveText('Настройки провайдера сохранены.');
});

test('providers: viewer read-only; probe rate limit; empty and error states; 360px', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  const ro = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
  let fail = true;
  const items = providers();
  api.route('GET', '/api/v1/providers', 'listProviders', ({ workspaceId }) => {
    if (workspaceId === ro.id) return { status: 200, body: { items } };
    if (fail) return problem(503, 'service_unavailable');
    return { status: 200, body: { items: [] } };
  });
  api.route('POST', '/api/v1/providers/{id}/probe', 'probeProvider', () => ({ ...problem(429, 'rate_limited'), headers: { 'Retry-After': '600' } }));
  await login(page, api);

  await page.goto(`/w/${ro.id}/providers`);
  await expect(page.getByRole('heading', { level: 2, name: 'Codex CLI' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Перепроверить' })).toHaveCount(0);
  await expect(page.getByRole('form')).toHaveCount(0);
  await expect(page.locator('#provider-codex')).toContainText('Разработчик, QA');
  await expectNoSeriousA11yViolations(page, 'providers (viewer)');

  await page.setViewportSize({ width: 360, height: 740 });
  await page.goto(`/w/${ws.id}/providers`);
  const error = page.getByTestId('error-state');
  await expect(error).toContainText('Сервис временно недоступен');
  await expectNoSeriousA11yViolations(page, 'providers error');
  fail = false;
  await error.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('empty-state')).toContainText('Провайдеры не зарегистрированы');
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'providers empty 360px');
});

test('providers: probe rate limit (429) is shown next to the button', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  mockProviders(api, providers(), { probeLimited: true });
  await login(page, api);
  await page.goto(`/w/${ws.id}/providers`);
  const codex = page.locator('#provider-codex');
  await codex.getByRole('button', { name: 'Перепроверить' }).click();
  await expect(codex.getByRole('status').filter({ hasText: 'Слишком много запросов' })).toBeVisible();
});
