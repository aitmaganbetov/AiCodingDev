import type { Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { emptyAnalyticsSummary, problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

/**
 * F5 «Агенты» (план §3.4, §5.7; D-2): каталог 12 ролей AgentRole — профиль (B7 `GET /agent-profiles`),
 * активность только по выполняемым задачам (B1 `GET /tasks?status=LEASED&status=RUNNING`), результаты
 * `analytics.tasks.by_role`, провайдеры по настройкам workspace; вид сетка/список (URL + устройство);
 * карточка роли `agents/:role`. Ни одна роль не показывается «онлайн». Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */

const ROLES: S['AgentRole'][] = [
  'requirements',
  'architect',
  'planner',
  'developer',
  'database',
  'devops',
  'reviewer',
  'security',
  'qa',
  'performance',
  'maintenance',
  'documentation',
];
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

function profile(role: S['AgentRole']): S['AgentProfile'] {
  const writer = ['developer', 'database', 'devops', 'qa', 'documentation', 'maintenance', 'performance'].includes(role);
  return {
    role,
    template_version: '1.0.0',
    template_ref: `${role}@1.0.0`,
    template_sha256: `${'0123456789abcdef'.repeat(4)}`,
    permissions: {
      write_scope: writer ? 'worktree' : 'none',
      network: 'restricted',
      path_policy: writer ? ['src/**', 'tests/**'] : [],
      path_deny: ['.git/**', '.env*'],
      task_paths_allowed: writer,
      comments_only_paths: [],
    },
    prototype_permissions: null,
    allowed_tools: writer ? ['read', 'search', 'edit', 'test'] : ['read', 'search'],
    required_capabilities: ['json_output'],
    acp_send: role === 'reviewer' ? ['FINDING', 'FIX_REQUEST', 'TASK_COMPLETED'] : ['HANDOFF', 'QUESTION', 'TASK_COMPLETED'],
    complexity_band: { min: role === 'requirements' ? 'L3' : 'L1', max: 'L5' },
    independent_check_from: role === 'developer' ? 'L3' : null,
    must_differ_from_author: role === 'reviewer',
    can_send_fix_request: role === 'reviewer',
    // Роль «security» не разрешена ни одному провайдеру (allowed_roles) — карточка предупреждает.
    eligible_providers: role === 'security' ? [] : role === 'developer' ? ['codex', 'claude'] : ['codex'],
    catalog: role === 'documentation' ? null : { template_version: '1.0.0', enabled: true, updated_at: '2026-11-01T09:00:00Z' },
  };
}

const RUN_ID = '01900000-0000-7000-d000-000000000001';
const PROJECT_ID = '01900000-0000-7000-d000-0000000000a1';

function task(role: S['AgentRole'], title: string, status: S['TaskStatus'], n: number): S['WorkspaceTask'] {
  return {
    id: `01900000-0000-7000-c000-${String(n).padStart(12, '0')}`,
    run_id: RUN_ID,
    project_id: PROJECT_ID,
    agent_role: role,
    title,
    status,
    blocked_reason: null,
    complexity: 'L2',
    risk_level: 'low',
    current_provider: 'codex',
    attempt_count: 1,
    max_attempts: 3,
    depends_on: [],
    version: 1,
    updated_at: `2026-11-08T08:${String(n).padStart(2, '0')}:00Z`,
    run_status: 'RUNNING',
    created_at: '2026-11-08T07:00:00Z',
    current_attempt: {
      id: `01900000-0000-7000-e000-${String(n).padStart(12, '0')}`,
      attempt_no: 1,
      status: status === 'SUCCEEDED' ? 'SUCCEEDED' : 'RUNNING',
      provider: role === 'reviewer' ? 'claude' : 'codex',
      started_at: '2026-11-08T08:00:00Z',
      finished_at: status === 'SUCCEEDED' ? '2026-11-08T08:30:00Z' : null,
    },
  };
}

const TASKS: S['WorkspaceTask'][] = [
  task('developer', 'Экспорт отчётов в CSV', 'RUNNING', 1),
  task('developer', 'Тесты экспорта', 'LEASED', 2),
  task('reviewer', 'Ревью экспорта', 'RUNNING', 3),
  task('developer', 'Схема отчётов', 'SUCCEEDED', 4),
  task('qa', 'Тест-план экспорта', 'SUCCEEDED', 5),
];

function providers(): S['Provider'][] {
  const base = { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, settings_version: 1 };
  return [
    {
      provider_id: '01900000-0000-7000-b000-000000000001',
      code: 'codex',
      display_name: 'Codex CLI',
      enabled_globally: true,
      installations: [],
      workspace_settings: { ...base, allowed_roles: ROLES.filter((r) => r !== 'security') },
    },
    {
      provider_id: '01900000-0000-7000-b000-000000000002',
      code: 'claude',
      display_name: 'Claude Code CLI',
      enabled_globally: true,
      installations: [],
      workspace_settings: { ...base, allowed_roles: ['developer'] },
    },
  ];
}

function analytics(): S['AnalyticsSummary'] {
  const summary = emptyAnalyticsSummary();
  summary.tasks.by_role = [
    { role: 'developer', attempts: 12, succeeded: 10, failed: 2, retries: 2, verified_completion_rate: 0.8 },
    { role: 'reviewer', attempts: 6, succeeded: 6, failed: 0, retries: 0, verified_completion_rate: 1 },
    // Не роль агента (компонент платформы) — в каталог 12 ролей не попадает (D-2).
    { role: 'orchestrator', attempts: 3, succeeded: 3, failed: 0, retries: 0, verified_completion_rate: null },
  ];
  return summary;
}

function mockAgents(api: MockApi, { profilesFail = () => false }: { profilesFail?: () => boolean } = {}) {
  api.route('GET', '/api/v1/agent-profiles', 'listAgentProfiles', () =>
    profilesFail() ? problem(503, 'service_unavailable') : { status: 200, body: { items: ROLES.map(profile) } },
  );
  api.route('GET', '/api/v1/tasks', 'listTasks', ({ url }) => {
    const statuses = url.searchParams.getAll('status');
    const roles = url.searchParams.getAll('agent_role');
    const items = TASKS.filter((t) => (statuses.length === 0 || statuses.includes(t.status)) && (roles.length === 0 || roles.includes(t.agent_role)));
    return { status: 200, body: { items, next_cursor: null } };
  });
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', () => ({ status: 200, body: analytics() }));
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({ status: 200, body: { items: providers() } }));
}

test('agents: 12 role cards with real activity, zero-stat roles, grid/list persisted, role detail (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'admin');
  mockAgents(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Агенты', exact: true }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/agents`);
  await expect(page.getByRole('heading', { level: 1, name: 'Агенты' })).toBeFocused();
  await expect(page).toHaveTitle('Агенты — AiCodingDev');

  const catalog = page.getByRole('list', { name: 'Роли агентов' });
  await expect(catalog.getByRole('article')).toHaveCount(12);
  await expect(page.getByTestId('agents-active-summary')).toHaveText('Активны 2 роли из 12');
  const dev = catalog.getByRole('article', { name: 'Разработчик' });
  await expect(dev).toHaveAttribute('data-state', 'active');
  await expect(dev).toContainText('Активна · выполняет 2 задачи');
  await expect(dev).toContainText('Активна, когда выполняет задачу');
  await expect(dev.getByRole('link', { name: 'Экспорт отчётов в CSV' })).toHaveAttribute(
    'href',
    `/w/${ws.id}/mission-control?run=${RUN_ID}&task=01900000-0000-7000-c000-000000000001`,
  );
  await expect(dev.getByTestId('role-results')).toContainText('80%');
  await expect(dev).toContainText('Codex CLI');
  await expect(dev).toContainText('Claude Code CLI');
  // Роль без задач и без попыток в окне: «не активна» и «нет попыток», никаких выдуманных нулей.
  const qa = catalog.getByRole('article', { name: 'QA' });
  await expect(qa).toHaveAttribute('data-state', 'idle');
  await expect(qa).toContainText('Не активна · нет выполняемых задач');
  await expect(qa.getByTestId('role-no-attempts')).toHaveText('Нет попыток в окне analytics.');
  await expect(catalog.getByRole('article', { name: 'Безопасность' })).toContainText('Нет разрешённых провайдеров');
  await expect(catalog).not.toContainText(/онлайн|online/i);
  await expect(catalog).not.toContainText('Оркестратор');
  const tasksReq = api.requests.filter((r) => r.operationId === 'listTasks');
  expect(tasksReq.length).toBeGreaterThan(0);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `agents grid (${scheme})`);
  }

  // Вид «Список»: URL + выбор устройства (после перехода без параметра остаётся список).
  const listButton = page.getByRole('group', { name: 'Вид каталога ролей' }).getByRole('button', { name: 'Список' });
  await listButton.click();
  await expect(listButton).toHaveAttribute('aria-pressed', 'true');
  await expect(page).toHaveURL(`/w/${ws.id}/agents?view=list`);
  const table = page.getByRole('table', { name: /Роли агентов/ });
  await expect(table.getByRole('row')).toHaveCount(13);
  await expect(table.locator('tr[data-role="developer"]')).toContainText('Активна · выполняет 2 задачи');
  await expectNoSeriousA11yViolations(page, 'agents list');
  await page.goto(`/w/${ws.id}/agents`);
  await expect(page.getByRole('table', { name: /Роли агентов/ })).toBeVisible();
  await expect(page.getByRole('group', { name: 'Вид каталога ролей' }).getByRole('button', { name: 'Список' })).toHaveAttribute('aria-pressed', 'true');

  // Карточка роли: профиль B7, провайдеры по allowed_roles, история задач роли.
  await page.getByRole('table', { name: /Роли агентов/ }).getByRole('link', { name: /Ревьюер/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/agents/reviewer`);
  await expect(page.getByRole('heading', { level: 1, name: 'Агент: Ревьюер' })).toBeFocused();
  const status = page.getByRole('region', { name: 'Активность' });
  await expect(status).toContainText('Активна · выполняет 1 задачу');
  await expect(status.getByRole('link', { name: 'Ревью экспорта' })).toBeVisible();
  const profileRegion = page.getByRole('region', { name: 'Профиль роли' });
  await expect(profileRegion).toContainText('нет (только чтение)');
  await expect(profileRegion).toContainText('Запрос исправления');
  await expect(profileRegion.getByTestId('template-sha')).toHaveText('0123456789abcdef'.repeat(4));
  await expect(profileRegion).toContainText('reviewer@1.0.0');
  const prov = page.getByRole('region', { name: 'Провайдеры для роли' });
  await expect(prov.locator('[data-provider="codex"] [data-eligible]')).toHaveAttribute('data-eligible', 'true');
  await expect(prov.locator('[data-provider="claude"] [data-eligible]')).toHaveAttribute('data-eligible', 'false');
  // admin может менять allowed_roles — ссылка ведёт в Настройки › CLI-провайдеры.
  await expect(prov.getByRole('link', { name: 'Изменить разрешённые роли в настройках CLI-провайдеров' })).toHaveAttribute('href', `/w/${ws.id}/settings/providers`);
  const results = page.getByRole('region', { name: 'Результаты' });
  await expect(results.getByTestId('role-verified')).toContainText('100%');
  const history = page.getByRole('table', { name: 'Задачи роли Ревьюер' });
  await expect(history.getByRole('row')).toHaveCount(2);
  await expect(history.getByRole('link', { name: /^Задача\s*:\s*Ревью экспорта$/ })).toHaveAttribute(
    'href',
    `/w/${ws.id}/projects/${PROJECT_ID}/runs/${RUN_ID}/tasks/01900000-0000-7000-c000-000000000003`,
  );
  const historyReq = api.requests.filter((r) => r.operationId === 'listTasks').at(-1);
  expect(historyReq?.headers['x-workspace-id']).toBe(ws.id);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `agent role (${scheme})`);
  }
  await page.getByRole('link', { name: 'Все агенты' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/agents`);
});

test('agents: viewer (RBAC) sees provider settings read-only link; profiles error with retry; unknown role', async ({ page, api }) => {
  const ws = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
  let fail = true;
  mockAgents(api, { profilesFail: () => fail });
  await login(page, api);
  await page.goto(`/w/${ws.id}/agents`);
  const errors = page.getByTestId('agents-errors');
  await expect(errors).toContainText('Не удалось загрузить профили ролей');
  // Активность и результаты не зависят от профилей.
  await expect(page.getByRole('list', { name: 'Роли агентов' }).getByRole('article', { name: 'Разработчик' })).toContainText('Активна · выполняет 2 задачи');
  await expect(page.getByRole('list', { name: 'Роли агентов' }).getByRole('article', { name: 'Разработчик' })).toContainText('Профиль не загружен');
  await expectNoSeriousA11yViolations(page, 'agents profiles error');
  fail = false;
  await errors.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('agents-errors')).toHaveCount(0);
  await expect(page.getByRole('list', { name: 'Роли агентов' }).getByRole('article', { name: 'Разработчик' })).toContainText('v1.0.0');

  await page.goto(`/w/${ws.id}/agents/developer`);
  await expect(page.getByRole('heading', { level: 1, name: 'Агент: Разработчик' })).toBeFocused();
  const prov = page.getByRole('region', { name: 'Провайдеры для роли' });
  await expect(prov.getByRole('link', { name: 'Настройки CLI-провайдеров' })).toBeVisible();
  await expect(prov.getByRole('link', { name: /Изменить/ })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page, 'agent role (viewer)');

  await page.goto(`/w/${ws.id}/agents/orchestrator`);
  await expect(page.getByTestId('empty-state')).toContainText('Роль «orchestrator» не входит в 12 ролей AgentRole.');
});

test('agents: 375/768/1366/1920 without horizontal overflow in both themes (grid, list, role), reduced motion', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  mockAgents(api);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await login(page, api);
  for (const [path, name] of [
    [`/w/${ws.id}/agents?view=grid`, 'agents-grid'],
    [`/w/${ws.id}/agents?view=list`, 'agents-list'],
    [`/w/${ws.id}/agents/developer`, 'agent-developer'],
  ] as const) {
    await page.setViewportSize({ width: 1366, height: 900 });
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.locator('[aria-busy="true"]')).toHaveCount(0);
    for (const width of [375, 768, 1366, 1920]) {
      await page.setViewportSize({ width, height: 900 });
      for (const scheme of ['dark', 'light'] as const) {
        await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' });
        await expectNoHorizontalScroll(page);
        if (width === 1366 || width === 375) await shot(page, `${name}-${scheme}-${width}`);
      }
    }
  }
  // reduced motion: карточки не анимируют сдвиг.
  const duration = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--dur-instant').trim());
  expect(['0ms', '0s', '0']).toContain(duration);
});
