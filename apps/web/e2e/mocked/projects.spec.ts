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

test('repository explorer: project overview, repository with index snapshot, gap notice, technologies tab; attach (keyboard)', async ({
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
  await expect(repos).toContainText('Дерево файлов недоступно');
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
