import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

/** P7-04 Projects + Repository Explorer: репозитории проекта, индекс, стек; привязка репозитория. */
type S = components['schemas'];

function mockExplorer(api: MockApi, projectId: string, repos: S['Repository'][], techs: S['Technology'][]) {
  api.route('GET', '/api/v1/projects/{id}/repositories', 'listRepositories', ({ id }) =>
    id === projectId ? { status: 200, body: { items: repos, next_cursor: null } } : problem(404, 'not_found'),
  );
  api.route('POST', '/api/v1/projects/{id}/repositories', 'attachRepository', ({ id, body }) => {
    if (id !== projectId) return problem(404, 'not_found');
    const b = body as S['RepositoryAttachRequest'];
    if (b.provider === 'local' && b.local_path?.startsWith('/etc')) {
      return problem(422, 'validation_error', { errors: [{ pointer: '/local_path', code: 'path_not_allowed', message: 'Путь вне разрешённых корней.' }] });
    }
    const repo: S['Repository'] = {
      id: '01900000-0000-7000-a100-000000000009',
      project_id: projectId,
      provider: b.provider,
      remote_url: b.remote_url ?? null,
      local_root_ref: b.local_path ? 'repos/billing' : null,
      default_branch: b.default_branch ?? 'main',
      last_synced_at: null,
      latest_index_snapshot: null,
    };
    repos.push(repo);
    return { status: 201, body: repo, headers: { ETag: '"v0"', Location: `/api/v1/projects/${projectId}/repositories/${repo.id}` } };
  });
  api.route('GET', '/api/v1/projects/{id}/technologies', 'listTechnologies', ({ id }) =>
    id === projectId ? { status: 200, body: { items: techs, next_cursor: null } } : problem(404, 'not_found'),
  );
}

const REPO = (projectId: string): S['Repository'] => ({
  id: '01900000-0000-7000-a100-000000000001',
  project_id: projectId,
  provider: 'github',
  remote_url: 'https://github.com/acme/payments',
  default_branch: 'main',
  local_root_ref: null,
  last_synced_at: '2026-11-07T10:00:00Z',
  latest_index_snapshot: { snapshot_id: '01900000-0000-7000-a100-000000000002', commit_sha: '3f2a9c1d'.padEnd(40, '0'), status: 'READY' },
});

const TECH: S['Technology'] = {
  technology_id: '01900000-0000-7000-a200-000000000001',
  name: 'Python',
  ecosystem: 'pypi',
  technology_version_id: '01900000-0000-7000-a200-000000000002',
  version_label: '3.12',
  support_status: 'supported',
  detected_from: 'pyproject.toml',
  confirmed_by: null,
};

test('repository explorer: project overview, repository with index snapshot, no local copy notice, technologies tab; attach (keyboard)', async ({
  page,
  api,
}) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const project = api.addProject(ws.id, 'Платёжный шлюз', 'Сервис платежей');
  mockExplorer(api, project.id, [REPO(project.id)], [TECH]);
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects`);
  await page.getByRole('link', { name: 'Открыть проект «Платёжный шлюз»: репозитории и стек' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/projects/${project.id}`);
  await expect(page.getByRole('heading', { level: 1, name: 'Платёжный шлюз' })).toBeFocused();
  await expect(page.getByText('Сервис платежей')).toBeVisible();

  const repos = page.getByRole('list', { name: 'Репозитории проекта' });
  await expect(repos.getByRole('listitem')).toHaveCount(1);
  await expect(repos).toContainText('https://github.com/acme/payments');
  await expect(repos).toContainText('3f2a9c1d0000');
  await expect(repos).toContainText('Просмотр файлов недоступен');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `project repositories (${scheme})`);
  }

  // Вкладки — стрелками (APG Tabs).
  await page.getByRole('tab', { name: 'Репозитории' }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Технологии' })).toHaveAttribute('aria-selected', 'true');
  const techTable = page.getByRole('table', { name: /Технологии/ });
  await expect(techTable.getByRole('row')).toHaveCount(2);
  await expect(techTable).toContainText('pyproject.toml');
  await expectNoSeriousA11yViolations(page, 'project technologies');

  // Привязка локального репозитория с клавиатуры, ошибка сервера по полю, затем успех.
  await page.keyboard.press('ArrowLeft');
  const attach = page.getByRole('button', { name: 'Привязать репозиторий' });
  await tabTo(page, attach, { backwards: true });
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Привязка репозитория' });
  await expect(dialog).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'attach repository dialog');
  await dialog.getByLabel('Путь к каталогу').fill('/etc/secret');
  await dialog.getByRole('button', { name: 'Привязать' }).click();
  await expect(dialog.getByLabel('Путь к каталогу')).toHaveAttribute('aria-invalid', 'true');
  await expect(dialog.getByText('Путь вне разрешённых корней.')).toBeVisible();
  await dialog.getByLabel('Путь к каталогу').fill('/srv/repos/billing');
  await dialog.getByLabel('Основная ветка').fill('develop');
  await dialog.getByRole('button', { name: 'Привязать' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('status').filter({ hasText: 'Репозиторий привязан' })).toBeVisible();
  await expect(repos.getByRole('listitem')).toHaveCount(2);
  const attachRequests = api.requests.filter((r) => r.operationId === 'attachRepository');
  expect(attachRequests.at(-1)?.body).toEqual({ provider: 'local', local_path: '/srv/repos/billing', default_branch: 'develop' });
  expect(attachRequests.at(-1)?.headers['idempotency-key']).toBeTruthy();
});

test('repository explorer: viewer is read-only, empty states, unknown project; 360px', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ro = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
  const project = api.addProject(ro.id, 'Сервис');
  mockExplorer(api, project.id, [], []);
  await login(page, api);
  await page.goto(`/w/${ro.id}/projects/${project.id}`);
  await expect(page.getByRole('heading', { level: 1, name: 'Сервис' })).toBeFocused();
  await expect(page.getByTestId('empty-state')).toContainText('Привязывать репозитории могут владелец и администраторы');
  await expect(page.getByRole('button', { name: 'Привязать репозиторий' })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'project empty (viewer, 360px)');
  await page.getByRole('tab', { name: 'Технологии' }).click();
  await expect(page.getByText('Стек ещё не определён')).toBeVisible();

  await page.goto(`/w/${ro.id}/projects/01900000-0000-7000-a100-0000000000ff`);
  await expect(page.getByText('Проект не найден', { exact: true })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'project not found');
});

const LOCAL_REPO_ID = '01900000-0000-7000-a100-000000000031';
const HEAD_SHA = 'b'.repeat(40);
const INDEX_SHA = 'c'.repeat(40);

function treeEntry(path: string, type: S['RepositoryTreeEntry']['type'], size: number | null): S['RepositoryTreeEntry'] {
  return { name: path.split('/').at(-1)!, path, type, mode: type === 'directory' ? '040000' : type === 'symlink' ? '120000' : '100644', size };
}

/** Файловое дерево двух commit: HEAD ветки (README с HTML-разметкой, большой и бинарный файлы) и снимок индекса. */
function mockRepositoryFiles(api: MockApi, projectId: string) {
  const trees: Record<string, Record<string, S['RepositoryTreeEntry'][]>> = {
    [HEAD_SHA]: {
      '': [
        treeEntry('src', 'directory', null),
        treeEntry('README.md', 'file', 52),
        treeEntry('big.log', 'file', 2_000_000),
        treeEntry('logo.png', 'file', 4096),
        treeEntry('vendor-lib', 'submodule', null),
      ],
      src: [treeEntry('src/app.py', 'file', 24)],
    },
    [INDEX_SHA]: { '': [treeEntry('README.md', 'file', 3)] },
  };
  const files: Record<string, Omit<S['RepositoryFile'], 'repository_id' | 'commit_sha' | 'path' | 'mode'>> = {
    'README.md': { type: 'file', size: 52, max_bytes: 524288, truncated: false, is_binary: false, encoding: 'utf-8', content: '# Платежи\n<b>не HTML</b>\n<script>alert(1)</script>\n' },
    'src/app.py': { type: 'file', size: 24, max_bytes: 524288, truncated: false, is_binary: false, encoding: 'utf-8', content: 'print("hi")\nx = 1\n' },
    'big.log': { type: 'file', size: 2_000_000, max_bytes: 524288, truncated: true, is_binary: false, encoding: 'utf-8', content: 'line\n'.repeat(50) },
    'logo.png': { type: 'file', size: 4096, max_bytes: 524288, truncated: false, is_binary: true, encoding: null, content: null },
  };
  const base = `/api/v1/projects/{id}/repositories/${LOCAL_REPO_ID}`;
  const resolve = (commit: string | null) => (commit === null || commit === 'main' ? HEAD_SHA : commit.length >= 7 && INDEX_SHA.startsWith(commit) ? INDEX_SHA : commit === HEAD_SHA ? HEAD_SHA : null);
  api.route('GET', `${base}/tree`, 'listRepositoryTree', ({ id, url }) => {
    if (id !== projectId) return problem(404, 'not_found');
    const sha = resolve(url.searchParams.get('commit'));
    if (!sha) return problem(404, 'not_found', { detail: 'Commit не найден в репозитории' });
    const path = url.searchParams.get('path') ?? '';
    const items = trees[sha]?.[path];
    if (!items) return problem(404, 'not_found');
    return { status: 200, body: { repository_id: LOCAL_REPO_ID, commit_sha: sha, path, items, next_cursor: null } };
  });
  api.route('GET', `${base}/file`, 'getRepositoryFile', ({ id, url }) => {
    if (id !== projectId) return problem(404, 'not_found');
    const sha = resolve(url.searchParams.get('commit'));
    const path = url.searchParams.get('path') ?? '';
    const f = files[path];
    if (!sha || !f) return problem(404, 'not_found');
    return { status: 200, body: { repository_id: LOCAL_REPO_ID, commit_sha: sha, path, mode: '100644', ...f } };
  });
}

test('repository explorer: file tree by commit, text view with line numbers, truncated/binary notices (keyboard, axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'developer');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const local: S['Repository'] = {
    id: LOCAL_REPO_ID,
    project_id: project.id,
    provider: 'local',
    remote_url: null,
    default_branch: 'main',
    local_root_ref: 'repos/payments',
    last_synced_at: '2026-11-07T10:00:00Z',
    latest_index_snapshot: { snapshot_id: '01900000-0000-7000-a100-000000000032', commit_sha: INDEX_SHA, status: 'READY' },
  };
  mockExplorer(api, project.id, [local], []);
  mockRepositoryFiles(api, project.id);
  await login(page, api);
  await page.goto(`/w/${ws.id}/projects/${project.id}`);

  const toggle = page.getByRole('button', { name: 'Открыть дерево файлов' });
  await toggle.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'Скрыть дерево файлов' })).toHaveAttribute('aria-expanded', 'true');
  const explorer = page.getByTestId('repository-explorer');
  const tree = explorer.getByRole('list', { name: 'Содержимое каталога /' });
  await expect(tree.getByRole('listitem')).toHaveCount(5);
  await expect(explorer.getByTestId('explorer-resolved-commit')).toContainText('bbbbbbbbbbbb');
  const treeReq = api.requests.filter((r) => r.operationId === 'listRepositoryTree');
  expect(treeReq.length).toBeGreaterThan(0);
  // Подмодуль не открывается.
  await expect(tree.locator('[data-entry-type="submodule"]').getByRole('button')).toHaveCount(0);

  // Файл — только текст с номерами строк: разметка из репозитория не исполняется.
  await tree.getByRole('button', { name: /README\.md/ }).click();
  const file = explorer.getByTestId('repository-file');
  await expect(file.getByRole('heading', { name: 'README.md' })).toBeVisible();
  const content = file.getByRole('region', { name: 'Содержимое файла README.md' });
  await expect(content).toContainText('<b>не HTML</b>');
  await expect(content.locator('b, script')).toHaveCount(0);
  await expect(file.getByTestId('line-numbers')).toHaveText('1\n2\n3');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `repository explorer file (${scheme})`);
  }

  await tree.getByRole('button', { name: /big\.log/ }).click();
  await expect(file.getByText('Файл показан не полностью')).toBeVisible();
  await expect(file).toContainText('Показаны первые 512 KB из 1,9 MB.');
  await tree.getByRole('button', { name: /logo\.png/ }).click();
  await expect(file.getByText('Бинарный файл')).toBeVisible();
  await expect(file.getByTestId('repository-file-content')).toHaveCount(0);

  // Навигация по каталогам: вглубь и обратно по «хлебным крошкам».
  await tree.getByRole('button', { name: /^src\W+каталог/ }).click();
  const sub = explorer.getByRole('list', { name: 'Содержимое каталога src' });
  await expect(sub.getByRole('listitem')).toHaveCount(1);
  await sub.getByRole('button', { name: /app\.py/ }).click();
  await expect(file.getByTestId('line-numbers')).toHaveText('1\n2');
  const fileReq = api.requests.filter((r) => r.operationId === 'getRepositoryFile');
  expect(fileReq.length).toBe(4);
  const crumbs = explorer.getByRole('navigation', { name: 'Путь в репозитории' });
  await expect(crumbs.locator('[aria-current="location"]')).toHaveText('src');
  await crumbs.getByRole('button', { name: 'Корень' }).click();
  await expect(explorer.getByRole('list', { name: 'Содержимое каталога /' })).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'repository explorer tree');

  // Выбор commit: снимок индекса, затем ввод произвольного commit (неверный формат — ошибка поля).
  await explorer.getByLabel('Commit', { exact: true }).selectOption({ label: 'Снимок индекса cccccccccccc' });
  await expect(explorer.getByTestId('explorer-resolved-commit')).toContainText('cccccccccccc');
  await expect(explorer.getByRole('list', { name: 'Содержимое каталога /' }).getByRole('listitem')).toHaveCount(1);
  await explorer.getByLabel('Commit', { exact: true }).selectOption({ label: 'Другой commit или ветка' });
  await expect(explorer.getByText('Укажите commit, чтобы открыть дерево файлов.')).toBeVisible();
  const input = explorer.getByLabel('SHA commit или имя ветки');
  await input.fill('HEAD~1 ; rm');
  await explorer.getByRole('button', { name: 'Показать' }).click();
  await expect(input).toHaveAttribute('aria-invalid', 'true');
  await input.fill('ccccccc');
  await page.keyboard.press('Enter');
  await expect(explorer.getByTestId('explorer-resolved-commit')).toContainText('cccccccccccc');
  await input.fill('0123456789');
  await page.keyboard.press('Enter');
  await expect(explorer.getByTestId('error-state')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'repository explorer commit selector');
});
