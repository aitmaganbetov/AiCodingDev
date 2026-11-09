/**
 * F6: Projects — список (карточки, фильтр) и рабочее пространство проекта: вкладки (обзор: режимы, AI-исполнители,
 * политика качества, последние запуски; репозитории с привязкой GitHub 202 → проверка воркером; файлы; ветки;
 * worktrees B4 без путей хоста; запуски → Mission Control / Code Review). axe в обеих темах, клавиатура,
 * нет горизонтального переполнения 375/768/1366/1920. Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { Page } from '@playwright/test';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];
const AT = '2026-11-08T10:00:00Z';
const REPO_ID = '01900000-0000-7000-e100-000000000001';
const GH_REPO_ID = '01900000-0000-7000-e100-000000000002';
const VER = '01900000-0000-7000-e100-0000000000f1';
const HEAD = 'b'.repeat(40);
const BASE = 'c'.repeat(40);
const FEATURE = 'e'.repeat(40);
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

const LOCAL: (projectId: string) => S['Repository'] = (projectId) => ({
  id: REPO_ID,
  project_id: projectId,
  provider: 'local',
  remote_url: null,
  default_branch: 'main',
  local_root_ref: 'repos/payments',
  last_synced_at: AT,
  latest_index_snapshot: { snapshot_id: '01900000-0000-7000-e100-000000000003', commit_sha: HEAD, status: 'READY' },
});

function provider(code: S['CliProvider'], enabled: boolean, roles: S['AgentRole'][]): S['Provider'] {
  return {
    provider_id: code === 'codex' ? '01900000-0000-7000-e200-000000000001' : '01900000-0000-7000-e200-000000000002',
    code,
    display_name: code === 'codex' ? 'Codex CLI' : 'Claude Code CLI',
    enabled_globally: true,
    installations: [],
    workspace_settings: { enabled, max_parallel_sessions: 4, default_timeout_seconds: 1800, allowed_roles: roles, settings_version: 1 },
  };
}

function seed(api: MockApi, role: S['WorkspaceRole'] = 'owner') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  api.addProject(ws.id, 'Биллинг', 'Счета и тарифы');
  const project = api.addProject(ws.id, 'Платёжный шлюз', 'Сервис платежей <b>не HTML</b>');
  project.settings.security_profile = 'pci-dss';
  const run = api.addRun(project, { status: 'RUNNING', objective: 'Добавить экспорт отчётов в CSV' });
  const repos: S['Repository'][] = [LOCAL(project.id)];
  const worktree = (id: string, status: S['WorktreeStatus'], task: string | null): S['Worktree'] => ({
    id,
    repository_id: REPO_ID,
    run_id: run.id,
    task_id: task,
    branch_name: `aicd/${run.id.slice(-4)}/${id.slice(-2)}`,
    base_commit_sha: BASE,
    head_commit_sha: status === 'REMOVED' ? null : HEAD,
    status,
    created_at: AT,
    updated_at: AT,
    removed_at: status === 'REMOVED' ? AT : null,
  });
  const worktrees = [
    worktree('01900000-0000-7000-e300-000000000001', 'ACTIVE', '01900000-0000-7000-e300-0000000000a1'),
    worktree('01900000-0000-7000-e300-000000000002', 'REMOVED', null),
  ];
  api.route('GET', '/api/v1/providers', 'listProviders', () => ({
    status: 200,
    body: { items: [provider('codex', true, ['developer', 'reviewer']), provider('claude', false, [])] },
  }));
  api.route('GET', '/api/v1/projects/{id}/repositories', 'listRepositories', ({ id }) =>
    id === project.id ? { status: 200, body: { items: repos, next_cursor: null } } : problem(404, 'not_found'),
  );
  api.route('GET', '/api/v1/projects/{id}/worktrees', 'listProjectWorktrees', ({ id, url }) => {
    if (id !== project.id) return problem(404, 'not_found');
    const statuses = url.searchParams.getAll('status');
    return { status: 200, body: { items: worktrees.filter((w) => statuses.length === 0 || statuses.includes(w.status)), next_cursor: null } };
  });
  const base = `/api/v1/projects/{id}/repositories/${REPO_ID}`;
  api.route('GET', `${base}/branches`, 'listRepositoryBranches', () => ({
    status: 200,
    body: {
      repository_id: REPO_ID,
      default_branch: 'main',
      truncated: false,
      items: [
        { name: 'main', commit_sha: HEAD, committed_at: AT, is_default: true },
        { name: 'feature/export', commit_sha: FEATURE, committed_at: AT, is_default: false },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', `${base}/commits`, 'listRepositoryCommits', ({ url }) => {
    const sha = url.searchParams.get('commit') === 'feature/export' ? FEATURE : HEAD;
    return {
      status: 200,
      body: { repository_id: REPO_ID, commit_sha: sha, items: [{ sha, parents: [], author_name: 'Анна', authored_at: AT, committed_at: AT, subject: 'Экспорт' }], next_cursor: null },
    };
  });
  api.route('GET', `${base}/tree`, 'listRepositoryTree', ({ url }) => {
    const sha = url.searchParams.get('commit') === 'feature/export' ? FEATURE : HEAD;
    const items: S['RepositoryTreeEntry'][] =
      sha === FEATURE
        ? [{ name: 'export.ts', path: 'export.ts', type: 'file', mode: '100644', size: 12 }]
        : [{ name: 'README.md', path: 'README.md', type: 'file', mode: '100644', size: 9 }];
    return { status: 200, body: { repository_id: REPO_ID, commit_sha: sha, path: '', items, next_cursor: null } };
  });
  return { ws, project, run, repos, worktrees };
}

test('projects list and project workspace: overview (modes, executors, quality policy, runs), keyboard tabs, axe light/dark', async ({ page, api }) => {
  const { ws, project, run } = seed(api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects`);
  const list = page.getByRole('list', { name: 'Список проектов' });
  await expect(list.getByRole('listitem')).toHaveCount(2);
  await page.getByLabel('Поиск по названию').fill('шлюз');
  await expect(list.getByRole('listitem')).toHaveCount(1);
  await expect(list).toContainText('Сервис платежей <b>не HTML</b>');
  await expect(list.locator('b')).toHaveCount(0);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `projects list (${scheme})`);
  }
  await shot(page, 'projects-list');

  await list.getByRole('link', { name: 'Открыть проект «Платёжный шлюз»: репозитории и стек' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Платёжный шлюз' })).toBeFocused();
  await expect(page.getByTestId('project-badges')).toContainText('Профиль качества: Строгий');
  await expect(page.getByTestId('overview-quality-profile')).toContainText('Расширенные проверки');
  await expect(page.locator('#project-quality')).toContainText('pci-dss');
  const executors = page.getByTestId('overview-executors');
  await expect(executors.getByRole('listitem')).toHaveCount(2);
  await expect(executors.getByRole('listitem').first()).toContainText('Включён');
  await expect(executors.getByRole('listitem').first()).toContainText('Роли: Разработчик, Ревьюер');
  await expect(executors.getByRole('listitem').last()).toContainText('Роли не назначены');
  await expect(page.getByRole('link', { name: 'Настройки CLI-провайдеров' })).toHaveAttribute('href', `/w/${ws.id}/settings/providers`);
  const recent = page.getByRole('table', { name: 'Последние запуски' });
  await expect(recent.getByRole('link', { name: /^Mission Control/ })).toHaveAttribute('href', `/w/${ws.id}/mission-control?run=${run.id}`);
  await expect(recent.getByRole('link', { name: /^Ревью кода/ })).toHaveAttribute('href', `/w/${ws.id}/projects/${project.id}/review/${run.id}`);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `project overview (${scheme})`);
  }
  await shot(page, 'project-overview');

  // Вкладки — только клавиатурой (APG Tabs, автоматическая активация); вкладка — в URL.
  await page.getByRole('tab', { name: 'Обзор' }).focus();
  for (const [name, param] of [
    ['Репозитории', 'repositories'],
    ['Файлы и изменения', 'files'],
    ['Ветки', 'branches'],
    ['Worktrees', 'worktrees'],
    ['Запуски', 'runs'],
  ] as const) {
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('tab', { name, exact: true })).toHaveAttribute('aria-selected', 'true');
    await expect(page).toHaveURL(new RegExp(`\\?tab=${param}$`));
  }
  const runs = page.getByRole('table', { name: 'История запусков' });
  await expect(runs.getByRole('row')).toHaveCount(2);
  await expect(runs).toContainText('Добавить экспорт отчётов в CSV');
  await expectNoSeriousA11yViolations(page, 'project runs tab');
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Запуски', exact: true })).toHaveAttribute('aria-selected', 'true');
});

test('attach a GitHub repository: 202 → worker verification PENDING → SUCCEEDED → repository listed', async ({ page, api }) => {
  const { ws, project, repos } = seed(api);
  let checks = 0;
  const verification = (status: S['GitHubVerification']['status']): S['GitHubVerification'] => ({
    id: VER,
    kind: 'repository',
    status,
    project_id: project.id,
    result_id: status === 'SUCCEEDED' ? GH_REPO_ID : null,
    error: null,
    created_at: AT,
    updated_at: AT,
    completed_at: status === 'PENDING' ? null : AT,
  });
  api.route('POST', '/api/v1/projects/{id}/repositories', 'attachRepository', ({ body }) => {
    const b = body as S['RepositoryAttachRequest'];
    expect(b).toEqual({ provider: 'github', remote_url: 'https://github.com/acme/payments', local_path: '/srv/repos/payments-gh' });
    return { status: 202, body: verification('PENDING') };
  });
  api.route('GET', '/api/v1/integrations/github/verifications/{id}', 'getGitHubVerification', () => {
    checks += 1;
    if (checks < 2) return { status: 200, body: verification('PENDING') };
    if (!repos.some((r) => r.id === GH_REPO_ID)) {
      repos.push({
        id: GH_REPO_ID,
        project_id: project.id,
        provider: 'github',
        remote_url: 'https://github.com/acme/payments',
        default_branch: 'main',
        local_root_ref: 'repos/payments-gh',
        last_synced_at: null,
        latest_index_snapshot: null,
      });
    }
    return { status: 200, body: verification('SUCCEEDED') };
  });
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}?tab=repositories`);
  const listEl = page.getByRole('list', { name: 'Репозитории проекта' });
  await expect(listEl.getByRole('listitem')).toHaveCount(1);
  await page.getByRole('button', { name: 'Привязать репозиторий' }).click();
  const dialog = page.getByRole('dialog', { name: 'Привязка репозитория' });
  await dialog.getByRole('radio', { name: 'GitHub' }).check();
  await dialog.getByLabel('Адрес репозитория GitHub').fill('https://github.com/acme/payments');
  await dialog.getByLabel('Путь к каталогу').fill('/srv/repos/payments-gh');
  await expectNoSeriousA11yViolations(page, 'attach GitHub dialog');
  await dialog.getByRole('button', { name: 'Привязать' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: 'Проверка доступа к репозиторию GitHub запущена' })).toBeVisible();
  await expect(page.locator('[data-status="SUCCEEDED"]')).toHaveText('Доступ к репозиторию подтверждён, репозиторий привязан.');
  await expect(listEl.getByRole('listitem')).toHaveCount(2);
  await expect(listEl).toContainText('https://github.com/acme/payments');
  expect(checks).toBeGreaterThanOrEqual(2);
  const attach = api.requests.find((r) => r.operationId === 'attachRepository');
  expect(attach?.headers['idempotency-key']).toBeTruthy();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `repositories after GitHub verification (${scheme})`);
  }
});

test('worktrees (B4, no host paths), branches → files of a branch, status filter; viewer has no attach', async ({ page, api }) => {
  const { ws, project, run, worktrees } = seed(api, 'viewer');
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}?tab=worktrees`);
  const table = page.getByRole('table', { name: 'Worktrees' });
  await expect(table.getByRole('row')).toHaveCount(3);
  await expect(table).toContainText(worktrees[0]!.branch_name);
  await expect(table).toContainText('Активен');
  await expect(table).toContainText('Удалён');
  await expect(table).toContainText('cccccccccccc');
  await expect(page.getByRole('main')).not.toContainText('/srv/');
  const first = table.getByRole('row').nth(1);
  await expect(first.getByRole('link', { name: /^Ревью кода/ })).toHaveAttribute(
    'href',
    `/w/${ws.id}/projects/${project.id}/review/${run.id}?task=${worktrees[0]!.task_id ?? ''}`,
  );
  await expect(first.getByRole('link', { name: /^Задача/ })).toHaveAttribute('href', `/w/${ws.id}/projects/${project.id}/runs/${run.id}/tasks/${worktrees[0]!.task_id ?? ''}`);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `worktrees (${scheme})`);
  }
  await shot(page, 'project-worktrees');
  await page.getByLabel('Статус').selectOption('REMOVED');
  await expect(table.getByRole('row')).toHaveCount(2);
  expect(api.requests.some((r) => r.operationId === 'listProjectWorktrees')).toBe(true);

  await page.getByRole('tab', { name: 'Ветки' }).click();
  const branches = page.getByRole('table', { name: 'Ветки repos/payments' });
  await expect(branches.getByRole('row')).toHaveCount(3);
  await expect(branches).toContainText('по умолчанию');
  await branches.getByRole('button', { name: /Открыть файлы\W+feature\/export/ }).click();
  await expect(page).toHaveURL(/tab=files/);
  await expect(page.getByTestId('explorer-branch')).toHaveValue('feature/export');
  await expect(page.getByTestId('explorer-resolved-commit')).toContainText('eeeeeeeeeeee');
  await expect(page.getByRole('list', { name: 'Содержимое каталога /' })).toContainText('export.ts');
  await expectNoSeriousA11yViolations(page, 'project files tab');

  await page.getByRole('tab', { name: 'Репозитории' }).click();
  await expect(page.getByRole('button', { name: 'Привязать репозиторий' })).toHaveCount(0);
});

for (const width of [375, 768, 1366, 1920]) {
  test(`project workspace: no horizontal overflow at ${width}px (both themes)`, async ({ page, api }) => {
    const { ws, project } = seed(api);
    await page.setViewportSize({ width, height: 900 });
    await login(page, api);
    for (const tab of ['', '?tab=worktrees', '?tab=branches']) {
      await page.goto(`/w/${ws.id}/projects/${project.id}${tab}`);
      await expect(page.getByRole('heading', { level: 1, name: 'Платёжный шлюз' })).toBeVisible();
      if (tab === '') await expect(page.getByTestId('overview-executors')).toBeVisible();
      else await expect(page.getByRole('table').first()).toBeVisible();
      for (const scheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoHorizontalScroll(page);
      }
    }
    await page.goto(`/w/${ws.id}/projects`);
    await expect(page.getByRole('list', { name: 'Список проектов' })).toBeVisible();
    await expectNoHorizontalScroll(page);
    if (width === 375) await shot(page, 'projects-list-375');
  });
}
