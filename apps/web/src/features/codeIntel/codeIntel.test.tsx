import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { AttemptContextPack } from '../../api/tasks';
import type { Repository } from '../../api/projects';
import { json, mockFetch, problem, project, renderApp, session, WS_ID } from '../../test/utils';
import {
  codeHref,
  codeParamsToSearch,
  formatSeconds,
  fragmentLines,
  isRepositoryPath,
  layoutDependencyGraph,
  parseCodeParams,
  summarizePack,
} from './model';

/** Ленивый экран + namespace грузятся асинхронно: под нагрузкой (параллельные тесты) дольше 1 с по умолчанию. */
const LAZY = { timeout: 5000 };
const P1 = project(1);
const SHA = 'a'.repeat(40);
const REPO: Repository = {
  id: '01900000-0000-7000-a100-000000000001',
  project_id: P1.id,
  provider: 'local',
  remote_url: null,
  default_branch: 'main',
  local_root_ref: 'repos/payments',
  last_synced_at: '2026-11-07T10:00:00Z',
  latest_index_snapshot: { snapshot_id: '01900000-0000-7000-a100-000000000002', commit_sha: SHA, status: 'READY' },
};

describe('URL state', () => {
  it('parses with safe defaults and round-trips without default values', () => {
    const p = parseCodeParams(new URLSearchParams('tab=nope&commit=XYZ&line=0&end=3&depth=9&dir=up&view=list'));
    expect(p).toMatchObject({ tab: 'explorer', commit: '', line: null, end: null, depth: 1, dir: 'both', view: 'list' });
    const q = parseCodeParams(new URLSearchParams(`tab=graph&commit=${SHA.toUpperCase()}&line=10&end=4&depth=3&dir=in`));
    expect(q).toMatchObject({ tab: 'graph', commit: SHA, line: 10, end: 10, depth: 3, dir: 'in' });
    expect(codeParamsToSearch(q).toString()).toBe(`commit=${SHA}&tab=graph&line=10&depth=3&dir=in`);
    expect(codeHref(WS_ID, {})).toBe(`/w/${WS_ID}/code`);
    expect(codeHref(WS_ID, { project: 'p', tab: 'explorer', file: 'src/a.ts', line: 5, end: 9 })).toBe(`/w/${WS_ID}/code?project=p&file=src%2Fa.ts&line=5&end=9`);
  });
});

describe('dependency graph layout', () => {
  it('puts roots in the centre, dependencies right and dependants left; counts edges', () => {
    const nodes = [
      { id: 'src/a.ts', kind: 'file' as const, path: 'src/a.ts', ref: null, language: 'ts', is_test: false, depth: 0 },
      { id: 'src/b.ts', kind: 'file' as const, path: 'src/b.ts', ref: null, language: 'ts', is_test: false, depth: 1 },
      { id: 'external:react', kind: 'external' as const, path: null, ref: 'react', language: null, is_test: false, depth: 2 },
      { id: 'test/a.test.ts', kind: 'file' as const, path: 'test/a.test.ts', ref: null, language: 'ts', is_test: true, depth: 1 },
    ];
    const edges = [
      { source: 'src/a.ts', target: 'src/b.ts', edge_type: 'imports' as const },
      { source: 'src/b.ts', target: 'external:react', edge_type: 'imports' as const },
      { source: 'test/a.test.ts', target: 'src/a.ts', edge_type: 'tests' as const },
    ];
    const out = layoutDependencyGraph({ nodes, edges, roots: ['src/a.ts'] });
    const by = new Map(out.map((n) => [n.node.id, n]));
    expect(by.get('src/a.ts')).toMatchObject({ side: 'root', column: 0, outgoing: 1, incoming: 1 });
    expect(by.get('src/b.ts')).toMatchObject({ side: 'out', column: 1 });
    expect(by.get('external:react')).toMatchObject({ side: 'out', column: 2 });
    expect(by.get('test/a.test.ts')).toMatchObject({ side: 'in', column: -1 });
    expect((by.get('test/a.test.ts')?.x ?? 0) < 0).toBe(true);
  });
});

describe('context pack summary', () => {
  const pack = {
    snapshot_commit: 'b'.repeat(40),
    base_commit: SHA,
    fragments: [
      { section: 'tests', kind: 'dependency', path: 'tests/a.py', lines: [1, 5], commit: SHA, sha256: null, bytes: 100, stale: false, source_ref: null },
      { section: 'code', kind: 'exact', path: 'src/a.py', lines: [10, 20], commit: SHA, sha256: null, bytes: 300, stale: true, source_ref: 'Invoice' },
      { section: 'code', kind: 'map', path: 'src/b.py', lines: null, commit: null, sha256: null, bytes: null, stale: false, source_ref: null },
    ],
    omitted: [
      { section: 'code', kind: 'dependency', path: 'src/big.py', commit: null, reason: 'fragment_too_large' },
      { section: 'code', kind: 'dependency', path: 'src/c.py', commit: null, reason: 'max_files' },
      { section: 'code', kind: 'dependency', path: 'src/d.py', commit: null, reason: 'max_files' },
    ],
    fragments_total: 5,
    omitted_total: 3,
  } as unknown as AttemptContextPack;

  it('groups by section in pipeline order, counts stale and reasons, flags truncation', () => {
    const s = summarizePack(pack);
    expect(s.staleSnapshot).toBe(true);
    expect(s.staleFragments).toBe(1);
    expect(s.bySection.map((x) => [x.section, x.fragments.length, x.bytes])).toEqual([
      ['code', 2, 300],
      ['tests', 1, 100],
    ]);
    expect(s.omittedByReason).toEqual([
      { reason: 'max_files', count: 2 },
      { reason: 'fragment_too_large', count: 1 },
    ]);
    expect(s.fragmentsTruncated).toBe(true);
    expect(s.omittedTruncated).toBe(false);
  });

  it('links only repository paths; lines and durations are parsed defensively', () => {
    expect(isRepositoryPath('src/a.py')).toBe(true);
    expect(isRepositoryPath('doc:fastapi/routing')).toBe(false);
    expect(isRepositoryPath('<repo_map>')).toBe(false);
    expect(isRepositoryPath('../etc/passwd')).toBe(false);
    expect(fragmentLines([3, 9])).toEqual({ from: 3, to: 9 });
    expect(fragmentLines([7])).toEqual({ from: 7, to: 7 });
    expect(fragmentLines([0, 2])).toBeNull();
    expect(fragmentLines(null)).toBeNull();
    const u = { h: 'ч', m: 'мин', s: 'с' };
    expect(formatSeconds(null, u)).toBeNull();
    expect(formatSeconds(4.4, u)).toBe('4 с');
    expect(formatSeconds(125, u)).toBe('2 мин 5 с');
    expect(formatSeconds(3720, u)).toBe('1 ч 2 мин');
  });
});

describe('CodeIntelligencePage', () => {
  const base = {
    'GET /api/v1/auth/session': () => json(200, session('viewer')),
    'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
    [`GET /api/v1/projects/${P1.id}/repositories/${REPO.id}/indexing-jobs`]: () => json(200, { items: [], next_cursor: null }),
    [`GET /api/v1/projects/${P1.id}/repositories/${REPO.id}/commits`]: () => json(200, { repository_id: REPO.id, commit_sha: SHA, items: [], next_cursor: null }),
    [`GET /api/v1/projects/${P1.id}/repositories/${REPO.id}/tree`]: () =>
      json(200, { repository_id: REPO.id, commit_sha: SHA, path: '', items: [], next_cursor: null }),
  };

  it('no index yet: explains how indexing happens instead of empty numbers', async () => {
    mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [{ ...REPO, latest_index_snapshot: null }], next_cursor: null }),
    });
    renderApp(`/w/${WS_ID}/code`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Code Intelligence' }, LAZY)).toBeInTheDocument();
    const state = await screen.findByTestId('no-index', {}, LAZY);
    expect(state).toHaveTextContent('Индекс кода ещё не построен');
    expect(state).toHaveTextContent('aicd-worker index');
    expect(within(screen.getByTestId('index-snapshot')).getByText('не построен')).toBeInTheDocument();
  });

  it('symbol search: ranked results with snapshot, open-in-file link carries path and lines', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      ...base,
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [REPO], next_cursor: null }),
      [`GET /api/v1/projects/${P1.id}/repositories/${REPO.id}/symbols`]: () =>
        json(200, {
          repository_id: REPO.id,
          snapshot: { id: REPO.latest_index_snapshot?.snapshot_id, commit_sha: SHA, status: 'READY', index_version: 1, file_count: 12, symbol_count: 340, completed_at: '2026-11-07T10:00:00Z' },
          items: [
            { id: '01900000-0000-7000-a300-000000000001', name: 'Invoice', qualified_name: 'billing.Invoice', kind: 'class', path: 'src/billing/invoice.py', start_line: 10, end_line: 42, signature: 'class Invoice(Base):', score: 1, match: 'name' },
          ],
          next_cursor: null,
        }),
    });
    renderApp(`/w/${WS_ID}/code?tab=symbols`);
    await user.type(await screen.findByRole('textbox', { name: /^Символ/ }, LAZY), 'Invoice');
    await user.selectOptions(screen.getByLabelText('Вид'), 'class');
    await user.click(screen.getByRole('button', { name: 'Искать' }));
    const results = await screen.findByRole('list', { name: 'Символы по запросу «Invoice»' }, LAZY);
    expect(within(results).getByText('class Invoice(Base):')).toBeInTheDocument();
    expect(screen.getByTestId('snapshot-line')).toHaveTextContent('12 файлов');
    const req = calls.find((c) => c.url.pathname.endsWith('/symbols'));
    expect(req?.url.searchParams.get('q')).toBe('Invoice');
    expect(req?.url.searchParams.get('kind')).toBe('class');
    const open = within(results).getByRole('link', { name: /Открыть в файле/ });
    expect(open.getAttribute('href')).toContain('file=src%2Fbilling%2Finvoice.py&line=10&end=42');
  });

  it('context pack 404 is shown as "not built", not as an error', async () => {
    const run = '01900000-0000-7000-a400-000000000001';
    const task = '01900000-0000-7000-a400-000000000002';
    const attempt = '01900000-0000-7000-a400-000000000003';
    mockFetch({
      ...base,
      'GET /api/v1/analytics/summary': () => problem(500, 'internal_error'),
      'GET /api/v1/runs': () => json(200, { items: [], next_cursor: null }),
      [`GET /api/v1/runs/${run}`]: () => json(200, { id: run, project_id: P1.id }),
      [`GET /api/v1/projects/${P1.id}/repositories`]: () => json(200, { items: [REPO], next_cursor: null }),
      [`GET /api/v1/runs/${run}/tasks`]: () => json(200, { run_id: run, plan_version: 1, tasks: [], edges: [], next_cursor: null }),
      [`GET /api/v1/tasks/${task}/attempts`]: () =>
        json(200, { items: [{ id: attempt, task_id: task, attempt_no: 1, status: 'FAILED', agent_role: 'developer' }], next_cursor: null }),
    });
    renderApp(`/w/${WS_ID}/code?tab=context&run=${run}&task=${task}`);
    expect(await screen.findByText('Context Pack не собирался', {}, LAZY)).toBeInTheDocument();
    await waitFor(() => expect(window.location.search).toContain(`attempt=${attempt}`), LAZY);
    expect(window.location.search).toContain(`project=${P1.id}`);
  });
});
