/**
 * F8: Code Intelligence — проект/репозиторий/commit со статусом снимка индекса; поиск символов (B3) → открыть файл
 * с выделенными строками в Code Explorer; граф зависимостей (React Flow, ленивый) ↔ доступный список, внешние узлы,
 * неполный граф; Context Pack попытки (разделы, строки, stale, пропущенные с причинами, ссылки в explorer); задания
 * индексации (статусы, ошибка как текст, фильтр); состояние «индекса нет»; знания, история, поиск; axe light/dark;
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
const REPO_ID = '01900000-0000-7000-f100-000000000001';
const SNAP_ID = '01900000-0000-7000-f100-000000000002';
const SYM_ID = '01900000-0000-7000-f100-0000000000a1';
const TASK_ATTEMPT = '01900000-0000-7000-f100-0000000000b1';
const HEAD = 'a1b2c3d4e5f6'.padEnd(40, '0');
const OLD = 'f'.repeat(40);
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

const FILE_TEXT = Array.from({ length: 60 }, (_, i) => (i === 11 ? 'class Invoice:  # <script>alert(1)</script>' : `line ${i + 1}`)).join('\n') + '\n';

function snapshotRef(): S['IndexSnapshotRef'] {
  return { id: SNAP_ID, commit_sha: HEAD, status: 'READY', index_version: 3, file_count: 128, symbol_count: 2048, completed_at: AT };
}

function repository(projectId: string, indexed: boolean): S['Repository'] {
  return {
    id: REPO_ID,
    project_id: projectId,
    provider: 'local',
    remote_url: null,
    default_branch: 'main',
    local_root_ref: 'repos/payments',
    last_synced_at: AT,
    latest_index_snapshot: indexed ? { snapshot_id: SNAP_ID, commit_sha: HEAD, status: 'READY' } : null,
  };
}

function dependencyGraph(truncated: boolean): S['DependencyGraph'] {
  const file = (path: string, depth: number, isTest = false): S['DependencyNode'] => ({ id: path, kind: 'file', path, ref: null, language: 'python', is_test: isTest, depth });
  return {
    repository_id: REPO_ID,
    snapshot: snapshotRef(),
    depth: 2,
    direction: 'both',
    roots: ['src/billing/invoice.py'],
    nodes: [
      file('src/billing/invoice.py', 0),
      file('src/billing/tax.py', 1),
      { id: 'external:requests', kind: 'external', path: null, ref: 'requests', language: null, is_test: false, depth: 1 },
      file('tests/test_invoice.py', 1, true),
      file('src/api/routes.py', 2),
    ],
    edges: [
      { source: 'src/billing/invoice.py', target: 'src/billing/tax.py', edge_type: 'imports' },
      { source: 'src/billing/invoice.py', target: 'external:requests', edge_type: 'imports' },
      { source: 'tests/test_invoice.py', target: 'src/billing/invoice.py', edge_type: 'tests' },
      { source: 'src/api/routes.py', target: 'tests/test_invoice.py', edge_type: 'imports' },
    ],
    truncated,
    limits: { max_nodes: 200, max_edges: 500 },
  };
}

function jobs(): S['IndexingJob'][] {
  const base = { repository_id: REPO_ID, base_snapshot_id: null, created_at: AT };
  return [
    { ...base, id: '01900000-0000-7000-f200-000000000001', commit_sha: OLD, kind: 'incremental', status: 'RUNNING', attempt_count: 1, snapshot_id: null, file_count: null, symbol_count: null, started_at: AT, finished_at: null, queue_seconds: 3, duration_seconds: null, error: null },
    { ...base, id: '01900000-0000-7000-f200-000000000002', commit_sha: OLD, kind: 'full', status: 'FAILED', attempt_count: 3, snapshot_id: null, file_count: null, symbol_count: null, started_at: AT, finished_at: AT, queue_seconds: 1, duration_seconds: 42, error: 'tree-sitter parse failed: <path redacted> token=[REDACTED]' },
    { ...base, id: '01900000-0000-7000-f200-000000000003', commit_sha: HEAD, kind: 'full', status: 'SUCCEEDED', attempt_count: 1, snapshot_id: SNAP_ID, file_count: 128, symbol_count: 2048, started_at: AT, finished_at: AT, queue_seconds: 2, duration_seconds: 125, error: null },
  ];
}

function seed(api: MockApi, { indexed = true, truncated = true }: { indexed?: boolean; truncated?: boolean } = {}) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const project = api.addProject(ws.id, 'Платёжный шлюз', 'Сервис платежей');
  const run = api.addRun(project, { status: 'COMPLETED', objective: 'Добавить экспорт счетов в CSV' });
  const task = api.addTask(run, 'Реализовать экспорт счетов', { status: 'SUCCEEDED', attempt_count: 1 });
  const repo = repository(project.id, indexed);
  const requests = { symbols: [] as URL[], deps: [] as URL[], jobs: [] as URL[] };
  api.route('GET', '/api/v1/projects/{id}/repositories', 'listRepositories', ({ id }) =>
    id === project.id ? { status: 200, body: { items: [repo], next_cursor: null } } : problem(404, 'not_found'),
  );
  const base = `/api/v1/projects/{id}/repositories/${REPO_ID}`;
  api.route('GET', `${base}/branches`, 'listRepositoryBranches', () => ({
    status: 200,
    body: {
      repository_id: REPO_ID,
      default_branch: 'main',
      truncated: false,
      items: [
        { name: 'main', commit_sha: HEAD, committed_at: AT, is_default: true },
        { name: 'feature/export', commit_sha: OLD, committed_at: AT, is_default: false },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', `${base}/commits`, 'listRepositoryCommits', ({ url }) => {
    const feature = url.searchParams.get('commit') === 'feature/export';
    const items: S['RepositoryCommitSummary'][] = feature
      ? [{ sha: OLD, parents: [HEAD], author_name: 'Борис', authored_at: AT, committed_at: AT, subject: 'Экспорт: черновик <b>не HTML</b>' }]
      : [
          { sha: HEAD, parents: [OLD, 'e'.repeat(40)], author_name: 'Анна', authored_at: AT, committed_at: AT, subject: 'Merge feature/tax' },
          { sha: OLD, parents: [], author_name: 'Борис', authored_at: AT, committed_at: AT, subject: 'Начальный commit' },
        ];
    return { status: 200, body: { repository_id: REPO_ID, commit_sha: feature ? OLD : HEAD, items, next_cursor: null } };
  });
  api.route('GET', `${base}/tree`, 'listRepositoryTree', ({ url }) => {
    const path = url.searchParams.get('path') ?? '';
    const commit = url.searchParams.get('commit') ?? HEAD;
    const items: S['RepositoryTreeEntry'][] =
      path === 'src/billing'
        ? [
            { name: 'invoice.py', path: 'src/billing/invoice.py', type: 'file', mode: '100644', size: FILE_TEXT.length },
            { name: 'tax.py', path: 'src/billing/tax.py', type: 'file', mode: '100644', size: 40 },
          ]
        : [
            { name: 'src', path: 'src', type: 'directory', mode: '040000', size: null },
            { name: 'README.md', path: 'README.md', type: 'file', mode: '100644', size: 9 },
          ];
    return { status: 200, body: { repository_id: REPO_ID, commit_sha: commit.length === 40 ? commit : HEAD, path, items, next_cursor: null } };
  });
  api.route('GET', `${base}/file`, 'getRepositoryFile', ({ url }) => {
    const path = url.searchParams.get('path') ?? '';
    const content = path === 'src/billing/invoice.py' ? FILE_TEXT : 'TAX = 0.2\n';
    return {
      status: 200,
      body: { repository_id: REPO_ID, commit_sha: url.searchParams.get('commit') ?? HEAD, path, type: 'file', mode: '100644', size: content.length, max_bytes: 1048576, truncated: false, is_binary: false, encoding: 'utf-8', content },
    };
  });
  api.route('GET', `${base}/symbols`, 'listRepositorySymbols', ({ url }) => {
    requests.symbols.push(url);
    if (!indexed) return { status: 200, body: { repository_id: REPO_ID, snapshot: null, items: [], next_cursor: null } };
    const all: S['CodeSymbol'][] = [
      { id: SYM_ID, name: 'Invoice', qualified_name: 'billing.invoice.Invoice', kind: 'class', path: 'src/billing/invoice.py', start_line: 12, end_line: 30, signature: 'class Invoice:  # <script>alert(1)</script>', score: 1, match: 'name' },
      { id: '01900000-0000-7000-f100-0000000000a2', name: 'InvoiceLine', qualified_name: 'billing.invoice.InvoiceLine', kind: 'class', path: 'src/billing/invoice.py', start_line: 40, end_line: 52, signature: null, score: 0.8, match: 'name' },
      { id: '01900000-0000-7000-f100-0000000000a3', name: 'render_invoice', qualified_name: null, kind: 'function', path: 'src/api/routes.py', start_line: 7, end_line: 7, signature: 'def render_invoice(id: str) -> str', score: 0.45, match: 'text' },
    ];
    const cursor = url.searchParams.get('cursor');
    const kind = url.searchParams.get('kind');
    const items = all.filter((s) => !kind || s.kind === kind);
    const page = cursor ? items.slice(2) : items.slice(0, 2);
    return { status: 200, body: { repository_id: REPO_ID, snapshot: snapshotRef(), items: page, next_cursor: !cursor && items.length > 2 ? 'c2' : null } };
  });
  api.route('GET', `${base}/dependencies`, 'getRepositoryDependencies', ({ url }) => {
    requests.deps.push(url);
    if (!indexed) return { status: 200, body: { ...dependencyGraph(false), snapshot: null, nodes: [], edges: [], roots: [] } };
    return { status: 200, body: dependencyGraph(truncated) };
  });
  api.route('GET', `${base}/indexing-jobs`, 'listIndexingJobs', ({ url }) => {
    requests.jobs.push(url);
    const status = url.searchParams.get('status');
    const limit = Number(url.searchParams.get('limit') ?? 50);
    const items = indexed ? jobs().filter((j) => !status || j.status === status) : [];
    return { status: 200, body: { items: items.slice(0, limit), next_cursor: null } };
  });
  api.route('GET', '/api/v1/tasks/{id}/attempts', 'listTaskAttempts', ({ id }) => ({
    status: 200,
    body: {
      items: id === task.id ? [{ id: TASK_ATTEMPT, task_id: task.id, attempt_no: 1, status: 'SUCCEEDED', provider: 'codex', agent_role: 'developer', started_at: AT, finished_at: AT, executions: [] }] : [],
      next_cursor: null,
    },
  }));
  const pack: S['AttemptContextPack'] = {
    id: '01900000-0000-7000-f300-000000000001',
    task_id: task.id,
    attempt_id: TASK_ATTEMPT,
    snapshot_id: SNAP_ID,
    base_commit: HEAD,
    snapshot_commit: OLD,
    policy_version: 2,
    documentation_mode: 'SMART',
    token_budget: 24000,
    size_bytes: 18432,
    content_hash: 'c'.repeat(64),
    artifact_id: '01900000-0000-7000-f300-000000000002',
    created_at: AT,
    schema: 1,
    stale_policy: 'mark',
    role: 'developer',
    docs: 'allowed',
    budget: { max_bytes: 65536, max_files: 40, max_fragment_bytes: 8192 },
    cache_hit: false,
    fragments: [
      { section: 'code', kind: 'exact', path: 'src/billing/invoice.py', lines: [12, 30], commit: HEAD, sha256: null, bytes: 1200, stale: false, source_ref: 'Invoice' },
      { section: 'code', kind: 'dependency', path: 'src/billing/tax.py', lines: [1, 10], commit: OLD, sha256: null, bytes: 300, stale: true, source_ref: null },
      { section: 'tests', kind: 'task_relevance', path: 'tests/test_invoice.py', lines: [1, 40], commit: HEAD, sha256: null, bytes: 900, stale: false, source_ref: null },
      { section: 'docs', kind: 'docs', path: 'doc:fastapi/routing', lines: null, commit: null, sha256: null, bytes: 2000, stale: false, source_ref: 'fastapi 0.115' },
    ],
    omitted: [
      { section: 'code', kind: 'dependency', path: 'src/legacy/huge.py', commit: HEAD, reason: 'fragment_too_large' },
      { section: 'code', kind: 'recency', path: 'src/api/routes.py', commit: HEAD, reason: 'max_files' },
    ],
    warnings: ['docs_partial'],
    fragments_total: 4,
    omitted_total: 2,
  };
  api.route('GET', '/api/v1/tasks/{id}/attempts/' + TASK_ATTEMPT + '/context-pack', 'getAttemptContextPack', ({ id }) => (id === task.id ? { status: 200, body: pack } : problem(404, 'not_found')));
  api.route('GET', '/api/v1/projects/{id}/technologies', 'listTechnologies', () => ({
    status: 200,
    body: {
      items: [
        { technology_id: '01900000-0000-7000-f400-000000000001', name: 'FastAPI', ecosystem: 'pypi', technology_version_id: '01900000-0000-7000-f400-000000000002', version_label: '0.115', support_status: 'supported', detected_from: 'pyproject.toml', confirmed_by: null },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/documentation/sources', 'listDocumentationSources', () => ({
    status: 200,
    body: {
      items: [
        {
          id: '01900000-0000-7000-f400-000000000003',
          workspace_id: ws.id,
          technology_version_id: '01900000-0000-7000-f400-000000000002',
          source_type: 'url',
          uri_or_file_ref: 'https://fastapi.tiangolo.com/',
          trust_level: 'official',
          license_info: null,
          refresh_policy: 'manual',
          enabled: true,
          latest_snapshot: { id: '01900000-0000-7000-f400-000000000004', fetched_at: AT, status: 'READY' },
          created_at: AT,
        },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/projects/{id}/documentation-policy', 'getDocumentationPolicy', ({ id }) => ({
    status: 200,
    body: { project_id: id ?? '', documentation_mode: 'SMART', settings_version: 1 },
  }));
  api.route('GET', '/api/v1/search', 'searchWorkspace', ({ url }) => ({
    status: 200,
    body: {
      query: url.searchParams.get('q') ?? '',
      types: url.searchParams.getAll('types') as S['SearchResultType'][],
      skipped_types: [],
      items: [
        { type: 'task', id: task.id, title: 'Реализовать экспорт счетов', snippet: 'экспорт <b>счетов</b>', score: 0.9, created_at: AT, project_id: project.id, run_id: run.id, task_id: task.id, api_path: `/api/v1/tasks/${task.id}` },
      ],
      truncated: false,
    },
  }));
  return { ws, project, run, task, requests };
}

const codeUrl = (wsId: string, qs = '') => `/w/${wsId}/code${qs ? `?${qs}` : ''}`;

test('symbol search → open file at the symbol lines in Code Explorer; selectors and index status; axe light/dark', async ({ page, api }) => {
  const { ws, requests } = seed(api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id));
  await expect(page.getByRole('heading', { level: 1, name: 'Code Intelligence' })).toBeFocused();
  await expect(page.getByTestId('index-snapshot')).toContainText('a1b2c3d4e5f6');
  await expect(page.getByTestId('index-snapshot')).toContainText('готов');
  await expect(page.getByTestId('index-last-job')).toContainText('Выполняется');
  await expect(page.getByTestId('explorer-commit-note')).toContainText('последнего снимка индекса');
  await expect(page.getByTestId('repository-tree')).toBeVisible();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `code explorer (${scheme})`);
  }

  await page.getByRole('tab', { name: 'Символы' }).click();
  await page.getByRole('textbox', { name: /^Символ/ }).fill('Invoice');
  await page.getByLabel('Вид').selectOption('class');
  await page.getByRole('button', { name: 'Искать' }).click();
  const results = page.getByRole('list', { name: 'Символы по запросу «Invoice»' });
  await expect(results.getByRole('listitem')).toHaveCount(2);
  await expect(page.getByTestId('snapshot-line')).toContainText('128 файлов');
  await expect(page.getByTestId('snapshot-line')).toContainText(/2\s048 символов/);
  // Ранжирование сервера сохраняется; сигнатура — только текст.
  await expect(results.getByRole('listitem').first()).toContainText('Invoice');
  await expect(results.getByRole('listitem').first()).toContainText('релевантность 1,00');
  await expect(results.locator('script')).toHaveCount(0);
  expect(requests.symbols.at(-1)?.searchParams.get('kind')).toBe('class');
  expect(requests.symbols.at(-1)?.searchParams.get('q')).toBe('Invoice');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `symbols (${scheme})`);
  }
  await shot(page, 'code-symbols');

  await results.getByRole('link', { name: /^Открыть в файле\s?: Invoice —/ }).click();
  await expect(page).toHaveURL(/file=src%2Fbilling%2Finvoice\.py&line=12&end=30/);
  await expect(page.getByRole('tab', { name: 'Code Explorer' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('repository-file')).toContainText('src/billing/invoice.py');
  await expect(page.getByTestId('highlighted-lines')).toHaveText('Выделены строки 12–30.');
  await expect(page.getByTestId('line-highlight')).toBeVisible();
  await expect(page.getByTestId('repository-file-content')).toContainText('<script>alert(1)</script>');
  await expect(page.getByTestId('repository-file-content').locator('script')).toHaveCount(0);
  const scrolled = await page.getByTestId('repository-file-content').evaluate((el) => el.scrollTop);
  expect(scrolled).toBeGreaterThan(0);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `explorer at symbol (${scheme})`);
  }
  await shot(page, 'code-explorer-symbol');
});

test('dependency graph: React Flow canvas with distinct external nodes and truncated notice, accessible list view, list-only on 375', async ({ page, api }) => {
  const { ws, project, requests } = seed(api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id, `project=${project.id}&tab=graph`));
  await page.getByLabel(/^Корень: файл или каталог/).fill('src/billing/invoice.py');
  await page.getByLabel('Глубина').selectOption('2');
  await page.getByLabel('Направление').selectOption('both');
  await page.getByRole('button', { name: 'Построить граф' }).click();
  await expect(page.getByTestId('graph-summary')).toHaveText('Узлов: 5, связей: 4, внешних: 1 · глубина 2, в обе стороны.');
  const req = requests.deps.at(-1);
  expect(req?.searchParams.get('path')).toBe('src/billing/invoice.py');
  expect(req?.searchParams.get('depth')).toBe('2');
  expect(req?.searchParams.get('direction')).toBe('both');
  await expect(page.getByText('Граф неполон')).toBeVisible();
  const canvas = page.getByTestId('dependency-graph-canvas');
  await expect(canvas).toBeVisible();
  await expect(canvas.locator('[data-kind="external"]')).toHaveCount(1);
  await expect(canvas.locator('[data-kind="external"]')).toContainText('внешний');
  await expect(canvas.locator('[data-side="root"]')).toContainText('invoice.py');
  await expect(canvas.locator('[data-side="in"]')).toHaveCount(2);
  await canvas.getByRole('button', { name: 'Вписать граф' }).click();
  await canvas.getByRole('button', { name: 'Увеличить' }).click();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `dependency graph (${scheme})`);
  }
  await shot(page, 'code-graph');

  await page.getByRole('button', { name: 'Список' }).click();
  await expect(page.getByRole('button', { name: 'Список' })).toHaveAttribute('aria-pressed', 'true');
  const list = page.getByTestId('dependency-graph-list');
  await expect(list.getByRole('row')).toHaveCount(6);
  await expect(list.getByRole('row', { name: /requests/ })).toContainText('нет файла в репозитории');
  // Клавиатура: раскрыть связи и сделать узел корнем.
  const rootRow = list.getByRole('row', { name: /src\/billing\/invoice\.py/ });
  await rootRow.locator('summary').focus();
  await page.keyboard.press('Enter');
  await expect(rootRow).toContainText('Зависит от');
  await expect(rootRow).toContainText('requests');
  await list.getByRole('link', { name: /^Сделать корнем\s?: src\/billing\/tax\.py$/ }).focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => requests.deps.at(-1)?.searchParams.get('path')).toBe('src/billing/tax.py');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `dependency list (${scheme})`);
  }

  // Граф от символа (из поиска символов).
  await page.goto(codeUrl(ws.id, `project=${project.id}&tab=graph&gsym=${SYM_ID}&gsymname=Invoice`));
  await expect(page.getByTestId('graph-root-symbol')).toHaveText('Invoice');
  await expect.poll(() => requests.deps.at(-1)?.searchParams.get('symbol')).toBe(SYM_ID);

  await page.setViewportSize({ width: 375, height: 800 });
  await expect(page.getByTestId('graph-narrow-note')).toBeVisible();
  await expect(page.getByTestId('dependency-graph-canvas')).toHaveCount(0);
  await expect(page.getByTestId('dependency-graph-list')).toBeVisible();
  await expectNoHorizontalScroll(page);
  await shot(page, 'code-graph-375');
});

test('context pack manifest: run → task → attempt; sections, lines, stale, omitted with reasons; links into the explorer', async ({ page, api }) => {
  const { ws, project, run, task } = seed(api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id, `project=${project.id}&tab=context`));
  await expect(page.getByTestId('context-aggregates')).toContainText('В окне не собрано ни одного Context Pack.');
  await page.getByLabel('Запуск', { exact: true }).selectOption(run.id);
  await page.getByLabel('Задача', { exact: true }).selectOption(task.id);
  await expect(page.getByLabel('Попытка', { exact: true })).toHaveValue(TASK_ATTEMPT);
  const manifest = page.getByTestId('context-manifest');
  await expect(manifest).toBeVisible();
  await expect(manifest).toContainText('Снимок индекса старше commit задачи');
  await expect(page.getByTestId('pack-size')).toContainText('18 KB');
  await expect(page.getByTestId('pack-size')).toContainText('из 64 KB бюджета');
  await expect(manifest.locator('[data-section="code"]')).toContainText('src/billing/invoice.py');
  await expect(manifest.locator('[data-section="code"]')).toContainText('12–30');
  await expect(manifest.locator('[data-section="code"]').getByText('устарел', { exact: true })).toHaveCount(1);
  await expect(manifest.locator('[data-section="tests"]')).toContainText('tests/test_invoice.py');
  // Документация — не файл репозитория: без ссылки.
  await expect(manifest.locator('[data-section="docs"]')).toContainText('doc:fastapi/routing');
  await expect(manifest.locator('[data-section="docs"]').getByRole('link')).toHaveCount(0);
  const omitted = page.getByTestId('context-omitted');
  await expect(omitted).toContainText('Пропущено: 2');
  await expect(omitted).toContainText('фрагмент слишком большой');
  await expect(omitted).toContainText('предел числа файлов');
  await expect(manifest).toContainText('docs_partial');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `context pack (${scheme})`);
  }
  await shot(page, 'code-context-pack');

  await manifest.locator('[data-section="code"]').getByRole('link', { name: /src\/billing\/tax\.py/ }).click();
  await expect(page.getByRole('tab', { name: 'Code Explorer' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('explorer-commit-note')).toContainText('ffffffffffff');
  await expect(page.getByTestId('highlighted-lines')).toHaveText('Выделены строки 1–1.');
  await expect(page.getByTestId('highlighted-lines')).toBeVisible();

  // Ссылка из Mission Control (без проекта): проект берётся из run.
  await page.goto(codeUrl(ws.id, `tab=context&run=${run.id}&task=${task.id}&attempt=${TASK_ATTEMPT}`));
  await expect(page.getByTestId('context-manifest')).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`project=${project.id}`));
});

test('indexing jobs: statuses, attempts, timings, redacted error as text, status filter', async ({ page, api }) => {
  const { ws, project, requests } = seed(api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id, `project=${project.id}&tab=indexing`));
  const table = page.getByTestId('indexing-jobs');
  await expect(table.locator('tbody tr')).toHaveCount(3);
  await expect(table.locator('[data-job-status="RUNNING"]')).toContainText('не завершено');
  await expect(table.locator('[data-job-status="FAILED"]')).toContainText('Ошибка');
  await expect(table.locator('[data-job-status="FAILED"]')).toContainText('42 с');
  await expect(table.locator('[data-job-status="SUCCEEDED"]')).toContainText('2 мин 5 с');
  await expect(table.locator('[data-job-status="SUCCEEDED"]')).toContainText('файлов: 128, символов: 2048');
  await expect(page.getByTestId('job-error')).toHaveText('tree-sitter parse failed: <path redacted> token=[REDACTED]');
  await page.getByLabel('Статус', { exact: true }).selectOption('FAILED');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  expect(requests.jobs.at(-1)?.searchParams.get('status')).toBe('FAILED');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `indexing jobs (${scheme})`);
  }
  await shot(page, 'code-indexing');
});

test('no index yet: clear state with how to index (summary, symbols, graph), no zeros; axe light/dark', async ({ page, api }) => {
  const { ws, project } = seed(api, { indexed: false });
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id, `project=${project.id}`));
  await expect(page.getByTestId('index-snapshot')).toHaveText('не построен');
  await expect(page.getByTestId('index-last-job')).toContainText('заданий нет');
  const state = page.getByTestId('no-index').first();
  await expect(state).toContainText('Индекс кода ещё не построен');
  await expect(state).toContainText('aicd-worker index');
  await expect(state.getByRole('link', { name: 'Репозитории проекта' })).toHaveAttribute('href', `/w/${ws.id}/projects/${project.id}?tab=repositories`);
  await expect(page.getByTestId('explorer-commit-note')).toContainText('Снимка индекса нет');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `no index (${scheme})`);
  }
  await shot(page, 'code-no-index');

  await page.getByRole('tab', { name: 'Символы' }).click();
  await page.getByRole('textbox', { name: /^Символ/ }).fill('Invoice');
  await page.getByRole('button', { name: 'Искать' }).click();
  await expect(page.getByTestId('symbol-search').getByTestId('no-index')).toBeVisible();
  await expect(page.getByTestId('symbol-count')).toHaveCount(0);
  await page.getByRole('tab', { name: 'Граф зависимостей' }).click();
  await page.getByLabel(/^Корень: файл или каталог/).fill('src');
  await page.getByRole('button', { name: 'Построить граф' }).click();
  await expect(page.getByTestId('dependency-graph').getByTestId('no-index')).toBeVisible();
  await page.getByRole('tab', { name: 'Индексация' }).click();
  await expect(page.getByText('Заданий индексации нет')).toBeVisible();
});

test('knowledge, git history (view files at commit), workspace search; keyboard tabs', async ({ page, api }) => {
  const { ws, project } = seed(api);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id, `project=${project.id}&tab=knowledge`));
  const knowledge = page.getByTestId('knowledge-panel');
  await expect(knowledge).toContainText('FastAPI');
  await expect(knowledge).toContainText('https://fastapi.tiangolo.com/');
  await expect(knowledge.getByRole('link', { name: 'Открыть «Документация»' })).toHaveAttribute('href', `/w/${ws.id}/knowledge`);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `knowledge (${scheme})`);
  }

  // Вкладки — стрелками (APG Tabs).
  await page.getByRole('tab', { name: 'Знания' }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'История' })).toBeFocused();
  await expect(page.getByRole('tab', { name: 'История' })).toHaveAttribute('aria-selected', 'true');
  const history = page.getByTestId('history-panel');
  await expect(history).toContainText('Merge feature/tax');
  await expect(history).toContainText('проиндексирован');
  await expect(history).toContainText('слияние');
  await page.getByLabel('Ветка').selectOption('feature/export');
  await expect(history).toContainText('Экспорт: черновик <b>не HTML</b>');
  await expect(history.locator('b')).toHaveCount(0);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `history (${scheme})`);
  }
  await history.getByRole('link', { name: new RegExp(`^Файлы в этом commit\\s?: ${OLD.slice(0, 12)}$`) }).click();
  await expect(page.getByRole('tab', { name: 'Code Explorer' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('explorer-commit-note')).toContainText('выбранном commit');
  await expect(page.getByTestId('code-commit')).toHaveValue(OLD);

  await page.getByRole('tab', { name: 'Поиск' }).click();
  await page.getByLabel('Запрос').fill('экспорт');
  await page.getByRole('button', { name: 'Найти' }).click();
  const search = page.getByTestId('code-search');
  await expect(search.getByRole('link', { name: 'Реализовать экспорт счетов' })).toBeVisible();
  await expect(search).toContainText('экспорт <b>счетов</b>');
  const searchReq = api.requests.filter((r) => r.operationId === 'searchWorkspace').at(-1);
  expect(searchReq).toBeTruthy();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `search (${scheme})`);
  }
});

for (const width of [375, 768, 1366, 1920]) {
  test(`no horizontal overflow at ${width}px on every tab, both themes`, async ({ page, api }) => {
    const { ws, project, run, task } = seed(api);
    await page.setViewportSize({ width, height: 900 });
    await login(page, api);
    const tabs: [string, string][] = [
      ['explorer', `file=src%2Fbilling%2Finvoice.py&line=12&end=30`],
      ['symbols', 'q=Invoice'],
      ['graph', 'groot=src%2Fbilling%2Finvoice.py&depth=2'],
      ['context', `run=${run.id}&task=${task.id}&attempt=${TASK_ATTEMPT}`],
      ['indexing', ''],
      ['knowledge', ''],
      ['history', ''],
      ['search', ''],
    ];
    for (const [tab, extra] of tabs) {
      await page.goto(codeUrl(ws.id, `project=${project.id}&tab=${tab}${extra ? `&${extra}` : ''}`));
      await expect(page.getByRole('tab', { selected: true })).toBeVisible();
      if (tab === 'symbols') await expect(page.getByTestId('symbol-results')).toBeVisible();
      if (tab === 'graph') await expect(page.getByTestId('graph-summary')).toBeVisible();
      if (tab === 'context') await expect(page.getByTestId('context-manifest')).toBeVisible();
      if (tab === 'explorer') await expect(page.getByTestId('highlighted-lines')).toBeVisible();
      for (const scheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoHorizontalScroll(page);
      }
      if (width === 375 || width === 1920) await shot(page, `code-${tab}-${width}`);
    }
  });
}

test('reduced motion: graph renders without animations', async ({ page, api }) => {
  const { ws, project } = seed(api, { truncated: false });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await page.goto(codeUrl(ws.id, `project=${project.id}&tab=graph&groot=src%2Fbilling%2Finvoice.py`));
  await expect(page.getByTestId('dependency-graph-canvas')).toBeVisible();
  await expect(page.getByText('Граф неполон')).toHaveCount(0);
  const running = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && a.effect?.getComputedTiming().iterations === Infinity).length);
  expect(running).toBe(0);
});
