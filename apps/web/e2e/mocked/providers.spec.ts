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
      workspace_settings: { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, model: null, allowed_roles: ['developer', 'qa'], settings_version: 3 },
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
      workspace_settings: { enabled: false, max_parallel_sessions: 1, default_timeout_seconds: 3600, model: null, allowed_roles: [], settings_version: 0 },
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

/**
 * Device-code вход CLI из UI (P2-01, cli-auth.md): владелец нажимает «Войти», платформа запускает
 * официальную команду входа CLI на хосте исполнения, UI показывает ссылку и код, которые напечатал CLI.
 * Проверяется и главное свойство: экран нигде не просит и не принимает токен или пароль провайдера.
 */
const LOGIN_ID = '01900000-0000-7000-b000-0000000000ee';
const LOGIN_URL = 'https://claude.com/cai/oauth/authorize?code=true&code_challenge=7r2Avt&state=LYqBZu';

function mockLogin(api: MockApi, items: S['Provider'][], { stepUp = false, awaitCode = false } = {}) {
  let attempts = 0;
  let polls = 0;
  let cancelled = false;
  // Claude Code завершает вход вводом кода из браузера: сессия ждёт его, пока оператор не отправит.
  let codeSubmitted = false;
  api.route('POST', '/api/v1/providers/{id}/login', 'loginProvider', ({ id }) => {
    attempts += 1;
    // Вход привязывает подписку к хосту: сервер требует подтверждения пароля (SC-IAM-08).
    if (stepUp && attempts === 1) return problem(403, 'step_up_required', { max_age_seconds: 600 });
    return {
      status: 202,
      body: {
        provider_id: id,
        login_session_id: LOGIN_ID,
        accepted_at: '2026-11-08T09:00:00Z',
        status_url: `/api/v1/providers/${id}/logins/${LOGIN_ID}`,
      },
    };
  });
  api.route('GET', `/api/v1/providers/{id}/logins/${LOGIN_ID}`, 'getProviderLogin', ({ id }) => {
    const p = items.find((x) => x.provider_id === id);
    if (!p) return problem(404, 'not_found');
    polls += 1;
    // PENDING → CHALLENGE (CLI напечатал ссылку) → AUTHENTICATED, либо CANCELLED по кнопке.
    const waiting = awaitCode && !codeSubmitted && !cancelled;
    const state: S['ProviderLoginStatus']['state'] = cancelled
      ? 'CANCELLED'
      : polls === 1
        ? 'PENDING'
        : waiting || polls < 4
          ? 'CHALLENGE'
          : 'AUTHENTICATED';
    const awaiting = waiting && state === 'CHALLENGE';
    return {
      status: 200,
      body: {
        login_session_id: LOGIN_ID,
        provider_id: p.provider_id,
        state,
        verification_url: state === 'PENDING' ? null : LOGIN_URL,
        user_code: state === 'PENDING' ? null : 'WDJB-MJHT',
        awaiting_code: awaiting,
        output_tail:
          state === 'PENDING'
            ? ''
            : "Opening browser to sign in…\nIf the browser didn't open, visit: " +
              LOGIN_URL +
              (awaiting ? '\nPaste code here if prompted >' : ''),
        error_category: state === 'CANCELLED' ? 'CANCELLED' : null,
        host: 'worker-exec-2',
        created_at: '2026-11-08T09:00:00Z',
        updated_at: '2026-11-08T09:00:06Z',
        expires_at: '2026-11-08T09:10:00Z',
      },
    };
  });
  api.route('POST', `/api/v1/providers/{id}/logins/${LOGIN_ID}/cancel`, 'cancelProviderLogin', ({ id }) => {
    cancelled = true;
    return { status: 202, body: { login_session_id: LOGIN_ID, provider_id: id, state: 'CANCELLED', verification_url: LOGIN_URL, user_code: 'WDJB-MJHT', awaiting_code: false, output_tail: '', error_category: 'CANCELLED', host: 'worker-exec-2', created_at: '2026-11-08T09:00:00Z', updated_at: '2026-11-08T09:00:08Z', expires_at: '2026-11-08T09:10:00Z' } };
  });
  api.route('POST', `/api/v1/providers/{id}/logins/${LOGIN_ID}/code`, 'submitProviderLoginCode', ({ id }) => {
    codeSubmitted = true;
    return { status: 202, body: { login_session_id: LOGIN_ID, provider_id: id, state: 'CHALLENGE', verification_url: LOGIN_URL, user_code: 'WDJB-MJHT', awaiting_code: false, output_tail: '— код авторизации отправлен CLI —', error_category: null, host: 'worker-exec-2', created_at: '2026-11-08T09:00:00Z', updated_at: '2026-11-08T09:00:10Z', expires_at: '2026-11-08T09:10:00Z' } };
  });
  api.route('POST', '/api/v1/auth/reauthenticate', 'reauthenticate', ({ body }) =>
    (body as S['ReauthenticateRequest']).password === 'correct horse battery staple'
      ? { status: 200, body: { ...api.session(), reauthenticated_at: '2026-11-08T09:00:02Z' } }
      : problem(403, 'reauthentication_failed'),
  );
}

test('providers: owner signs the CLI in by subscription — device code from the CLI, no secrets asked (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const items = providers();
  mockProviders(api, items);
  mockLogin(api, items, { stepUp: true });
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/providers`);

  const claude = page.locator('#provider-claude');
  const section = claude.getByTestId('provider-login-claude');
  await expect(section).toContainText('Платформа не принимает и не хранит токены');
  // Нигде не предлагается ввести секрет: ни поля пароля, ни поля для ключа.
  await expect(page.locator('input[type="password"]')).toHaveCount(0);

  await section.getByRole('button', { name: 'Войти' }).click();
  // Step-up: сервер просит пароль пользователя платформы (не провайдера), затем запрос повторяется.
  const stepUp = page.getByRole('dialog', { name: 'Подтвердите пароль' });
  await expect(stepUp).toBeVisible();
  await stepUp.getByLabel('Пароль').fill('correct horse battery staple');
  await stepUp.getByRole('button', { name: 'Подтвердить' }).click();
  await expect(stepUp).toBeHidden();

  // CLI напечатал ссылку и код — показываем их как есть: оператор открывает ровно эту ссылку.
  await expect(section.getByRole('link', { name: LOGIN_URL })).toHaveAttribute('href', LOGIN_URL);
  await expect(section.getByText('WDJB-MJHT')).toBeVisible();
  await expect(section.locator('[data-login-state]')).toHaveAttribute('data-login-state', 'CHALLENGE');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `providers login challenge (${scheme})`);
  }
  // Провайдер подтвердил вход: состояние станет AUTHENTICATED без действий оператора.
  await expect(section.locator('[data-login-state]')).toHaveAttribute('data-login-state', 'AUTHENTICATED', { timeout: 15_000 });

  const requests = api.requests.filter((r) => r.operationId === 'loginProvider');
  expect(requests).toHaveLength(2); // первый — 403 step_up_required, повтор — тот же Idempotency-Key
  expect(requests[0]?.headers['idempotency-key']).toBe(requests[1]?.headers['idempotency-key']);
  expect(requests.every((r) => r.body === undefined)).toBe(true); // тело запроса пустое: передавать нечего
  expect(api.requests.filter((r) => r.operationId === 'getProviderLogin').every((r) => r.headers['x-workspace-id'] === ws.id)).toBe(true);
  // Список провайдеров перечитан: auth_status установки меняется после входа.
  expect(api.requests.filter((r) => r.operationId === 'listProviders').length).toBeGreaterThanOrEqual(2);
});

/**
 * Claude Code завершает вход вводом кода, который провайдер показал в браузере («Paste code here if
 * prompted >»). Поле появляется только по просьбе самого CLI (`awaiting_code`), код уходит в процесс CLI
 * на хосте исполнения и платформой не хранится (ТЗ §1): в UI это единственное поле ввода на экране.
 */
test('providers: operator pastes the authorization code the CLI asked for (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const items = providers();
  mockProviders(api, items);
  mockLogin(api, items, { awaitCode: true });
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/providers`);

  const section = page.locator('#provider-claude').getByTestId('provider-login-claude');
  await section.getByRole('button', { name: 'Войти' }).click();
  await expect(section.locator('[data-login-state]')).toHaveAttribute('data-login-state', 'CHALLENGE');

  // Поле появилось, потому что об этом попросил сам CLI; это не пароль и не ключ провайдера.
  const field = section.getByTestId('login-code-input');
  await expect(field).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `providers login code (${scheme})`);
  }
  await expectNoHorizontalScroll(page);

  await field.fill('  ABCD-1234-EFGH  ');
  await section.getByTestId('login-code-submit').click();

  const sent = api.requests.find((r) => r.operationId === 'submitProviderLoginCode');
  expect(sent?.body).toEqual({ code: 'ABCD-1234-EFGH' }); // пробелы по краям срезаются
  expect(sent?.headers['idempotency-key']).toBeTruthy();
  // CLI получил код и завершил вход; поле исчезает вместе с просьбой CLI.
  await expect(section.locator('[data-login-state]')).toHaveAttribute('data-login-state', 'AUTHENTICATED', { timeout: 15_000 });
  await expect(section.getByTestId('login-code-input')).toHaveCount(0);
});

test('providers: login can be cancelled, and a non-owner has no login control at all', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const items = providers();
  mockProviders(api, items);
  mockLogin(api, items);
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings/providers`);

  const section = page.locator('#provider-claude').getByTestId('provider-login-claude');
  await section.getByRole('button', { name: 'Войти' }).click();
  await expect(section.locator('[data-login-state]')).toHaveAttribute('data-login-state', 'CHALLENGE');
  await section.getByRole('button', { name: 'Отменить вход' }).click();
  await expect(section.locator('[data-login-state]')).toHaveAttribute('data-login-state', 'CANCELLED');
  await expect(section.getByRole('paragraph').filter({ hasText: 'Вход отменён' })).toBeVisible();
  const cancel = api.requests.find((r) => r.operationId === 'cancelProviderLogin');
  expect(cancel?.headers['idempotency-key']).toBeTruthy();
  await expectNoHorizontalScroll(page);

  // admin — не владелец подписки: раздела входа нет (provider:login — только owner, OQ-01/OQ-24).
  const other = api.addWorkspace('Команда Бета', 'beta', 'admin');
  await page.goto(`/w/${other.id}/settings/providers`);
  await expect(page.locator('#provider-claude')).toBeVisible();
  await expect(page.getByTestId('provider-login-claude')).toHaveCount(0);
});
