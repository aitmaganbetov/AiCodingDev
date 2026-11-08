import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import i18n from '../../i18n';
import { ApiError } from '../../api/client';
import type { GitHubInstallation, GitHubVerification, PullRequest } from '../../api/github';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../../test/utils';
import { recallSetup, rememberSetup } from './GitHubPage';
import { problemApprovalId, validatePullRequest } from './PullRequestsPanel';
import { verificationMessage } from './VerificationStatus';

const t = i18n.t.bind(i18n);
const AT = '2026-11-08T10:00:00Z';
const INST_ID = '01900000-0000-7000-b000-000000000001';
const VER_ID = '01900000-0000-7000-b000-0000000000f1';

function installation(status: GitHubInstallation['status'], overrides: Partial<GitHubInstallation> = {}): GitHubInstallation {
  return {
    id: INST_ID,
    installation_id: 42,
    account_login: status === 'PENDING' ? 'pending' : 'acme',
    account_type: 'organization',
    repository_selection: 'selected',
    permissions: {},
    status,
    created_at: AT,
    updated_at: AT,
    ...overrides,
  };
}

function verification(status: GitHubVerification['status'], error: GitHubVerification['error'] = null): GitHubVerification {
  return {
    id: VER_ID,
    kind: 'installation',
    status,
    project_id: null,
    result_id: status === 'SUCCEEDED' ? INST_ID : null,
    error,
    created_at: AT,
    updated_at: AT,
    completed_at: status === 'PENDING' ? null : AT,
  };
}

describe('integrations helpers', () => {
  it('verification messages per status and error code', () => {
    expect(verificationMessage(t, verification('PENDING'))).toMatch(/проверяет/);
    expect(verificationMessage(t, verification('SUCCEEDED'))).toBe('Установка проверена и подключена.');
    expect(verificationMessage(t, verification('FAILED', { code: 'permissions', detail: 'workflows:write' }))).toBe(
      'Проверка не пройдена: права установки не совпадают с минимальным набором (workflows:write)',
    );
    expect(verificationMessage(t, verification('FAILED', { code: 'brand_new', detail: null }))).toBe('Проверка не пройдена: brand_new');
  });

  it('setup state is remembered per workspace and only for the same state', () => {
    rememberSetup(WS_ID, 'state-1');
    expect(recallSetup('state-1')).toBe(WS_ID);
    expect(recallSetup('other')).toBeNull();
  });

  it('PR form validation and approval_id from a policy_denied problem', () => {
    expect(validatePullRequest({ title: '', base: 'main' }, t)).toEqual({});
    expect(validatePullRequest({ title: 'x'.repeat(257), base: 'bad branch' }, t)).toEqual({
      title: 'Не более 256 символов.',
      base_branch: 'Допустимы латинские буквы, цифры и . _ / -',
    });
    const err = new ApiError({ status: 403, code: 'policy_denied', problem: { type: 'x', title: 'x', status: 403, code: 'policy_denied', approval_id: 'a1' } as never });
    expect(problemApprovalId(err)).toBe('a1');
    expect(problemApprovalId(new Error('x'))).toBeUndefined();
  });
});

describe('GitHub integration page', () => {
  it('handles the GitHub setup callback once: connect (201 PENDING) → verification polled to SUCCEEDED → list refreshed', async () => {
    let checks = 0;
    let current = installation('PENDING');
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/integrations/github/installations': () => json(200, { items: [current] }),
      'POST /api/v1/integrations/github/installations': () => json(201, { ...installation('PENDING'), verification_id: VER_ID }),
      [`GET /api/v1/integrations/github/verifications/${VER_ID}`]: () => {
        checks += 1;
        if (checks < 2) return json(200, verification('PENDING'));
        current = installation('ACTIVE');
        return json(200, verification('SUCCEEDED'));
      },
    });
    renderApp(`/w/${WS_ID}/integrations?installation_id=42&state=st-1&setup_action=install`);
    expect(await screen.findByText('Установка 42 зарегистрирована и проверяется.')).toBeInTheDocument();
    expect(await screen.findByText('Установка проверена и подключена.', {}, { timeout: 5000 })).toBeInTheDocument();
    const table = await screen.findByRole('table', { name: 'Установки GitHub App' });
    await waitFor(() => expect(within(table).getByText('acme')).toBeInTheDocument());
    expect(within(table).getByText('Активна')).toBeInTheDocument();
    const connects = calls.filter((c) => c.method === 'POST' && c.url.pathname === '/api/v1/integrations/github/installations');
    expect(connects).toHaveLength(1);
    expect(connects[0]?.body).toEqual({ installation_id: 42, state: 'st-1', setup_action: 'install' });
    expect(connects[0]?.headers.get('X-Workspace-Id')).toBe(WS_ID);
    expect(connects[0]?.headers.get('Idempotency-Key')).toBeTruthy();
  });

  it('viewer sees installations read-only; invalid callback and 503 are explained', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/integrations/github/installations': () => json(200, { items: [installation('ACTIVE')] }),
    });
    const { unmount } = renderApp(`/w/${WS_ID}/integrations?installation_id=x&state=st`);
    const table = await screen.findByRole('table', { name: 'Установки GitHub App' });
    expect(within(table).queryByRole('button', { name: /Отключить/ })).toBeNull();
    expect(screen.getByText('Подключать и отключать установки могут владелец и администраторы.')).toBeInTheDocument();
    expect(screen.getByText(/Ссылка возврата с GitHub некорректна/)).toBeInTheDocument();
    unmount();
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/integrations/github/installations': () => json(200, { items: [] }),
      'POST /api/v1/integrations/github/installations/setup': () => problem(503, 'service_unavailable'),
    });
    renderApp(`/w/${WS_ID}/integrations`);
    expect(await screen.findByText('Установок пока нет')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Подключить GitHub App' }));
    expect(await screen.findByText(/GitHub App не настроен/)).toBeInTheDocument();
  });
});

describe('pull requests', () => {
  const P1 = project(1);
  const RUN = '01900000-0000-7000-c000-000000000001';
  const PR: PullRequest = {
    id: '01900000-0000-7000-c000-0000000000a1',
    project_id: P1.id,
    repository_id: '01900000-0000-7000-c000-0000000000b1',
    repository_full_name: 'acme/app',
    run_id: RUN,
    pr_number: 7,
    url: 'https://github.com/acme/app/pull/7',
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

  it('project tab lists PRs and requests a merge with a link to the approval', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('developer')),
      [`GET /api/v1/projects/${P1.id}`]: () => json(200, P1),
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      [`GET /api/v1/projects/${P1.id}/pull-requests`]: () => json(200, { items: [PR], next_cursor: null }),
      [`POST /api/v1/projects/${P1.id}/pull-requests/${PR.id}/merge-request`]: () =>
        json(202, { pull_request_id: PR.id, approval_id: 'appr-1', status: 'AWAITING_APPROVAL' }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
    await user.click(await screen.findByRole('tab', { name: 'Pull requests' }));
    const table = await screen.findByRole('table', { name: 'Pull requests' });
    expect(within(table).getByRole('link', { name: 'Открыть PR #7 на GitHub' })).toHaveAttribute('rel', 'noopener noreferrer');
    await user.click(within(table).getByRole('button', { name: 'Запросить merge PR #7' }));
    const dialog = await screen.findByRole('dialog', { name: 'Запросить merge PR #7' });
    await user.click(within(dialog).getByRole('radio', { name: 'Squash' }));
    await user.click(within(dialog).getByRole('button', { name: 'Запросить merge' }));
    expect(await screen.findByRole('link', { name: 'Перейти к одобрению' })).toHaveAttribute('href', `/w/${WS_ID}/approvals/appr-1`);
    expect(calls.find((c) => c.url.pathname.endsWith('/merge-request'))?.body).toEqual({ merge_method: 'squash' });
  });
});
