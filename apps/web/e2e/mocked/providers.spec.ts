import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { emptyAnalyticsSummary, problem, type MockApi } from '../support/mockApi';
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

const PROBE_ID = '01900000-0000-7000-b000-0000000000ff';

function mockProviders(api: MockApi, items: S['Provider'][], { probeLimited = false } = {}) {
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({ status: 200, body: { items } }));
  api.route('POST', '/api/v1/providers/{id}/probe', 'probeProvider', ({ id }) => {
    if (probeLimited) return { ...problem(429, 'rate_limited'), headers: { 'Retry-After': '600' } };
    return {
      status: 202,
      body: {
        provider_id: id,
        probe_request_id: PROBE_ID,
        accepted_at: '2026-11-08T09:00:00Z',
        status_url: `/api/v1/providers/${id}/probes/${PROBE_ID}`,
      },
    };
  });
  // OQ-45 (c): статус запроса проверки — первый опрос PENDING, затем COMPLETED с проверенной установкой.
  let polls = 0;
  api.route('GET', `/api/v1/providers/{id}/probes/${PROBE_ID}`, 'getProviderProbe', ({ id }) => {
    const p = items.find((x) => x.provider_id === id);
    if (!p) return problem(404, 'not_found');
    polls += 1;
    const done = polls > 1;
    return {
      status: 200,
      body: {
        probe_request_id: PROBE_ID,
        provider_id: p.provider_id,
        status: done ? 'COMPLETED' : 'PENDING',
        requested_at: '2026-11-08T09:00:00Z',
        completed_at: done ? '2026-11-08T09:00:04Z' : null,
        installation: done && p.installations[0] ? { ...p.installations[0], auth_status: 'AUTHENTICATED' } : null,
        error_category: null,
      },
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
  // С F2 CLI-провайдеры — подраздел Настроек (D-3); переход — по чипу статуса в верхней панели.
  await page.getByRole('list', { name: 'Состояние CLI-провайдеров' }).getByRole('link', { name: /^Codex CLI/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/settings/providers`);
  await expect(page.getByRole('navigation', { name: 'Разделы настроек' }).getByRole('link', { name: 'CLI-провайдеры' })).toHaveAttribute('aria-current', 'page');
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
  await expect(claude.getByRole('status').filter({ hasText: 'Перепроверка «Claude Code» выполняется' })).toBeVisible();
  await expect(claude.getByRole('status').filter({ hasText: 'Проверка завершена: хост worker-exec-2, авторизация — Выполнен вход.' })).toBeVisible({
    timeout: 10_000,
  });
  const polls = api.requests.filter((r) => r.operationId === 'getProviderProbe');
  expect(polls.length).toBeGreaterThanOrEqual(2);
  expect(polls.every((r) => r.headers['x-workspace-id'] === ws.id)).toBe(true);
  await expectNoSeriousA11yViolations(page, 'providers probe status');
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

/* ── F5: история исполнений (B1), измеренное использование, предупреждение UNTESTED, адаптивность ── */

function execution(i: number, providerCode: S['CliProvider'], status: S['ExecutionStatus']): S['ProviderExecution'] {
  const failed = status === 'TIMED_OUT' || status === 'CRASHED';
  return {
    id: `01900000-0000-7000-f000-${String(i).padStart(12, '0')}`,
    provider: providerCode,
    installation_id: '01900000-0000-7000-b000-000000000011',
    task_attempt_id: `01900000-0000-7000-e000-${String(i).padStart(12, '0')}`,
    attempt_no: 1 + (i % 2),
    task_id: `01900000-0000-7000-c000-${String(i).padStart(12, '0')}`,
    run_id: '01900000-0000-7000-d000-000000000001',
    agent_role: i % 3 === 0 ? 'reviewer' : 'developer',
    status,
    exit_code: failed ? null : 0,
    failure_category: failed ? 'TIMEOUT' : null,
    timeout_seconds: 1800,
    output_artifact_id: null,
    created_at: new Date(Date.parse('2026-11-08T09:00:00Z') - i * 60_000).toISOString().replace('.000Z', 'Z'),
    started_at: '2026-11-08T08:00:00Z',
    finished_at: '2026-11-08T08:01:05Z',
    duration_ms: 65_000,
  };
}

const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

test('providers: execution history (virtualized, status filter), measured usage without quotas, UNTESTED warning; viewer read-only (axe light/dark)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'owner');
  const ro = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
  const items = providers();
  mockProviders(api, items);
  const all = Array.from({ length: 150 }, (_, i) => execution(i + 1, 'codex', i % 10 === 0 ? 'TIMED_OUT' : 'COLLECTED'));
  api.route('GET', '/api/v1/providers/{id}/executions', 'listProviderExecutions', ({ id, url }) => {
    const statuses = url.searchParams.getAll('status');
    const limit = Number(url.searchParams.get('limit') ?? 50);
    const start = Number(url.searchParams.get('cursor') ? Buffer.from(url.searchParams.get('cursor') ?? '', 'base64url').toString('utf8') : 0);
    const own = id === items[0]?.provider_id ? all.filter((e) => statuses.length === 0 || statuses.includes(e.status)) : [];
    const page = own.slice(start, start + limit);
    const end = start + limit;
    return { status: 200, body: { items: page, next_cursor: end < own.length ? Buffer.from(String(end)).toString('base64url') : null } };
  });
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', () => {
    const summary = emptyAnalyticsSummary();
    summary.providers = [
      {
        provider: 'codex',
        executions: 150,
        collected: 135,
        errors: 15,
        rate_limited: 0,
        auth_required: 0,
        cancelled: 0,
        in_progress: 0,
        success_rate: 0.9,
        avg_duration_seconds: 65,
        failures_by_category: { TIMEOUT: 15 },
        tokens: { total: null, source: 'not_exposed_by_cli', executions_reported: 0 },
      },
    ];
    return { status: 200, body: summary };
  });
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/providers`);
  const codex = page.locator('#provider-codex');
  const claude = page.locator('#provider-claude');
  await expect(codex.getByTestId('provider-state-codex')).toHaveAttribute('data-state', 'ready');
  await expect(codex.getByTestId('provider-state-codex')).toContainText('Проверено');
  // UNTESTED — предупреждение о непроверенной версии.
  await expect(claude).toContainText('Версия CLI не проверена на совместимость');
  await expect(claude).toContainText('worker-exec-2 (2.0.1)');
  await expect(codex).not.toContainText('Версия CLI не проверена');

  // Использование: только измеренное; токены — «CLI не сообщает», квоты не показываются.
  const usage = codex.getByTestId('usage-codex');
  await expect(usage.locator('[data-metric="executions"]')).toHaveText('150');
  await expect(usage.locator('[data-metric="successRate"]')).toHaveText('90%');
  await expect(usage.locator('[data-metric="tokens"]')).toHaveText('CLI не сообщает токены');
  await expect(usage).toContainText('Тайм-аут: 15');
  await expect(usage).toContainText('Квоты, лимиты и остаток подписки CLI не сообщают');
  await expect(page.locator('main')).not.toContainText(/остаток\s+\d|квота:\s*\d/i);
  await expect(claude.getByTestId('usage-none')).toHaveText('Нет исполнений в окне analytics.');

  // История: виртуализация (в DOM — только окно из 50 загруженных), подгрузка, фильтр статуса на сервере.
  const history = codex.getByRole('list', { name: 'История исполнений Codex CLI' });
  await expect(history).toBeVisible();
  await expect(history.getByRole('listitem').first()).toHaveAttribute('aria-setsize', '50');
  expect(await history.getByRole('listitem').count()).toBeLessThan(50);
  await expect(history.getByRole('listitem').first()).toContainText('Тайм-аут');
  await expect(history.getByRole('listitem').first().getByRole('link', { name: /Mission Control/ })).toHaveAttribute(
    'href',
    `/w/${ws.id}/mission-control?run=01900000-0000-7000-d000-000000000001&task=01900000-0000-7000-c000-000000000001`,
  );
  await codex.getByRole('button', { name: 'Загрузить ещё' }).click();
  await expect(history.getByRole('listitem').first()).toHaveAttribute('aria-setsize', '100');
  await codex.getByLabel('Статус исполнения').selectOption({ label: 'Только сбои' });
  await expect(history.getByRole('listitem').first()).toHaveAttribute('aria-setsize', '15');
  const lastHistory = api.requests.filter((r) => r.operationId === 'listProviderExecutions').at(-1);
  expect(lastHistory?.headers['x-workspace-id']).toBe(ws.id);
  await expect(claude.getByTestId('executions-empty')).toHaveText('Исполнений нет.');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `providers F5 (${scheme})`);
  }

  // RBAC: viewer — история и использование видны, перепроверки и формы нет.
  await page.goto(`/w/${ro.id}/settings/providers`);
  await expect(page.locator('#provider-codex').getByRole('list', { name: 'История исполнений Codex CLI' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Перепроверить' })).toHaveCount(0);
  await expect(page.getByRole('form')).toHaveCount(0);
  await expectNoSeriousA11yViolations(page, 'providers F5 viewer');
});

test('providers: 375/768/1366/1920 without horizontal overflow in both themes', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда', 'team', 'admin');
  mockProviders(api, providers());
  api.route('GET', '/api/v1/providers/{id}/executions', 'listProviderExecutions', ({ id }) => ({
    status: 200,
    body: { items: id === '01900000-0000-7000-b000-000000000001' ? Array.from({ length: 8 }, (_, i) => execution(i + 1, 'codex', 'COLLECTED')) : [], next_cursor: null },
  }));
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/providers`);
  await expect(page.locator('#provider-codex').getByRole('list', { name: 'История исполнений Codex CLI' })).toBeVisible();
  for (const width of [375, 768, 1366, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const scheme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
      if (SHOTS && (width === 1366 || width === 375)) {
        mkdirSync(SHOTS, { recursive: true });
        await page.evaluate(() => document.fonts.ready);
        await page.screenshot({ path: join(SHOTS, `providers-${scheme}-${width}.png`), fullPage: true });
      }
    }
  }
});
