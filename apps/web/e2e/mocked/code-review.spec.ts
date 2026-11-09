/**
 * F6: Code Review Workspace (`projects/:pid/review/:runId`, ADR-0015/B5): diff из артефакта git_diff (Monaco
 * side-by-side — лениво; текстовый вид с комментариями), inline-треды (создать/ответить/править своё с историей/
 * resolve/unresolve, устаревший тред), находки и проверки (D-8), отправка ревью (Approve — существующий approval,
 * step-up; Request changes — поручение с impact preview), RBAC (reviewer без REQUEST_CHANGES, viewer — только
 * чтение), live `review.comment`, клавиатура, axe в обеих темах, 375/768/1366/1920. Комментарии — только текст.
 */
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { Page } from '@playwright/test';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type MockRequest } from '../support/mockApi';
import { focusDocumentStart, login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];
const AT = (s: number) => `2026-11-08T09:${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}Z`;
const ME = '01900000-0000-7000-8000-0000000000aa';
const OTHER: S['UserRef'] = { id: '01900000-0000-7000-8000-0000000000bb', display_name: 'Борис Ревьюеров' };
const HEAD = 'b'.repeat(40);
const BASE = 'c'.repeat(40);
const OLD = 'a'.repeat(40);
const REPO_ID = '01900000-0000-7000-f100-000000000001';
const DIFF_ARTIFACT = '01900000-0000-7000-f100-0000000000a1';
const EV_DIFF = '01900000-0000-7000-f100-0000000000e1';
const EV_TESTS = '01900000-0000-7000-f100-0000000000e2';
const APPROVAL = '01900000-0000-7000-f100-00000000000a';
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

const DIFF = `diff --git a/src/export.ts b/src/export.ts
index 1111111..2222222 100644
--- a/src/export.ts
+++ b/src/export.ts
@@ -1,3 +1,4 @@
 export function exportReport() {
-  return toJson();
+  return toCsv();
+  // CSV по умолчанию
 }
diff --git a/src/csv.ts b/src/csv.ts
new file mode 100644
--- /dev/null
+++ b/src/csv.ts
@@ -0,0 +1,2 @@
+export const SEP = ';';
+export function toCsv() { return ''; }
`;

async function shot(page: Page, name: string): Promise<void> {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: join(SHOTS, `${name}.png`), fullPage: true });
}

let seq = 0;
const uid = (prefix: string) => `01900000-0000-7000-${prefix}-${String(++seq).padStart(12, '0')}`;

interface Opts {
  role?: S['WorkspaceRole'];
  approvals?: 'decidable' | 'none' | 'foreign';
  noDiff?: boolean;
  stepUp?: boolean;
}

function seed(api: MockApi, opts: Opts = {}) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', opts.role ?? 'developer');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'RUNNING', version: 3, objective: 'Добавить экспорт отчётов в CSV' });
  const task = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING', current_provider: 'codex' });
  const comment = (thread: string, author: S['UserRef'], body: string, created: number): S['ReviewComment'] => ({
    id: uid('f200'),
    thread_id: thread,
    author,
    body,
    revision: 1,
    created_at: AT(created),
    edited_at: null,
    can_edit: author.id === ME,
  });
  const thread = (anchor: Partial<S['ReviewAnchor']>, author: S['UserRef'], body: string, created: number): S['ReviewThread'] => {
    const id = uid('f300');
    return {
      id,
      project_id: project.id,
      repository_id: REPO_ID,
      run_id: run.id,
      task_id: task.id,
      finding_id: null,
      anchor: { commit_sha: HEAD, base_commit_sha: BASE, side: 'head', file_path: 'src/export.ts', start_line: 2, end_line: 2, ...anchor },
      status: 'OPEN',
      outdated: null,
      current_commit_sha: HEAD,
      resolved_by: null,
      resolved_at: null,
      created_by: author,
      created_at: AT(created),
      updated_at: AT(created),
      version: 1,
      comment_count: 1,
      comments: [comment(id, author, body, created)],
    };
  };
  const threads: S['ReviewThread'][] = [
    thread({ side: 'head', start_line: 2, end_line: 2 }, OTHER, 'Почему не JSON? <script>alert(1)</script>', 10),
    thread({ commit_sha: OLD, start_line: 1, end_line: 1 }, { id: ME, display_name: api.user.display_name }, 'Старое замечание к прошлой попытке', 5),
  ];
  const revisions = new Map<string, S['ReviewCommentRevision'][]>();
  const submissions: S['ReviewSubmission'][] = [];

  const artifact = (id: string, kind: S['ArtifactKind'], size: number): S['ArtifactRef'] => ({
    id,
    kind,
    sha256: 'd'.repeat(64),
    size_bytes: size,
    mime_type: 'text/plain',
    download_url: `/api/v1/artifacts/${id}/download`,
  });
  const evidence: S['Evidence'][] = [
    {
      id: EV_TESTS,
      run_id: run.id,
      task_id: task.id,
      kind: 'test_report',
      producer: 'gate:UNIT_TESTS',
      verification_status: 'VERIFIED',
      commit_sha: HEAD,
      artifact: artifact('01900000-0000-7000-f100-0000000000a2', 'test_report', 100),
      created_at: AT(20),
    },
    ...(opts.noDiff
      ? []
      : [
          {
            id: EV_DIFF,
            run_id: run.id,
            task_id: task.id,
            kind: 'git_diff',
            producer: 'verification-engine',
            verification_status: 'VERIFIED' as const,
            commit_sha: HEAD,
            artifact: artifact(DIFF_ARTIFACT, 'git_diff', DIFF.length),
            created_at: AT(21),
          },
        ]),
  ];
  const gates: S['GateExecution'][] = [
    { id: '01900000-0000-7000-f400-000000000001', gate_id: '01900000-0000-7000-f400-000000000011', gate_type: 'UNIT_TESTS', required: true, task_id: task.id, status: 'PASS', evidence_id: EV_TESTS, commit_sha: HEAD },
    { id: '01900000-0000-7000-f400-000000000002', gate_id: '01900000-0000-7000-f400-000000000012', gate_type: 'SAST', required: true, task_id: task.id, status: 'FAIL', evidence_id: null, commit_sha: HEAD },
    { id: '01900000-0000-7000-f400-000000000003', gate_id: '01900000-0000-7000-f400-000000000013', gate_type: 'SECRETS', required: true, task_id: task.id, status: 'ERROR', evidence_id: null, commit_sha: HEAD, error_code: 'TOOL_UNAVAILABLE' },
  ];
  const finding: S['Finding'] = {
    id: '01900000-0000-7000-f500-000000000001',
    task_id: task.id,
    gate_execution_id: gates[1]!.id,
    severity: 'high',
    rule_id: 'sast.csv-injection',
    title: 'CSV injection: значение начинается с = <img src=x>',
    cwe_id: 'CWE-1236',
    file_path: 'src/export.ts',
    line: 2,
    status: 'OPEN',
    evidence_id: null,
    run_id: run.id,
    source: 'gate',
    version: 1,
    created_at: AT(22),
    updated_at: AT(22),
  };
  const approval: S['Approval'] = {
    id: APPROVAL,
    workspace_id: ws.id,
    run_id: run.id,
    task_id: task.id,
    action_type: 'MANUAL_REVIEW',
    status: 'PENDING',
    risk_level: 'medium',
    summary: 'Ручное ревью изменений экспорта',
    requested_by: { actor_type: 'agent', role: 'reviewer', user: null },
    can_decide: opts.approvals !== 'foreign',
    created_at: AT(23),
    version: 2,
  };

  api.route('GET', '/api/v1/projects/{id}/repositories', 'listRepositories', () => ({
    status: 200,
    body: {
      items: [{ id: REPO_ID, project_id: project.id, provider: 'local', remote_url: null, default_branch: 'main', local_root_ref: 'repos/payments', last_synced_at: AT(0), latest_index_snapshot: null }],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/projects/{id}/worktrees', 'listProjectWorktrees', ({ url }) => {
    expect(url.searchParams.get('run_id')).toBe(run.id);
    return {
      status: 200,
      body: {
        items: [
          {
            id: '01900000-0000-7000-f600-000000000001',
            repository_id: REPO_ID,
            run_id: run.id,
            task_id: task.id,
            branch_name: 'aicd/run/export',
            base_commit_sha: BASE,
            head_commit_sha: HEAD,
            status: 'ACTIVE',
            created_at: AT(1),
            updated_at: AT(2),
            removed_at: null,
          },
        ],
        next_cursor: null,
      },
    };
  });
  api.route('GET', '/api/v1/runs/{id}/evidence', 'listRunEvidence', () => ({ status: 200, body: { items: evidence, next_cursor: null } }));
  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', () => ({
    status: 200,
    body: { run_id: run.id, quality_profile: 'strict', verdict: 'RUNNING', gate_executions: gates, findings_summary: {}, findings: [], findings_total: 1 },
  }));
  api.route('GET', '/api/v1/runs/{id}/findings', 'listRunFindings', () => ({ status: 200, body: { items: [finding], next_cursor: null } }));
  api.route('GET', '/api/v1/approvals', 'listApprovals', ({ url }) => {
    const items = opts.approvals === 'none' ? [] : [approval].filter((a) => a.status === 'PENDING');
    return { status: 200, body: { items: url.searchParams.get('run_id') === run.id || url.searchParams.get('decidable_by_me') ? items : [], next_cursor: null } };
  });

  const view = (th: S['ReviewThread'], headSha: string | null): S['ReviewThread'] => ({
    ...th,
    outdated: headSha ? th.anchor.commit_sha !== headSha : null,
    comment_count: th.comments.length,
  });
  const threadOf = (req: MockRequest) => threads.find((th) => req.url.pathname.split('/')[4] === th.id);
  api.route('GET', `/api/v1/projects/{id}/repositories/${REPO_ID}/review-threads`, 'listReviewThreads', ({ url }) => {
    const taskFilter = url.searchParams.get('task_id');
    const items = threads.filter((th) => th.run_id === url.searchParams.get('run_id') && (!taskFilter || th.task_id === taskFilter));
    return { status: 200, body: { items: items.map((th) => view(th, url.searchParams.get('head_sha'))), next_cursor: null } };
  });
  api.route('POST', `/api/v1/projects/{id}/repositories/${REPO_ID}/review-threads`, 'createReviewThread', ({ body }) => {
    const b = body as S['ReviewThreadCreateRequest'];
    const th = thread({ ...b.anchor, base_commit_sha: b.anchor.base_commit_sha ?? null }, { id: ME, display_name: api.user.display_name }, b.body, 40);
    th.task_id = b.task_id ?? null;
    threads.push(th);
    return { status: 201, body: view(th, HEAD), headers: { ETag: '"v1"', Location: `/api/v1/review-threads/${th.id}` } };
  });
  api.route('POST', '/api/v1/review-threads/{id}/comments', 'replyReviewThread', (req) => {
    const th = threadOf(req);
    if (!th) return problem(404, 'not_found');
    const c = comment(th.id, { id: ME, display_name: api.user.display_name }, (req.body as S['ReviewCommentCreateRequest']).body, 50);
    th.comments.push(c);
    return { status: 201, body: c, headers: { ETag: '"v1"', Location: `/api/v1/review-comments/${c.id}` } };
  });
  for (const action of ['resolve', 'unresolve'] as const) {
    api.route('POST', `/api/v1/review-threads/{id}/${action}`, action === 'resolve' ? 'resolveReviewThread' : 'unresolveReviewThread', (req) => {
      const th = threadOf(req);
      if (!th) return problem(404, 'not_found');
      if (req.headers['if-match'] !== `"v${th.version}"`) return problem(412, 'precondition_failed');
      if ((action === 'resolve') === (th.status === 'RESOLVED')) return problem(409, 'invalid_state_transition', { current_status: th.status });
      th.status = action === 'resolve' ? 'RESOLVED' : 'OPEN';
      th.resolved_by = action === 'resolve' ? { id: ME, display_name: api.user.display_name } : null;
      th.resolved_at = action === 'resolve' ? AT(60) : null;
      th.version += 1;
      return { status: 200, body: view(th, null), headers: { ETag: `"v${th.version}"` } };
    });
  }
  const commentOf = (req: MockRequest) => threads.flatMap((th) => th.comments).find((c) => req.url.pathname.split('/')[4] === c.id);
  api.route('PATCH', '/api/v1/review-comments/{id}', 'editReviewComment', (req) => {
    const c = commentOf(req);
    if (!c) return problem(404, 'not_found');
    if (!c.can_edit) return problem(403, 'forbidden');
    if (req.headers['if-match'] !== `"v${c.revision}"`) return problem(412, 'precondition_failed');
    revisions.set(c.id, [...(revisions.get(c.id) ?? []), { revision: c.revision, body: c.body, authored_at: c.edited_at ?? c.created_at, superseded_at: AT(70) }]);
    c.body = (req.body as S['ReviewCommentUpdateRequest']).body;
    c.revision += 1;
    c.edited_at = AT(70);
    return { status: 200, body: c, headers: { ETag: `"v${c.revision}"` } };
  });
  api.route('GET', '/api/v1/review-comments/{id}/revisions', 'listReviewCommentRevisions', (req) => {
    const c = threads.flatMap((th) => th.comments).find((x) => req.url.pathname.split('/')[4] === x.id);
    return c ? { status: 200, body: { comment: c, items: revisions.get(c.id) ?? [] } } : problem(404, 'not_found');
  });
  api.route('GET', '/api/v1/runs/{id}/reviews', 'listReviews', () => ({ status: 200, body: { items: [...submissions].reverse(), next_cursor: null } }));
  let submits = 0;
  const directives: S['Directive'][] = [];
  api.route('POST', '/api/v1/runs/{id}/reviews', 'submitReview', ({ body }) => {
    submits += 1;
    const b = body as S['ReviewSubmitRequest'];
    if (b.expected_run_version !== run.version) return problem(409, 'version_mismatch', { current_version: run.version });
    if (opts.role === 'reviewer' && b.verdict === 'REQUEST_CHANGES') return problem(403, 'forbidden');
    if (b.verdict === 'APPROVE') {
      if (b.approval_id !== approval.id || approval.status !== 'PENDING') return problem(422, 'validation_error');
      if (opts.stepUp && submits === 1) return problem(403, 'step_up_required', { max_age_seconds: 600 });
      approval.status = 'APPROVED';
    }
    let directive: S['Directive'] | null = null;
    if (b.verdict === 'REQUEST_CHANGES') {
      directive = {
        id: uid('f700'),
        run_id: run.id,
        chat_message_id: uid('f800'),
        target: b.task_id ? { type: 'task', task_id: b.task_id } : { type: 'run' },
        directive_type: 'CHANGE_REQUEST',
        message: 'Ревью: замечания',
        status: 'IMPACT_ANALYSIS_PENDING',
        impact_status: 'PENDING',
        impact_summary: null,
        impacts: [],
        requires_confirmation: false,
        approval_id: null,
        delivery: { state: 'NOT_DISPATCHED', acknowledgements: [] },
        completion: { state: 'NOT_STARTED', resulting_task_ids: [] },
        submitted_by: { id: ME, display_name: api.user.display_name },
        created_at: AT(80),
        updated_at: AT(80),
        version: 0,
      };
      directives.push(directive);
    }
    const s: S['ReviewSubmission'] = {
      id: uid('f900'),
      run_id: run.id,
      task_id: b.task_id ?? null,
      verdict: b.verdict,
      body: b.body ?? null,
      thread_ids: b.thread_ids ?? [],
      approval_id: b.approval_id ?? null,
      approval_status: b.verdict === 'APPROVE' ? 'APPROVED' : null,
      directive_id: directive?.id ?? null,
      directive_status: directive?.status ?? null,
      submitted_by: { id: ME, display_name: api.user.display_name },
      created_at: AT(80),
    };
    submissions.push(s);
    return { status: 202, body: s };
  });
  api.route('GET', '/api/v1/runs/{id}/directives/[^/]+', 'getDirective', (req) => {
    const d = directives.find((x) => req.url.pathname.split('/')[6] === x.id);
    return d ? { status: 200, body: d, headers: { ETag: `"v${d.version}"` } } : problem(404, 'not_found');
  });
  api.route('POST', '/api/v1/auth/reauthenticate', 'reauthenticate', ({ body }) =>
    (body as S['ReauthenticateRequest']).password === 'correct horse battery staple'
      ? { status: 200, body: { ...api.session(), reauthenticated_at: AT(90) } }
      : problem(403, 'reauthentication_failed'),
  );
  return { ws, project, run, task, threads, approval, url: `/w/${ws.id}/projects/${project.id}/review/${run.id}?task=${task.id}`, comment };
}

/** downloadArtifact (как в tasks.spec): потоковая отдача, маршрут после MockApi.install имеет приоритет. */
async function serveArtifacts(page: Page): Promise<string[]> {
  const fetched: string[] = [];
  await page.route('**/api/v1/artifacts/*/download', async (route) => {
    fetched.push(route.request().url());
    await route.fulfill({ status: 200, contentType: 'application/octet-stream', headers: { 'Cache-Control': 'no-store' }, body: DIFF });
  });
  return fetched;
}

async function openText(page: Page, url: string) {
  await page.goto(`${url}&view=text`);
  await expect(page.getByTestId('diff-text')).toBeVisible();
}

test('code review: Monaco side-by-side loads lazily; files, inline/outdated threads (text only), findings and checks (D-8); axe light/dark', async ({ page, api }) => {
  const { url, threads } = seed(api);
  const monaco: string[] = [];
  const external: string[] = [];
  page.on('request', (r) => {
    if (/MonacoDiff|editor\.worker/.test(r.url())) monaco.push(r.url());
    if (!r.url().startsWith('http://127.0.0.1')) external.push(r.url());
  });
  const fetched = await serveArtifacts(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  expect(monaco, 'Monaco is not part of the initial bundle').toEqual([]);
  await page.goto(url);
  await expect(page.getByRole('heading', { level: 1, name: 'Ревью кода' })).toBeFocused();
  await expect(page.getByTestId('review-run-bar')).toContainText('Добавить экспорт отчётов в CSV');
  const files = page.getByTestId('review-files');
  await expect(files.getByRole('button')).toHaveCount(2);
  await expect(files.getByRole('button').first()).toContainText('+2 −1');
  await expect(files.getByRole('button').first()).toContainText('1 открытое обсуждение');
  await expect(page.getByRole('button', { name: 'Рядом' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('monaco-diff').locator('.monaco-diff-editor')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('monaco-diff')).toContainText('toCsv');
  expect(monaco.length).toBeGreaterThan(0);
  expect(fetched).toEqual([expect.stringContaining(`/api/v1/artifacts/${DIFF_ARTIFACT}/download`)]);
  // В режиме Monaco обсуждения файла — списком под редактором (и тот, что к прошлому commit).
  await expect(page.getByRole('heading', { name: 'Обсуждения файла (2)' })).toBeVisible();
  const listReq = api.requests.find((r) => r.operationId === 'listReviewThreads');
  expect(listReq).toBeTruthy();
  await shot(page, 'review-split-1366');

  await page.getByRole('button', { name: 'Текст с комментариями' }).click();
  const table = page.getByRole('table', { name: 'Изменения файла src/export.ts' });
  await expect(table).toBeVisible();
  const inline = table.locator(`[data-thread-id="${threads[0]!.id}"]`);
  await expect(inline).toContainText('Почему не JSON? <script>alert(1)</script>');
  await expect(inline.locator('script')).toHaveCount(0);
  await expect(inline).toContainText('Борис Ревьюеров');
  await expect(inline.getByRole('button', { name: 'Изменить' })).toHaveCount(0); // чужой комментарий
  await expect(table.getByTestId('row-finding-marker')).toContainText('sast.csv-injection');
  const outdated = page.locator(`[data-thread-id="${threads[1]!.id}"]`);
  await expect(outdated).toHaveAttribute('data-outdated', 'true');
  await expect(outdated.getByTestId('thread-outdated')).toHaveText('Устарело');
  await expect(outdated).toContainText('aaaaaaa');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `code review text view (${scheme})`);
  }
  await shot(page, 'review-text-1366');

  await page.getByRole('tab', { name: /^Находки/ }).click();
  const findings = page.getByTestId('review-findings');
  await expect(findings).toContainText('Высокая');
  await expect(findings).toContainText('CSV injection: значение начинается с = <img src=x>');
  await expect(findings.locator('img')).toHaveCount(0);
  await expect(findings.locator('[data-verification]')).toHaveAttribute('data-verification', 'FAIL');
  await page.getByRole('tab', { name: 'Тесты и безопасность' }).click();
  await expect(page.getByTestId('checks-tests').locator('[data-verification]')).toHaveAttribute('data-verification', 'PASS');
  const security = page.getByTestId('checks-security');
  await expect(security.locator('[data-gate="SAST"] [data-verification]')).toHaveAttribute('data-verification', 'FAIL');
  await expect(security.locator('[data-gate="SECRETS"] [data-verification]')).toHaveText(/Заблокировано/);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `code review checks (${scheme})`);
  }
  expect(external, 'no CDN').toEqual([]);
});

test('threads: add on a line, reply, edit own comment with history, resolve and unresolve (If-Match)', async ({ page, api }) => {
  const { url, threads, task, run } = seed(api);
  await serveArtifacts(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await openText(page, url);

  await page.getByRole('button', { name: 'Комментировать строку 3 (новая версия)' }).click();
  const form = page.getByTestId('new-thread-form');
  await expect(form.getByLabel('Комментарий')).toBeFocused();
  await form.getByRole('button', { name: 'Добавить комментарий' }).click();
  await expect(form.getByText('Введите текст комментария.')).toBeVisible();
  await form.getByLabel('Комментарий').fill('Добавьте тест на разделитель');
  await form.getByRole('button', { name: 'Добавить комментарий' }).click();
  await expect(form).toHaveCount(0);
  const created = api.requests.find((r) => r.operationId === 'createReviewThread');
  expect(created?.body).toEqual({
    anchor: { commit_sha: HEAD, base_commit_sha: BASE, side: 'head', file_path: 'src/export.ts', start_line: 3, end_line: 3 },
    body: 'Добавьте тест на разделитель',
    run_id: run.id,
    task_id: task.id,
  });
  expect(created?.headers['idempotency-key']).toBeTruthy();
  const mine = page.locator(`[data-thread-id="${threads[2]!.id}"]`);
  await expect(mine).toBeFocused();
  await expect(page.getByTestId('review-live')).toHaveText('Комментарий добавлен: src/export.ts:3');

  // Правка своего комментария: If-Match — revision, история правок.
  await mine.getByRole('button', { name: 'Изменить' }).click();
  await mine.getByLabel('Изменить комментарий').fill('Добавьте тест на разделитель «;»');
  await mine.getByRole('button', { name: 'Сохранить' }).click();
  await expect(mine.getByTestId('comment-body')).toHaveText('Добавьте тест на разделитель «;»');
  await expect(mine.getByTestId('comment-edited')).toBeVisible();
  const edit = api.requests.find((r) => r.operationId === 'editReviewComment');
  expect(edit?.headers['if-match']).toBe('"v1"');
  await mine.getByRole('button', { name: 'История правок' }).click();
  await expect(mine.getByTestId('comment-history')).toContainText('Редакция 1');
  await expect(mine.getByTestId('comment-history')).toContainText('Добавьте тест на разделитель');

  // Ответ в чужом треде и resolve / unresolve.
  const other = page.locator(`[data-thread-id="${threads[0]!.id}"]`);
  await other.getByLabel('Ответить').fill('JSON остаётся для API');
  await other.getByLabel('Ответить').press('Control+Enter');
  await expect(other.getByTestId('comment-body')).toHaveCount(2);
  await expect(other.getByTestId('comment-body').last()).toHaveText('JSON остаётся для API');
  await other.getByRole('button', { name: 'Отметить решённым' }).click();
  await expect(other).toHaveAttribute('data-status', 'RESOLVED');
  await expect(other).toContainText('Решено: Анна Петрова');
  expect(api.requests.find((r) => r.operationId === 'resolveReviewThread')?.headers['if-match']).toBe('"v1"');
  await other.getByRole('button', { name: 'Переоткрыть' }).click();
  await expect(other).toHaveAttribute('data-status', 'OPEN');
  expect(api.requests.find((r) => r.operationId === 'unresolveReviewThread')?.headers['if-match']).toBe('"v2"');
  await expectNoSeriousA11yViolations(page, 'code review threads');
});

test('submit review: Approve decides the existing pending approval (shown) with step-up; Request changes → CHANGE_REQUEST impact preview', async ({ page, api }) => {
  const { url, approval, threads, task } = seed(api, { stepUp: true });
  await serveArtifacts(page);
  await page.setViewportSize({ width: 1920, height: 1000 });
  await login(page, api);
  await openText(page, url);
  await page.getByRole('tab', { name: 'Отправить ревью' }).click();
  const approve = page.getByRole('radio', { name: 'Утвердить' });
  await expect(approve).toBeEnabled();
  await approve.check();
  const target = page.getByTestId('approve-target');
  await expect(target).toContainText('Утверждается');
  await expect(target).toContainText('Ручная проверка');
  await expect(target).toContainText('Ручное ревью изменений экспорта');
  await expect(target.getByRole('link', { name: 'Открыть одобрение' })).toHaveAttribute('href', expect.stringContaining(approval.id));
  await expectNoSeriousA11yViolations(page, 'submit approve');
  await page.getByRole('button', { name: 'Утвердить', exact: true }).click();
  const stepUp = page.getByRole('dialog', { name: 'Подтвердите пароль' });
  await expect(stepUp).toBeVisible();
  await stepUp.getByLabel('Пароль').fill('correct horse battery staple');
  await stepUp.getByRole('button', { name: 'Подтвердить' }).click();
  await expect(page.getByTestId('review-result')).toContainText('одобрение решено');
  await expect(page.getByTestId('review-result')).toContainText('Одобрено');
  const sent = api.requests.filter((r) => r.operationId === 'submitReview');
  expect(sent).toHaveLength(2);
  expect(sent[1]?.body).toEqual({ verdict: 'APPROVE', expected_run_version: 3, requires_impact_preview: true, approval_id: approval.id });
  expect(sent[1]?.headers['idempotency-key']).toBe(sent[0]?.headers['idempotency-key']);
  await expect(page.getByTestId('review-submissions')).toContainText('Утвердить');
  // Approval решён — «Утвердить» больше нет: утверждать нечего.
  await expect(page.getByRole('radio', { name: 'Утвердить' })).toBeDisabled();
  await expect(page.getByText('Нет ожидающего одобрения этого запуска — утверждать нечего.')).toBeVisible();

  await page.getByRole('radio', { name: 'Запросить изменения' }).check();
  await expect(page.getByTestId('request-changes-target')).toContainText('Это поручение агентам');
  await expect(page.getByRole('radio', { name: 'Задаче «Реализовать экспорт»' })).toBeChecked();
  await expect(page.getByRole('checkbox', { name: 'src/export.ts:2' })).toBeChecked();
  await expect(page.getByRole('checkbox', { name: 'Показать анализ влияния перед выполнением' })).toBeChecked();
  await page.getByLabel('Общий комментарий (необязательно)').fill('Нужен тест на CSV injection');
  await page.getByRole('button', { name: 'Запросить изменения', exact: true }).click();
  const track = page.getByRole('dialog', { name: 'Поручение «Изменение (CHANGE_REQUEST)»' });
  await expect(track).toBeVisible();
  await expect(track.locator('[data-step="impact"]')).toHaveAttribute('data-state', 'current');
  const rc = api.requests.filter((r) => r.operationId === 'submitReview').at(-1);
  expect(rc?.body).toEqual({
    verdict: 'REQUEST_CHANGES',
    expected_run_version: 3,
    requires_impact_preview: true,
    body: 'Нужен тест на CSV injection',
    thread_ids: threads.filter((th) => th.status === 'OPEN').map((th) => th.id),
    task_id: task.id,
  });
  await expectNoSeriousA11yViolations(page, 'request changes track dialog');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('review-result')).toContainText('поручение принято (ещё не выполнено)');
  await expect(page.getByRole('link', { name: 'Ход поручения в Mission Control' })).toHaveAttribute('href', /view=directives/);
});

test('RBAC: reviewer cannot request changes; viewer is read-only; no git_diff → empty state', async ({ page, api }) => {
  const { url, threads } = seed(api, { role: 'reviewer', approvals: 'foreign' });
  await serveArtifacts(page);
  await login(page, api);
  await openText(page, url);
  await expect(page.getByRole('button', { name: /^Комментировать строку/ }).first()).toBeVisible();
  await page.getByRole('tab', { name: 'Отправить ревью' }).click();
  await expect(page.getByRole('radio', { name: 'Запросить изменения' })).toBeDisabled();
  await expect(page.getByRole('radio', { name: 'Утвердить' })).toBeDisabled();
  await expect(page.getByText('Есть ожидающие одобрения, но решать их вы не можете')).toBeVisible();
  await expect(page.getByRole('radio', { name: 'Комментарий' })).toBeChecked();
  await page.getByRole('button', { name: 'Оставить комментарий' }).click();
  await expect(page.getByText('Введите комментарий к ревью.')).toBeVisible();
  await page.getByLabel('Комментарий к ревью (обязательно)').fill('Посмотрел, вопросы в тредах');
  await page.getByRole('button', { name: 'Оставить комментарий' }).click();
  await expect(page.getByTestId('review-result')).toContainText('Ревью с комментарием сохранено.');
  expect(api.requests.find((r) => r.operationId === 'submitReview')?.body).toMatchObject({ verdict: 'COMMENT', thread_ids: [threads[0]!.id, threads[1]!.id] });
});

test('RBAC: viewer reads threads but cannot comment, reply, resolve or submit', async ({ page, api }) => {
  const { url, threads } = seed(api, { role: 'viewer' });
  await serveArtifacts(page);
  await login(page, api);
  await openText(page, url);
  await expect(page.getByRole('button', { name: /^Комментировать строку/ })).toHaveCount(0);
  await expect(page.getByText('Ваша роль может только читать обсуждения.')).toBeVisible();
  const th = page.locator(`[data-thread-id="${threads[0]!.id}"]`);
  await expect(th).toContainText('Почему не JSON?');
  await expect(th.getByLabel('Ответить')).toHaveCount(0);
  await expect(th.getByRole('button', { name: 'Отметить решённым' })).toHaveCount(0);
  await expect(page.locator(`[data-thread-id="${threads[1]!.id}"]`).getByRole('button', { name: 'Изменить' })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Отправить ревью' }).click();
  await expect(page.getByText('Ваша роль может читать ревью, но не отправлять его.')).toBeVisible();
  await expect(page.getByRole('radio')).toHaveCount(0);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `code review viewer (${scheme})`);
  }
  expect(api.requests.filter((r) => /create|reply|resolve|submit/i.test(r.operationId) && r.operationId.includes('Review'))).toEqual([]);
});

test('no git_diff evidence → empty state with a link to Mission Control', async ({ page, api }) => {
  const { url, run } = seed(api, { noDiff: true });
  await login(page, api);
  await page.goto(url);
  await expect(page.getByTestId('empty-state')).toContainText('Изменений для ревью нет');
  await expect(page.getByTestId('empty-state').getByRole('link', { name: 'Открыть в Mission Control' })).toHaveAttribute('href', new RegExp(`run=${run.id}`));
  await expectNoSeriousA11yViolations(page, 'code review empty');
});

test('live: review.comment from another reviewer updates the thread without reload', async ({ page, api }) => {
  const { url, threads, run, comment } = seed(api);
  await serveArtifacts(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await openText(page, url);
  const th = threads[0]!;
  await expect(page.locator(`[data-thread-id="${th.id}"]`).getByTestId('comment-body')).toHaveCount(1);
  const c = comment(th.id, OTHER, 'Поддерживаю: нужен тест', 100);
  th.comments.push(c);
  api.addEvent(run, 'review.comment', {
    action: 'comment_created',
    thread_id: th.id,
    comment_id: c.id,
    review_id: null,
    repository_id: REPO_ID,
    task_id: th.task_id,
    commit_sha: HEAD,
    side: 'head',
    file_path: 'src/export.ts',
    start_line: 2,
    end_line: 2,
    status: 'OPEN',
    actor_id: OTHER.id,
    occurred_at: AT(100),
  });
  await expect(page.locator(`[data-thread-id="${th.id}"]`).getByTestId('comment-body').last()).toHaveText('Поддерживаю: нужен тест');
  await expect(page.getByTestId('review-live')).toHaveText('Новый комментарий: src/export.ts:2');
});

test('keyboard: files with arrows and ] / [, threads with n / p, comment on a line with Tab + Enter, Ctrl+Enter', async ({ page, api }) => {
  const { url, threads } = seed(api);
  await serveArtifacts(page);
  await page.setViewportSize({ width: 1366, height: 900 });
  await login(page, api);
  await openText(page, url);
  const files = page.getByTestId('review-files');
  await focusDocumentStart(page);
  await tabTo(page, files.getByRole('button').first(), { max: 150 });
  await page.keyboard.press('ArrowDown');
  await expect(files.getByRole('button').nth(1)).toBeFocused();
  await expect(page.getByTestId('review-current-file')).toHaveText('src/csv.ts');
  await expect(page).toHaveURL(/file=src%2Fcsv\.ts/);
  await page.keyboard.press('[');
  await expect(page.getByTestId('review-current-file')).toHaveText('src/export.ts');
  await page.keyboard.press(']');
  await expect(page.getByTestId('review-current-file')).toHaveText('src/csv.ts');
  // n — к первому обсуждению (по порядку файла/строки): оно в другом файле — файл переключается, фокус на треде.
  await page.keyboard.press('n');
  await expect(page.getByTestId('review-current-file')).toHaveText('src/export.ts');
  await expect(page.locator(`[data-thread-id="${threads[1]!.id}"]`)).toBeFocused();
  await page.keyboard.press('n');
  await expect(page.locator(`[data-thread-id="${threads[0]!.id}"]`)).toBeFocused();
  await page.keyboard.press('p');
  await expect(page.locator(`[data-thread-id="${threads[1]!.id}"]`)).toBeFocused();

  // Комментарий к строке только с клавиатуры.
  const line = page.getByRole('button', { name: 'Комментировать строку 4 (новая версия)' });
  await page.getByRole('button', { name: 'Текст с комментариями' }).focus();
  await tabTo(page, line, { max: 60 });
  await page.keyboard.press('Enter');
  const body = page.getByTestId('new-thread-form').getByLabel('Комментарий');
  await expect(body).toBeFocused();
  await page.keyboard.type('Закрывающая скобка без перевода строки');
  await page.keyboard.press('Control+Enter');
  await expect(page.getByTestId('new-thread-form')).toHaveCount(0);
  await expect(page.locator(`[data-thread-id="${threads[2]!.id}"]`)).toBeFocused();
  expect((api.requests.find((r) => r.operationId === 'createReviewThread')?.body as S['ReviewThreadCreateRequest']).anchor).toMatchObject({ start_line: 4, end_line: 4, side: 'head' });
});

for (const width of [375, 768, 1366, 1920]) {
  test(`code review layout at ${width}px: no horizontal overflow (both themes), files drawer on mobile`, async ({ page, api }) => {
    const { url } = seed(api);
    await serveArtifacts(page);
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await login(page, api);
    await page.goto(url);
    if (width < 768) {
      // < 768: diff — текстовый вид, Monaco «Рядом» недоступен; файлы — в выезжающей панели.
      await expect(page.getByTestId('diff-text')).toBeVisible();
      await expect(page.getByRole('button', { name: 'Рядом' })).toHaveCount(0);
      await page.getByRole('button', { name: 'Файлы (2)' }).click();
      const drawer = page.getByRole('dialog', { name: 'Изменённые файлы' });
      await drawer.getByRole('button', { name: /src\/csv\.ts/ }).click();
      await expect(drawer).toHaveCount(0);
      await expect(page.getByTestId('review-current-file')).toHaveText('src/csv.ts');
    } else {
      await expect(page.getByTestId('monaco-diff').locator('.monaco-diff-editor')).toBeVisible({ timeout: 15_000 });
    }
    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
    }
    if (width === 375) {
      await expectNoSeriousA11yViolations(page, 'code review 375');
      await shot(page, 'review-375');
    }
    if (width === 1920) await shot(page, 'review-1920');
  });
}
