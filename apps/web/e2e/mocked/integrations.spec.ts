import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/**
 * Интеграции → GitHub App (P8-02, OQ-47 (2)): установка через GitHub (state + Setup URL), подключение (201 PENDING),
 * проверка воркером (getGitHubVerification), отключение; pull requests проекта и запуска (P8-04, P8-05).
 */
type S = components['schemas'];
const AT = '2026-11-08T10:00:00Z';
const INST = '01900000-0000-7000-b000-000000000001';
const VER = '01900000-0000-7000-b000-0000000000f1';

function mockGitHub(api: MockApi, opts: { installations: S['GitHubInstallation'][]; failVerification?: boolean }) {
  let checks = 0;
  const verification = (status: S['GitHubVerification']['status']): S['GitHubVerification'] => ({
    id: VER,
    kind: 'installation',
    status,
    project_id: null,
    result_id: status === 'SUCCEEDED' ? INST : null,
    error: status === 'FAILED' ? { code: 'permissions', detail: 'workflows:write' } : null,
    created_at: AT,
    updated_at: AT,
    completed_at: status === 'PENDING' ? null : AT,
  });
  api.route('GET', '/api/v1/integrations/github/installations', 'listGitHubInstallations', () => ({
    status: 200,
    body: { items: opts.installations },
  }));
  api.route('POST', '/api/v1/integrations/github/installations/setup', 'startGitHubInstallation', () => ({
    status: 200,
    body: { install_url: 'https://github.test/apps/aicd/installations/new?state=st-e2e', state: 'st-e2e', expires_at: AT },
  }));
  api.route('POST', '/api/v1/integrations/github/installations', 'connectGitHubInstallation', ({ body }) => {
    const b = body as S['GitHubInstallationConnectRequest'];
    if (b.state !== 'st-e2e') return problem(422, 'validation_error', { errors: [{ pointer: '/state', code: 'invalid_state' }] });
    const inst: S['GitHubInstallation'] = {
      id: INST,
      installation_id: b.installation_id,
      account_login: 'pending',
      account_type: 'organization',
      repository_selection: 'selected',
      permissions: {},
      status: 'PENDING',
      created_at: AT,
      updated_at: AT,
    };
    opts.installations.splice(0, opts.installations.length, inst);
    return { status: 201, body: { ...inst, verification_id: VER } };
  });
  api.route('GET', '/api/v1/integrations/github/verifications/{id}', 'getGitHubVerification', () => {
    checks += 1;
    if (checks < 2) return { status: 200, body: verification('PENDING') };
    const inst = opts.installations[0];
    if (inst && inst.status === 'PENDING') {
      Object.assign(
        inst,
        opts.failVerification
          ? { status: 'REVOKED' }
          : { status: 'ACTIVE', account_login: 'acme', permissions: { contents: 'write', metadata: 'read' } },
      );
    }
    return { status: 200, body: verification(opts.failVerification ? 'FAILED' : 'SUCCEEDED') };
  });
  api.route('DELETE', '/api/v1/integrations/github/installations/[^/]+', 'disconnectGitHubInstallation', () => {
    const inst = opts.installations[0];
    if (inst) inst.status = 'REVOKED';
    return { status: 204 };
  });
}

test('integrations (owner): connect GitHub App via GitHub redirect, PENDING → verified, disconnect (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const installations: S['GitHubInstallation'][] = [];
  mockGitHub(api, { installations });
  // «GitHub»: страница установки сразу возвращает на Setup URL App с installation_id и state.
  await page.route('https://github.test/**', async (route) => {
    const origin = new URL(page.url()).origin;
    await route.fulfill({ status: 302, headers: { location: `${origin}/integrations/github/setup?installation_id=42&state=st-e2e&setup_action=install` } });
  });
  await login(page, api);
  await page.goto(`/w/${ws.id}/integrations`);
  await expect(page.getByRole('heading', { level: 1, name: 'Интеграции' })).toBeFocused();
  await expect(page.getByText('Установок пока нет')).toBeVisible();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `integrations empty (${scheme})`);
  }

  await page.getByRole('button', { name: 'Подключить GitHub App' }).click();
  // Возврат: Setup URL вне workspace → страница интеграций workspace (state запомнен перед уходом на GitHub).
  await expect(page).toHaveURL(`/w/${ws.id}/integrations`);
  await expect(page.getByText('Установка 42 зарегистрирована и проверяется.')).toBeVisible();
  await expect(page.getByText('Установка проверена и подключена.')).toBeVisible();
  const connect = api.requests.find((r) => r.operationId === 'connectGitHubInstallation');
  expect(connect?.body).toEqual({ installation_id: 42, state: 'st-e2e', setup_action: 'install' });
  expect(connect?.headers['x-workspace-id']).toBe(ws.id);
  const table = page.getByRole('table', { name: 'Установки GitHub App' });
  await expect(table.getByRole('row', { name: /acme/ })).toContainText('Активна');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `integrations connected (${scheme})`);
  }

  await table.getByRole('button', { name: 'Отключить установку acme (42)' }).click();
  const dialog = page.getByRole('dialog', { name: 'Отключить установку «acme (42)»?' });
  await expectNoSeriousA11yViolations(page, 'disconnect dialog');
  await dialog.getByRole('button', { name: 'Отключить установку' }).click();
  await expect(page.getByText('Установка «acme (42)» отключена.')).toBeVisible();
  await expect(table.getByRole('row', { name: /acme/ })).toContainText('Отключена');
  expect(api.requests.filter((r) => r.operationId === 'connectGitHubInstallation')).toHaveLength(1);
});

test('integrations: failed verification shows the reason; viewer read-only at 360px', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const installations: S['GitHubInstallation'][] = [];
  mockGitHub(api, { installations, failVerification: true });
  await login(page, api);
  await page.goto(`/w/${ws.id}/integrations?installation_id=88&state=st-e2e&setup_action=install`);
  await expect(page.getByText('Проверка не пройдена: права установки не совпадают с минимальным набором (workflows:write)')).toBeVisible();
  await expect(page.getByRole('table', { name: 'Установки GitHub App' }).getByRole('row', { name: /88/ })).toContainText('Отключена');
  await expect(page).toHaveURL(`/w/${ws.id}/integrations`);

  const viewerWs = api.addWorkspace('Наблюдение', 'watch', 'viewer');
  await page.setViewportSize({ width: 360, height: 740 });
  await page.goto(`/w/${viewerWs.id}/integrations`);
  await expect(page.getByText('Подключать и отключать установки могут владелец и администраторы.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Подключить GitHub App' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /Отключить/ })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'integrations viewer 360px');
});

test('pull requests: project tab (merge request → approval link) and completed run (CI approval, then queued) (axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'developer');
  const project = api.addProject(ws.id, 'Платежи');
  const run = api.addRun(project, { status: 'COMPLETED', objective: 'Исправить округление' });
  const pr: S['PullRequest'] = {
    id: '01900000-0000-7000-c000-0000000000a1',
    project_id: project.id,
    repository_id: '01900000-0000-7000-c000-0000000000b1',
    repository_full_name: 'acme/payments',
    run_id: run.id,
    pr_number: 7,
    url: 'https://github.com/acme/payments/pull/7',
    state: 'OPEN',
    head_branch: 'aicd/run/task',
    base_branch: 'main',
    head_sha: 'a'.repeat(40),
    merged_commit_sha: null,
    create_action_id: null,
    merge_action_id: null,
    created_at: AT,
    updated_at: AT,
    merged_at: null,
    closed_at: null,
  };
  api.route('GET', '/api/v1/projects/{id}/repositories', 'listRepositories', () => ({ status: 200, body: { items: [], next_cursor: null } }));
  api.route('GET', '/api/v1/projects/{id}/pull-requests', 'listPullRequests', () => ({ status: 200, body: { items: [pr], next_cursor: null } }));
  api.route('POST', '/api/v1/projects/{id}/pull-requests/[^/]+/merge-request', 'requestPullRequestMerge', () => ({
    status: 202,
    body: { pull_request_id: pr.id, approval_id: '01900000-0000-7000-d000-000000000001', status: 'AWAITING_APPROVAL' },
  }));
  let creates = 0;
  api.route('POST', '/api/v1/runs/{id}/pull-requests', 'requestPullRequest', ({ body }) => {
    creates += 1;
    if (creates === 1) return problem(403, 'policy_denied', { approval_id: '01900000-0000-7000-d000-000000000002' } as Partial<S['Problem']>);
    const b = body as S['PullRequestCreateRequest'];
    return {
      status: 202,
      body: {
        run_id: run.id,
        task_id: '01900000-0000-7000-c000-0000000000c1',
        repository_id: pr.repository_id,
        repository_full_name: 'acme/payments',
        head_branch: 'aicd/run/task',
        head_sha: 'b'.repeat(40),
        base_branch: b.base_branch ?? 'main',
        title: b.title ?? 'AiCodingDev',
        status: 'QUEUED',
      },
    };
  });
  await login(page, api);

  await page.goto(`/w/${ws.id}/projects/${project.id}`);
  await page.getByRole('tab', { name: 'Pull requests' }).click();
  const table = page.getByRole('table', { name: 'Pull requests' });
  await expect(table.getByRole('link', { name: 'Открыть PR #7 на GitHub' })).toHaveAttribute('href', pr.url);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `project pull requests (${scheme})`);
  }
  await table.getByRole('button', { name: 'Запросить merge PR #7' }).click();
  const merge = page.getByRole('dialog', { name: 'Запросить merge PR #7' });
  await merge.getByRole('radio', { name: 'Squash' }).check();
  await merge.getByRole('button', { name: 'Запросить merge' }).click();
  await expect(page.getByRole('link', { name: 'Перейти к одобрению' })).toHaveAttribute(
    'href',
    `/w/${ws.id}/approvals/01900000-0000-7000-d000-000000000001`,
  );
  expect(api.requests.find((r) => r.operationId === 'requestPullRequestMerge')?.body).toEqual({ merge_method: 'squash' });

  await page.goto(`/w/${ws.id}/projects/${project.id}/runs/${run.id}`);
  await page.getByRole('button', { name: 'Создать pull request' }).click();
  let dialog = page.getByRole('dialog', { name: 'Создать pull request' });
  await dialog.getByLabel('Целевая ветка').fill('bad branch');
  await dialog.getByRole('button', { name: 'Создать PR' }).click();
  await expect(dialog.getByLabel('Целевая ветка')).toHaveAttribute('aria-invalid', 'true');
  expect(creates).toBe(0);
  await dialog.getByLabel('Целевая ветка').fill('release/1.0');
  await dialog.getByRole('button', { name: 'Создать PR' }).click();
  await expect(page.getByText('Изменения CI-конфигурации требуют одобрения перед push.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Перейти к одобрению' })).toHaveAttribute(
    'href',
    `/w/${ws.id}/approvals/01900000-0000-7000-d000-000000000002`,
  );
  await page.getByRole('button', { name: 'Создать pull request' }).click();
  dialog = page.getByRole('dialog', { name: 'Создать pull request' });
  await dialog.getByLabel('Заголовок PR').fill('Округление');
  await dialog.getByRole('button', { name: 'Создать PR' }).click();
  await expect(page.getByText('Создание PR поставлено в очередь: acme/payments, ветка aicd/run/task → release/1.0.')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'run pull requests');
});
