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
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
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
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
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
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
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
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
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

  it('shows not found for an inaccessible project and hides attach for viewers', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      [`GET /api/v1/projects/${P1.id}`]: () => problem(404, 'not_found'),
    });
    renderApp(`/w/${WS_ID}/projects/${P1.id}`);
    expect(await screen.findByText('Проект не найден')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Привязать репозиторий' })).toBeNull();
  });
});
