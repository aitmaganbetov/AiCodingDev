/**
 * P7-08 Task Detail: попытки, события/логи (история + live SSE), ACP-сообщения, diff viewer
 * (Monaco из локального пакета, лениво; текстовый вид), evidence и gate executions.
 */
import type { Page } from '@playwright/test';
import type { components } from '../../src/api/schema';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];
const AT = (s: number) => `2026-11-08T09:00:${String(s).padStart(2, '0')}Z`;
const SHA = 'b'.repeat(40);
const DIFF_ARTIFACT = '01900000-0000-7000-d000-0000000000a1';
const LOG_ARTIFACT = '01900000-0000-7000-d000-0000000000a2';
const EVIDENCE_DIFF = '01900000-0000-7000-d000-0000000000e1';
const EVIDENCE_TESTS = '01900000-0000-7000-d000-0000000000e2';

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

function seed(api: MockApi, opts: { failTask?: boolean } = {}) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'developer');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'RUNNING', version: 3 });
  const task = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING', attempt_count: 2, current_provider: 'codex', version: 4 });
  const attemptId = '01900000-0000-7000-d000-0000000000b1';

  api.route('GET', '/api/v1/tasks/{id}', 'getTask', ({ id }) => {
    if (opts.failTask || id !== task.id) return problem(404, 'not_found');
    const full: S['Task'] = {
      ...task,
      instructions: 'Добавить экспорт отчётов в CSV.\n<script>alert(1)</script>',
      acceptance_criteria: ['Разделитель — точка с запятой', 'Покрытие тестами ≥ 80%'],
      review_loop_count: 1,
      next_attempt_at: null,
      timeout_seconds: 1800,
      created_at: AT(0),
      links: {},
    };
    return { status: 200, body: full, headers: { ETag: `"v${task.version}"` } };
  });
  api.route('GET', '/api/v1/tasks/{id}/attempts', 'listTaskAttempts', () => ({
    status: 200,
    body: {
      items: [
        {
          id: attemptId,
          task_id: task.id,
          attempt_no: 1,
          status: 'FAILED',
          provider: 'claude',
          agent_role: 'developer',
          started_at: AT(1),
          finished_at: AT(5),
          failure_category: 'GATE_FAILED',
          executions: [{ id: '01900000-0000-7000-d000-0000000000c1', status: 'EXITED', exit_code: 1, output_artifact_id: LOG_ARTIFACT }],
          routing_reason: 'primary',
        },
        {
          id: '01900000-0000-7000-d000-0000000000b2',
          task_id: task.id,
          attempt_no: 2,
          status: 'RUNNING',
          provider: 'codex',
          agent_role: 'developer',
          started_at: AT(6),
          finished_at: null,
          failure_category: null,
          executions: [{ id: '01900000-0000-7000-d000-0000000000c2', status: 'RUNNING' }],
          routing_reason: 'fallback после GATE_FAILED',
        },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/tasks/{id}/messages', 'listTaskMessages', () => ({
    status: 200,
    body: {
      items: [
        {
          message_id: '01900000-0000-7000-d000-0000000000f1',
          protocol: 'acp/1.0',
          run_id: run.id,
          task_id: task.id,
          task_attempt_id: null,
          from: { role: 'orchestrator' },
          to: { role: 'developer', task_id: task.id },
          type: 'TASK_ASSIGN',
          correlation_id: '01900000-0000-7000-d000-0000000000f2',
          schema_version: '1.0',
          payload: { objective: 'Экспорт CSV', note: '<img src=x onerror=alert(1)>' },
          created_at: AT(1),
        },
      ],
      next_cursor: null,
    },
  }));
  const artifact = (id: string, kind: S['ArtifactKind'], size: number): S['ArtifactRef'] => ({
    id,
    kind,
    sha256: 'c'.repeat(64),
    size_bytes: size,
    mime_type: 'text/plain',
    download_url: `/api/v1/artifacts/${id}/download`,
  });
  api.route('GET', '/api/v1/tasks/{id}/evidence', 'listTaskEvidence', () => ({
    status: 200,
    body: {
      items: [
        {
          id: EVIDENCE_DIFF,
          run_id: run.id,
          task_id: task.id,
          kind: 'git_diff',
          producer: 'verification-engine',
          verification_status: 'VERIFIED',
          commit_sha: SHA,
          command: 'git diff base..head',
          exit_code: 0,
          started_at: AT(20),
          finished_at: AT(21),
          artifact: artifact(DIFF_ARTIFACT, 'git_diff', DIFF.length),
          created_at: AT(21),
        },
        {
          id: EVIDENCE_TESTS,
          run_id: run.id,
          task_id: task.id,
          kind: 'test_report',
          producer: 'gate:UNIT_TESTS',
          verification_status: 'VERIFIED',
          commit_sha: SHA,
          command: 'pnpm test -- --reporter=junit',
          exit_code: 0,
          started_at: AT(22),
          finished_at: AT(25),
          artifact: artifact('01900000-0000-7000-d000-0000000000a3', 'test_report', 2048),
          created_at: AT(25),
        },
      ],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', () => ({
    status: 200,
    body: {
      run_id: run.id,
      quality_profile: 'strict',
      verdict: 'RUNNING',
      gate_executions: [
        { id: '01900000-0000-7000-d000-000000000071', gate_id: '01900000-0000-7000-d000-000000000081', gate_type: 'UNIT_TESTS', required: true, task_id: task.id, status: 'PASS', evidence_id: EVIDENCE_TESTS, commit_sha: SHA },
        { id: '01900000-0000-7000-d000-000000000072', gate_id: '01900000-0000-7000-d000-000000000082', gate_type: 'SAST', required: true, task_id: task.id, status: 'ERROR', evidence_id: null, commit_sha: SHA, error_code: 'TOOL_UNAVAILABLE' },
        { id: '01900000-0000-7000-d000-000000000073', gate_id: '01900000-0000-7000-d000-000000000083', gate_type: 'LINT', required: false, task_id: '01900000-0000-7000-d000-000000000099', status: 'PASS', evidence_id: null, commit_sha: SHA },
      ],
      findings_summary: {},
      findings_by_status: {},
      findings: [],
      findings_total: 0,
    } satisfies S['QualityReport'],
  }));
  api.route('GET', '/api/v1/runs/{id}/activity', 'getRunActivity', ({ url }) => {
    expect(url.searchParams.get('task_id')).toBe(task.id);
    return {
      status: 200,
      body: {
        items: [
          { id: '01900000-0000-7000-d000-000000000061', run_id: run.id, task_id: task.id, seq: 1, event_type: 'task.state_changed', payload: { task_id: task.id, title: task.title, agent_role: 'developer', from_status: 'READY', to_status: 'RUNNING', task_version: 1 }, occurred_at: AT(1) },
          { id: '01900000-0000-7000-d000-000000000062', run_id: run.id, task_id: task.id, seq: 2, event_type: 'cli.output', payload: { execution_id: '01900000-0000-7000-d000-0000000000c1', task_id: task.id, provider: 'claude', agent_role: 'developer', cli_seq: 1, cli_event_type: 'output', stream: 'stdout', text: 'npm test: 1 failed' }, occurred_at: AT(4) },
        ],
        next_cursor: null,
      },
    };
  });
  // Журнал run: события 1..2 уже в истории (activity); SSE повторит их — UI не должен задублировать.
  api.addEvent(run, 'task.state_changed', { task_id: task.id, title: task.title, agent_role: 'developer', from_status: 'READY', to_status: 'RUNNING', task_version: 1 });
  api.addEvent(run, 'cli.output', { execution_id: '01900000-0000-7000-d000-0000000000c1', task_id: task.id, provider: 'claude', agent_role: 'developer', cli_seq: 1, cli_event_type: 'output', stream: 'stdout', text: 'npm test: 1 failed' });
  return { ws, project, run, task, url: `/w/${ws.id}/projects/${project.id}/runs/${run.id}/tasks/${task.id}` };
}

/**
 * downloadArtifact: потоковая отдача через API (200 application/octet-stream, как в контракте).
 * Маршрут регистрируется после MockApi.install и имеет приоритет.
 */
async function serveArtifacts(page: Page): Promise<string[]> {
  const fetched: string[] = [];
  await page.route('**/api/v1/artifacts/*/download', async (route) => {
    fetched.push(route.request().url());
    await route.fulfill({ status: 200, contentType: 'application/octet-stream', headers: { 'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="git_diff.diff"' }, body: DIFF });
  });
  return fetched;
}

test('task detail: summary, attempts, events/logs with live CLI output, ACP messages, axe', async ({ page, api }) => {
  const { run, task, url } = seed(api);
  await login(page, api);
  await page.goto(url);

  await expect(page.getByRole('heading', { level: 1, name: 'Реализовать экспорт' })).toBeFocused();
  await expect(page.getByTestId('task-executor')).toHaveText('Разработчик · Codex');
  await expect(page.getByText('Добавить экспорт отчётов в CSV.')).toBeVisible();
  await expect(page.getByText('<script>alert(1)</script>')).toBeVisible();
  await expect(page.getByText('Разделитель — точка с запятой')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'task overview');

  // Вкладки — клавиатурой (стрелки, APG Tabs).
  await page.getByRole('tab', { name: 'Описание' }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Попытки' })).toHaveAttribute('aria-selected', 'true');
  await expect(page).toHaveURL(/tab=attempts/);
  await expect(page.getByRole('tab', { name: 'Попытки' })).toBeFocused();
  const attempts = page.getByRole('table', { name: 'Попытки выполнения задачи' });
  await expect(attempts.getByRole('row')).toHaveCount(3);
  await expect(attempts.getByRole('row').nth(1)).toContainText('Разработчик · Codex');
  await expect(attempts.getByRole('row').nth(2)).toContainText('Проверка не пройдена');
  await expect(attempts.getByRole('link', { name: 'вывод' })).toHaveAttribute('href', `/api/v1/artifacts/${LOG_ARTIFACT}/download`);
  await expectNoSeriousA11yViolations(page, 'task attempts');

  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'События и логи' })).toHaveAttribute('aria-selected', 'true');
  const events = page.getByTestId('task-events');
  await expect(events.getByRole('listitem')).toHaveCount(2);
  api.addEvent(run, 'cli.output', {
    execution_id: '01900000-0000-7000-d000-0000000000c2',
    task_id: task.id,
    provider: 'codex',
    agent_role: 'developer',
    cli_seq: 1,
    cli_event_type: 'output',
    stream: 'stdout',
    text: 'npm test: 42 passed',
  });
  await expect(events.getByRole('listitem')).toHaveCount(3);
  await expect(events.getByRole('listitem').last()).toContainText('npm test: 42 passed');
  await page.getByRole('checkbox', { name: 'Только вывод CLI' }).check();
  await expect(events.getByRole('listitem')).toHaveCount(2);
  await expectNoSeriousA11yViolations(page, 'task events');

  await page.getByRole('tab', { name: 'Сообщения ACP' }).click();
  const messages = page.getByTestId('task-messages');
  await expect(messages).toContainText('Назначение задачи');
  await expect(messages).toContainText('Оркестратор → Разработчик');
  await expect(messages).toContainText('"note": "<img src=x onerror=alert(1)>"');
  await expect(messages.locator('img')).toHaveCount(0);
});

test('task detail: diff viewer (Monaco from the local bundle, lazy) and text view; evidence and gate links', async ({ page, api }) => {
  const { url } = seed(api);
  const external: string[] = [];
  page.on('request', (r) => {
    if (!r.url().startsWith('http://127.0.0.1')) external.push(r.url());
  });
  const fetched = await serveArtifacts(page);
  await login(page, api);
  await page.goto(`${url}?tab=diff`);

  const files = page.getByTestId('diff-files');
  await expect(files.getByRole('button')).toHaveCount(2);
  await expect(files.getByRole('button').first()).toContainText('src/export.ts');
  await expect(files.getByRole('button').first()).toContainText('+2 −1');
  await expect(page.getByTestId('monaco-diff').locator('.monaco-diff-editor')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('monaco-diff')).toContainText('toCsv');
  expect(fetched).toEqual([expect.stringContaining(`/api/v1/artifacts/${DIFF_ARTIFACT}/download`)]);

  await files.getByRole('button', { name: /src\/csv\.ts/ }).click();
  await expect(page.getByTestId('diff-current-file')).toHaveText('src/csv.ts');
  await page.getByRole('button', { name: 'Текст' }).click();
  await expect(page.getByRole('figure').filter({ hasText: 'src/csv.ts' })).toContainText("+export const SEP = ';';");
  await expectNoSeriousA11yViolations(page, 'task diff text');
  expect(external, 'Monaco must not load from a CDN').toEqual([]);

  await page.getByRole('tab', { name: 'Evidence и проверки' }).click();
  const gates = page.getByRole('table', { name: 'Проверки качества (gate executions)' });
  await expect(gates.getByRole('row')).toHaveCount(3); // только gates этой задачи
  await expect(gates).toContainText('TOOL_UNAVAILABLE');
  await gates.getByRole('link', { name: /evidence/ }).click();
  await expect(page).toHaveURL(new RegExp(`#evidence-${EVIDENCE_TESTS}$`));
  const evidence = page.getByTestId('task-evidence');
  await expect(evidence.getByRole('listitem')).toHaveCount(2);
  await expect(evidence.locator(`#evidence-${EVIDENCE_TESTS}`)).toContainText('pnpm test -- --reporter=junit');
  await expect(evidence.locator(`#evidence-${EVIDENCE_TESTS}`)).toContainText(SHA);
  await expectNoSeriousA11yViolations(page, 'task evidence');
});

test('task detail: not found shows an error state', async ({ page, api }) => {
  const { url } = seed(api, { failTask: true });
  await login(page, api);
  await page.goto(url);
  await expect(page.getByTestId('error-state')).toContainText('Не найдено или нет доступа.');
  await expectNoSeriousA11yViolations(page, 'task not found');
});
