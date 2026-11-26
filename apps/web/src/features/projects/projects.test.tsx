import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Repository } from '../../api/projects';
import i18n from '../../i18n';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../../test/utils';
import { validateAttach } from './AttachRepositoryForm';
import { lineNumbers } from './RepositoryExplorer';

const P1 = project(1);
const REPO: Repository = {
  id: '01900000-0000-7000-a100-000000000001',
  project_id: P1.id,
  provider: 'github',
  remote_url: 'https://github.com/acme/payments',
  default_branch: 'main',
  local_root_ref: null,
  last_synced_at: '2026-11-07T10:00:00Z',
  latest_index_snapshot: { snapshot_id: '01900000-0000-7000-a100-000000000002', commit_sha: 'a'.repeat(40), status: 'READY' },
};
const t = i18n.t.bind(i18n) as (k: string, o?: Record<string, unknown>) => string;

describe('validateAttach', () => {
  it('requires the location for the chosen provider and a GitHub URL format', () => {
    expect(validateAttach({ provider: 'local', remoteUrl: '', localPath: '', branch: '' }, t)).toEqual({ local_path: 'Обязательное поле.' });
    expect(validateAttach({ provider: 'github', remoteUrl: 'https://gitlab.com/a/b', localPath: '', branch: '' }, t).remote_url).toMatch(/github\.com/);
    expect(validateAttach({ provider: 'github', remoteUrl: 'https://github.com/a/b', localPath: 'main/b', branch: 'x'.repeat(256) }, t)).toEqual({
      default_branch: 'Не более 255 символов.',
    });
    // GitHub-репозиторий тоже требует локальную рабочую копию (OQ-47 (6)).
    expect(validateAttach({ provider: 'github', remoteUrl: 'https://github.com/a/b', localPath: '', branch: '' }, t)).toEqual({
      local_path: 'Обязательное поле.',
    });
  });
});

describe('lineNumbers', () => {
  it('numbers lines without an extra trailing line', () => {
    expect(lineNumbers('')).toBe('');
    expect(lineNumbers('a')).toBe('1');
    expect(lineNumbers('a\nb\n')).toBe('1\n2');
    expect(lineNumbers('a\n\nb')).toBe('1\n2\n3');
  });
});

describe('ProjectDetailPage (Repository Explorer)', () => {
  const base = {
    'GET /api/v1/auth/session': () => json(200, session('owner')),
    [`GET /api/v1/projects/${P1.id}`]: () => json(200, P1),
    [`GET /api/v1/projects/${P1.id}/technologies`]: () =>
      json(200, {
        items: [
          {
            technology_id: '01900000-0000-7000-a200-000000000001',
            name: 'Python',
            ecosystem: 'pypi',
            technology_version_id: '01900000-0000-7000-a200-000000000002',
            version_label: '3.12',
            support_status: 'supported',
            detected_from: 'pyproject.toml',
            confirmed_by: null,
          },
        ],
        next_cursor: null,
      }),
  };

  it('shows repositories with index state, no-local-copy notice and the stack', async () => {
    const user = userEvent.setup();
    mockFetch({ ...base, [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [REPO], next_cursor: null }) });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Проект 1' })).toBeInTheDocument();
    const list = await screen.findByRole('list', { name: 'Репозитории проекта' });
    expect(within(list).getByText('https://github.com/acme/payments')).toBeInTheDocument();
    expect(within(list).getByText('aaaaaaaaaaaa')).toBeInTheDocument();
    expect(within(list).getByText('Просмотр файлов недоступен')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: 'Технологии' }));
    expect(await screen.findByRole('cell', { name: 'pyproject.toml' })).toBeInTheDocument();
  });

  it('attaches a local repository with an idempotency key', async () => {
    const user = userEvent.setup();
    let repos: Repository[] = [];
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: repos, next_cursor: null }),
      [`POST /api/v1/projects/${P1.id}/repositories`]: () => {
        repos = [{ ...REPO, provider: 'local', remote_url: null, local_root_ref: 'repos/payments', latest_index_snapshot: null }];
        return json(201, repos[0]);
      },
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    expect(await screen.findByText('Репозиторий не привязан')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    await user.click(within(dialog).getByRole('button', { name: 'Привязать' }));
    expect(within(dialog).getByLabelText('Путь к каталогу')).toHaveAttribute('aria-invalid', 'true');
    await user.type(within(dialog).getByLabelText('Путь к каталогу'), '/srv/repos/payments');
    await user.click(within(dialog).getByRole('button', { name: 'Привязать' }));
    expect(await screen.findByText('Репозиторий привязан, индексация поставлена в очередь.')).toBeInTheDocument();
    const post = calls.find((c) => c.method === 'POST');
    expect(post?.body).toEqual({ provider: 'local', local_path: '/srv/repos/payments' });
    expect(post?.headers.get('Idempotency-Key')).toBeTruthy();
    await waitFor(() => expect(screen.getByText('repos/payments')).toBeInTheDocument());
  });

  it('offers the allowed directories instead of asking for a path', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/repository-roots': () =>
        json(200, {
          items: [
            {
              name: 'repos',
              path: '/srv/repos',
              truncated: false,
              repositories: [
                { ref: 'repos/payments', path: '/srv/repos/payments', name: 'payments', attached: false },
                { ref: 'repos/billing', path: '/srv/repos/billing', name: 'billing', attached: false },
              ],
            },
          ],
        }),
      [`POST /api/v1/projects/${P1.id}/repositories`]: () => json(201, { ...REPO, provider: 'local', remote_url: null }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    const select = await within(dialog).findByLabelText('Путь к каталогу');
    expect(select.tagName).toBe('SELECT');
    expect(within(dialog).getByText(/Разрешённые корни: repos \(\/srv\/repos\)|Найдено в разрешённых корнях сервера: repos \(\/srv\/repos\)/)).toBeInTheDocument();
    await user.selectOptions(select, 'repos/billing');
    await user.click(within(dialog).getByRole('button', { name: 'Привязать' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ provider: 'local', local_path: 'repos/billing' }));
  });

  it('falls back to typing a path when nothing was found in the allowed roots', async () => {
    const user = userEvent.setup();
    mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/repository-roots': () => json(200, { items: [] }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    expect(await within(dialog).findByText('Корни репозиториев не настроены')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Путь к каталогу').tagName).toBe('INPUT');
  });

  it('offers the repositories the GitHub App installation can reach and fills the chosen one in', async () => {
    const user = userEvent.setup();
    const installation = '01900000-0000-7000-a300-000000000001';
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/repository-roots': () =>
        json(200, {
          items: [{ name: 'repos', path: '/srv/repos', truncated: false, repositories: [{ ref: 'repos/payments', path: '/srv/repos/payments', name: 'payments', attached: false }] }],
        }),
      'GET /api/v1/integrations/github/installations': () =>
        json(200, {
          items: [
            {
              id: installation,
              installation_id: 77,
              account_login: 'acme',
              account_type: 'organization',
              repository_selection: 'selected',
              permissions: {},
              status: 'ACTIVE',
              created_at: '2026-11-01T00:00:00Z',
              updated_at: '2026-11-01T00:00:00Z',
            },
          ],
        }),
      [`GET /api/v1/integrations/github/installations/${installation}/repositories`]: () =>
        json(200, {
          items: [
            {
              github_repo_id: 1001,
              full_name: 'acme/payments',
              owner: 'acme',
              name: 'payments',
              private: true,
              archived: false,
              default_branch: 'release',
              remote_url: 'https://github.com/acme/payments',
              attached: false,
            },
          ],
          total: 1,
          truncated: false,
          synced_at: '2026-11-10T09:00:00Z',
          stale: false,
          repository_selection: 'selected',
        }),
      [`POST /api/v1/projects/${P1.id}/repositories`]: () => json(202, { id: 'v1', kind: 'repository', status: 'PENDING' }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    await user.click(within(dialog).getByRole('radio', { name: 'GitHub' }));
    const picker = await within(dialog).findByLabelText('Репозиторий GitHub');
    expect(within(picker).getByRole('option', { name: /acme\/payments · приватный/ })).toBeInTheDocument();
    await user.selectOptions(picker, 'acme/payments');
    await user.selectOptions(within(dialog).getByLabelText('Путь к каталогу'), 'repos/payments');
    await user.click(within(dialog).getByRole('button', { name: 'Привязать' }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST' && c.url.pathname.endsWith('/repositories'))?.body).toEqual({
        provider: 'github',
        remote_url: 'https://github.com/acme/payments',
        local_path: 'repos/payments',
        default_branch: 'release',
      }),
    );
  });

  it('offers to clone a working copy when there is none yet', async () => {
    const user = userEvent.setup();
    const installation = '01900000-0000-7000-a300-000000000004';
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/repository-roots': () => json(200, { items: [{ name: 'repos', path: '/srv/repos', truncated: false, repositories: [] }] }),
      'GET /api/v1/integrations/github/installations': () =>
        json(200, {
          items: [
            {
              id: installation,
              installation_id: 77,
              account_login: 'acme',
              account_type: 'organization',
              repository_selection: 'all',
              permissions: {},
              status: 'ACTIVE',
              created_at: '2026-11-01T00:00:00Z',
              updated_at: '2026-11-01T00:00:00Z',
            },
          ],
        }),
      [`GET /api/v1/integrations/github/installations/${installation}/repositories`]: () =>
        json(200, {
          items: [
            {
              github_repo_id: 1001,
              full_name: 'acme/payments',
              owner: 'acme',
              name: 'payments',
              private: true,
              archived: false,
              default_branch: 'main',
              remote_url: 'https://github.com/acme/payments',
              attached: false,
            },
          ],
          total: 1,
          truncated: false,
          synced_at: '2026-11-10T09:00:00Z',
          stale: false,
          repository_selection: 'all',
        }),
      [`POST /api/v1/projects/${P1.id}/repositories`]: () => json(202, { id: 'v1', kind: 'repository', status: 'PENDING' }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    await user.click(within(dialog).getByRole('radio', { name: 'GitHub' }));
    await user.selectOptions(await within(dialog).findByLabelText('Репозиторий GitHub'), 'acme/payments');
    await user.click(within(dialog).getByRole('checkbox', { name: /создать её клонированием/i }));
    // Каталог предлагается сам: имя репозитория в первом разрешённом корне.
    expect(within(dialog).getByLabelText('Новый каталог для рабочей копии')).toHaveValue('repos/payments');
    await user.click(within(dialog).getByRole('button', { name: 'Привязать' }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === 'POST' && c.url.pathname.endsWith('/repositories'))?.body).toEqual({
        provider: 'github',
        remote_url: 'https://github.com/acme/payments',
        create_local_copy: true,
        local_path: 'repos/payments',
        default_branch: 'main',
      }),
    );
  });

  it('marks what is already attached and does not offer it again', async () => {
    const user = userEvent.setup();
    const installation = '01900000-0000-7000-a300-000000000003';
    mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/repository-roots': () =>
        json(200, {
          items: [
            {
              name: 'repos',
              path: '/srv/repos',
              truncated: false,
              repositories: [
                { ref: 'repos/payments', path: '/srv/repos/payments', name: 'payments', attached: true },
                { ref: 'repos/billing', path: '/srv/repos/billing', name: 'billing', attached: false },
              ],
            },
          ],
        }),
      'GET /api/v1/integrations/github/installations': () =>
        json(200, {
          items: [
            {
              id: installation,
              installation_id: 77,
              account_login: 'acme',
              account_type: 'organization',
              repository_selection: 'all',
              permissions: {},
              status: 'ACTIVE',
              created_at: '2026-11-01T00:00:00Z',
              updated_at: '2026-11-01T00:00:00Z',
            },
          ],
        }),
      [`GET /api/v1/integrations/github/installations/${installation}/repositories`]: () =>
        json(200, {
          items: [
            {
              github_repo_id: 1001,
              full_name: 'acme/payments',
              owner: 'acme',
              name: 'payments',
              private: false,
              archived: false,
              default_branch: 'main',
              remote_url: 'https://github.com/acme/payments',
              attached: true,
            },
          ],
          total: 1,
          truncated: false,
          synced_at: '2026-11-10T09:00:00Z',
          stale: false,
          repository_selection: 'all',
        }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    // Занятый каталог виден (понятно, почему его нет среди доступных), но выбрать его нельзя.
    const paths = await within(dialog).findByLabelText('Путь к каталогу');
    expect(within(paths).getByRole('option', { name: /repos\/payments.*уже привязан/ })).toBeDisabled();
    expect(within(paths).getByRole('option', { name: /repos\/billing/ })).toBeEnabled();
    await user.click(within(dialog).getByRole('radio', { name: 'GitHub' }));
    const picker = await within(dialog).findByLabelText('Репозиторий GitHub');
    expect(within(picker).getByRole('option', { name: /acme\/payments · уже привязан/ })).toBeDisabled();
  });

  it('asks the worker to re-read the installation when the list has never been loaded', async () => {
    const user = userEvent.setup();
    const installation = '01900000-0000-7000-a300-000000000002';
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/repository-roots': () => json(200, { items: [] }),
      'GET /api/v1/integrations/github/installations': () =>
        json(200, {
          items: [
            {
              id: installation,
              installation_id: 77,
              account_login: 'acme',
              account_type: 'organization',
              repository_selection: 'all',
              permissions: {},
              status: 'ACTIVE',
              created_at: '2026-11-01T00:00:00Z',
              updated_at: '2026-11-01T00:00:00Z',
            },
          ],
        }),
      [`GET /api/v1/integrations/github/installations/${installation}/repositories`]: () =>
        json(200, { items: [], total: 0, truncated: false, synced_at: null, stale: true, repository_selection: 'all' }),
      [`POST /api/v1/integrations/github/installations/${installation}/repositories/refresh`]: () => json(202, { queued: true }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Привязать репозиторий' }));
    const dialog = await screen.findByRole('dialog', { name: 'Привязка репозитория' });
    await user.click(within(dialog).getByRole('radio', { name: 'GitHub' }));
    // Пустой кэш — форма не оставляет оператора с пустым списком и просит воркер прочитать состав.
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.pathname.endsWith('/repositories/refresh'))).toBe(true));
    expect(within(dialog).getByLabelText('Адрес репозитория GitHub').tagName).toBe('INPUT');
  });

  it('browses the tree of a local repository and shows a file as plain text', async () => {
    const user = userEvent.setup();
    const local: Repository = { ...REPO, provider: 'local', remote_url: null, local_root_ref: 'repos/payments' };
    const sha = 'b'.repeat(40);
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [local], next_cursor: null }),
      [`GET /api/v1/projects/${P1.id}/repositories/${REPO.id}/tree`]: () =>
        json(200, {
          repository_id: REPO.id,
          commit_sha: sha,
          path: '',
          items: [{ name: 'a.html', path: 'a.html', type: 'file', mode: '100644', size: 20 }],
          next_cursor: null,
        }),
      [`GET /api/v1/projects/${P1.id}/repositories/${REPO.id}/file`]: () =>
        json(200, {
          repository_id: REPO.id,
          commit_sha: sha,
          path: 'a.html',
          type: 'file',
          mode: '100644',
          size: 20,
          max_bytes: 524288,
          truncated: false,
          is_binary: false,
          encoding: 'utf-8',
          content: '<img src=x>\n<i>x</i>\n',
        }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Открыть дерево файлов' }));
    await user.click(await screen.findByRole('button', { name: /a\.html/ }));
    const region = await screen.findByRole('region', { name: 'Содержимое файла a.html' });
    expect(region).toHaveTextContent('<img src=x>');
    expect(region.querySelector('img, i')).toBeNull();
    const fileCall = calls.find((c) => c.url.pathname.endsWith('/file'));
    expect(fileCall?.url.searchParams.get('commit')).toBe(sha);
    expect(fileCall?.url.searchParams.get('path')).toBe('a.html');
  });

  it('selects a branch and a recent commit from the repository history (OQ-45 (c))', async () => {
    const user = userEvent.setup();
    const local: Repository = { ...REPO, provider: 'local', remote_url: null, local_root_ref: 'repos/payments' };
    const head = 'c'.repeat(40);
    const older = 'd'.repeat(40);
    const base64 = `/api/v1/projects/${P1.id}/repositories/${REPO.id}`;
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [local], next_cursor: null }),
      [`GET ${base64}/branches`]: () =>
        json(200, {
          repository_id: REPO.id,
          default_branch: 'main',
          truncated: false,
          items: [
            { name: 'main', commit_sha: 'b'.repeat(40), committed_at: '2026-11-07T10:00:00Z', is_default: true },
            { name: 'feature/x', commit_sha: head, committed_at: '2026-11-07T09:00:00Z', is_default: false },
          ],
          next_cursor: null,
        }),
      [`GET ${base64}/commits`]: (req) =>
        json(200, {
          repository_id: REPO.id,
          commit_sha: req.url.searchParams.get('commit') === 'feature/x' ? head : 'b'.repeat(40),
          items: [
            { sha: head, parents: [older], author_name: 't', authored_at: null, committed_at: null, subject: '<b>fix</b> login' },
            { sha: older, parents: [], author_name: 't', authored_at: null, committed_at: null, subject: 'init' },
          ],
          next_cursor: null,
        }),
      [`GET ${base64}/tree`]: (req) =>
        json(200, {
          repository_id: REPO.id,
          commit_sha: req.url.searchParams.get('commit') === older ? older : head,
          path: '',
          items: [{ name: 'README.md', path: 'README.md', type: 'file', mode: '100644', size: 3 }],
          next_cursor: null,
        }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Открыть дерево файлов' }));
    const branch = await screen.findByLabelText('Ветка');
    await waitFor(() => expect(within(branch).getAllByRole('option')).toHaveLength(2));
    await user.selectOptions(branch, 'feature/x');
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/tree') && c.url.searchParams.get('commit') === 'feature/x')).toBe(true));
    const commit = screen.getByLabelText('Commit');
    await waitFor(() => expect(within(commit).getByRole('option', { name: `${older.slice(0, 12)} — init` })).toBeInTheDocument());
    // Тема commit — недоверенный текст: только как текст опции.
    expect(within(commit).getByRole('option', { name: `${head.slice(0, 12)} — <b>fix</b> login` })).toBeInTheDocument();
    await user.selectOptions(commit, older);
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/tree') && c.url.searchParams.get('commit') === older)).toBe(true));
    expect(await screen.findByTestId('explorer-resolved-commit')).toHaveTextContent(older.slice(0, 12));
    expect(calls.find((c) => c.url.pathname.endsWith('/commits') && c.url.searchParams.get('commit') === 'feature/x')).toBeTruthy();
  });

  it('detaches a repository after confirmation and refreshes the list', async () => {
    const user = userEvent.setup();
    const local: Repository = { ...REPO, provider: 'local', remote_url: null, local_root_ref: 'repos/payments' };
    let detached = false;
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: detached ? [] : [local], next_cursor: null }),
      [`DELETE /api/v1/projects/${P1.id}/repositories/${REPO.id}`]: () => {
        detached = true;
        return new Response(null, { status: 204 });
      },
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Отвязать' }));
    const dialog = await screen.findByRole('dialog', { name: 'Отвязать репозиторий от проекта' });
    // В диалоге сказано, что файлы на диске остаются: «отвязать» — это не «удалить рабочую копию».
    expect(within(dialog).getByText(/Файлы рабочей копии на диске не удаляются/)).toBeInTheDocument();
    await user.click(within(dialog).getByTestId('detach-confirm'));
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true));
    expect(await screen.findByText('Репозиторий не привязан')).toBeInTheDocument();
  });

  it('keeps the repository when detaching is refused because a run is unfinished', async () => {
    const user = userEvent.setup();
    const local: Repository = { ...REPO, provider: 'local', remote_url: null, local_root_ref: 'repos/payments' };
    mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [local], next_cursor: null }),
      [`DELETE /api/v1/projects/${P1.id}/repositories/${REPO.id}`]: () =>
        problem(409, 'conflict', { detail: 'Репозиторий занят: незавершённых прогонов — 1. Дождитесь их или отмените.' }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=repositories`);
    await user.click(await screen.findByRole('button', { name: 'Отвязать' }));
    const dialog = await screen.findByRole('dialog', { name: 'Отвязать репозиторий от проекта' });
    await user.click(within(dialog).getByTestId('detach-confirm'));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('незавершённых прогонов');
    expect(screen.getByText('repos/payments')).toBeInTheDocument();
  });

  it('shows not found for an inaccessible project and hides attach for viewers', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      [`GET /api/v1/projects/${P1.id}`]: () => problem(404, 'not_found'),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
    expect(await screen.findByText('Проект не найден')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Привязать репозиторий' })).toBeNull();
  });

  it('workspace tabs: worktrees (B4) without host paths link to Mission Control, task and Code Review', async () => {
    const run = '01900000-0000-7000-b000-000000000001';
    const task = '01900000-0000-7000-b000-000000000002';
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('developer')),
      [`GET /api/v1/projects/${P1.id}`]: () => json(200, P1),
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [REPO], next_cursor: null }),
      [`GET /api/v1/projects/${P1.id}/worktrees`]: () =>
        json(200, {
          items: [
            {
              id: '01900000-0000-7000-b000-000000000003',
              repository_id: REPO.id,
              run_id: run,
              task_id: task,
              branch_name: 'aicd/run/export',
              base_commit_sha: 'c'.repeat(40),
              head_commit_sha: 'd'.repeat(40),
              status: 'ACTIVE',
              created_at: '2026-11-08T09:00:00Z',
              updated_at: '2026-11-08T09:05:00Z',
              removed_at: null,
            },
          ],
          next_cursor: null,
        }),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}?tab=worktrees`);
    const table = await screen.findByRole('table', { name: 'Worktrees' });
    expect(within(table).getByText('aicd/run/export')).toBeInTheDocument();
    expect(within(table).getByText('Активен')).toBeInTheDocument();
    expect(within(table).getByText('cccccccccccc')).toBeInTheDocument();
    expect(within(table).getByRole('link', { name: /^Ревью кода/ })).toHaveAttribute('href', `/w/${WS_ID}/projects/${P1.id}/review/${run}?task=${task}`);
    expect(within(table).getByRole('link', { name: /^Mission Control/ })).toHaveAttribute('href', `/w/${WS_ID}/mission-control?run=${run}&task=${task}`);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText('Статус'), 'REMOVED');
    await waitFor(() => expect(calls.some((c) => c.url.pathname.endsWith('/worktrees') && c.url.searchParams.get('status') === 'REMOVED')).toBe(true));
  });
});
