/**
 * F4 AI Mission Control (план §3.3, §5.8, §6.2–6.3): выбор run, DAG с живыми статусами и измеренными
 * индикаторами (D-8), виды ленты (беседа + фильтры, вывод CLI — виртуализация, системные события,
 * evidence), User Command Center (impact preview → подтверждение → доставлено/подтверждено/выполнено,
 * критические — подтверждение до отправки), Agent Inspector с Context Pack (B3), клавиатура, axe в
 * светлой/тёмной теме, нет горизонтального переполнения на 375/768/1366/1920, reduced motion.
 * Операции экрана регистрируются здесь (api.route) и валидируются по контракту.
 * Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */
import type { Page } from '@playwright/test';
import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type MockRequest } from '../support/mockApi';
import { focusDocumentStart, login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

let n = 0;
const uid = (group: string) => `01900000-0000-7000-${group}-${String(++n).padStart(12, '0')}`;
const AT = (s: number) => `2026-11-08T09:00:${String(s).padStart(2, '0')}Z`;
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;

function artifact(kind: S['ArtifactKind']): S['ArtifactRef'] {
  const id = uid('f000');
  return { id, kind, sha256: 'a'.repeat(64), size_bytes: 2048, mime_type: 'application/json', download_url: `/api/v1/artifacts/${id}/download` };
}

function seed(api: MockApi, role: S['WorkspaceRole'] = 'developer') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  api.addRun(project, { objective: 'Старый завершённый запуск', status: 'COMPLETED', finished_at: AT(1) });
  const run = api.addRun(project, { objective: 'Добавить экспорт отчётов в CSV', status: 'RUNNING', version: 3 });
  const a = api.addTask(run, 'Спроектировать API', { agent_role: 'architect', status: 'SUCCEEDED', attempt_count: 1, current_provider: 'claude' });
  const b = api.addTask(run, 'Реализовать экспорт', { status: 'RUNNING', depends_on: [a.id], attempt_count: 1, current_provider: 'codex', version: 2 });
  const c = api.addTask(run, 'Проверить экспорт', { agent_role: 'qa', status: 'PENDING', depends_on: [b.id] });
  const cli = (i: number, text: string, extra: Record<string, unknown> = {}) =>
    api.addEvent(run, 'cli.output', {
      execution_id: '01900000-0000-7000-b000-000000000003',
      task_id: b.id,
      provider: 'codex',
      agent_role: 'developer',
      cli_seq: i,
      cli_event_type: 'output',
      stream: 'stdout',
      text,
      ...extra,
    });
  api.addEvent(run, 'task.state_changed', { task_id: b.id, title: b.title, agent_role: 'developer', from_status: 'READY', to_status: 'RUNNING', task_version: 2, attempt_no: 1, provider: 'codex' });
  cli(1, 'Пишу export/csv.py');
  cli(2, 'token=*** pytest -q', { redactions: 1, stream: 'stderr' });

  // Отчёт качества и evidence run: у задачи A один PASS с VERIFIED evidence и один PASS без подтверждения.
  const evVerified: S['Evidence'] = {
    id: uid('9000'),
    run_id: run.id,
    task_id: a.id,
    kind: 'test_report',
    producer: 'gate:unit_tests',
    verification_status: 'VERIFIED',
    commit_sha: 'a'.repeat(40),
    command: 'pytest -q',
    exit_code: 0,
    artifact: artifact('test_report'),
    created_at: AT(3),
  };
  const evUnverified: S['Evidence'] = { ...evVerified, id: uid('9000'), kind: 'gate_log', producer: 'gate:lint', verification_status: 'UNVERIFIED', artifact: artifact('gate_log') };
  const gates: S['GateExecution'][] = [
    { id: uid('8000'), gate_id: uid('8000'), gate_type: 'UNIT_TESTS', required: true, task_id: a.id, status: 'PASS', evidence_id: evVerified.id, commit_sha: 'a'.repeat(40) },
    { id: uid('8000'), gate_id: uid('8000'), gate_type: 'LINT', required: true, task_id: a.id, status: 'PASS', evidence_id: evUnverified.id, commit_sha: 'a'.repeat(40) },
    { id: uid('8000'), gate_id: uid('8000'), gate_type: 'UNIT_TESTS', required: true, task_id: b.id, status: 'RUNNING', evidence_id: null },
  ];
  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', ({ id }) => ({
    status: 200,
    body: { run_id: id ?? run.id, quality_profile: 'strict', verdict: 'RUNNING', gate_executions: gates, findings_summary: {}, findings: [], findings_total: 0 },
  }));
  api.route('GET', '/api/v1/runs/{id}/evidence', 'listRunEvidence', ({ url }) => {
    const status = url.searchParams.get('verification_status');
    const task = url.searchParams.get('task_id');
    const items = [evVerified, evUnverified].filter((e) => (!status || e.verification_status === status) && (!task || e.task_id === task));
    return { status: 200, body: { items, next_cursor: null } };
  });

  // Беседа агентов.
  const thread = uid('e000');
  const chat: S['ChatItem'][] = [
    {
      id: uid('c000'),
      item_type: 'agent_message',
      thread_id: thread,
      task_id: a.id,
      sender_type: 'agent',
      sender: { user: null, role: 'architect', provider: 'claude' },
      recipient_role: 'developer',
      message_kind: 'agent_relay',
      acp_type: 'HANDOFF',
      body: 'Контракт API готов, передаю реализацию',
      mentions: [],
      refs: [{ type: 'evidence', id: evVerified.id }],
      created_at: AT(4),
    },
    {
      id: uid('c000'),
      item_type: 'agent_message',
      thread_id: thread,
      task_id: b.id,
      sender_type: 'agent',
      sender: { user: null, role: 'developer', provider: 'codex' },
      recipient_role: 'reviewer',
      message_kind: 'agent_relay',
      acp_type: 'REVIEW_REQUEST',
      body: 'Экспорт готов, прошу ревью <img src=x onerror=alert(1)>',
      mentions: [],
      created_at: AT(5),
    },
  ];
  api.route('GET', '/api/v1/runs/{id}/chat', 'getRunChat', ({ url }) => {
    const stream = url.searchParams.get('stream');
    const items = chat.filter((i) => !(stream === 'codex' || stream === 'claude') || i.sender?.provider === stream).reverse();
    return { status: 200, body: { items, threads: [{ id: thread, run_id: run.id, scope: 'run', created_at: AT(0) }], next_cursor: null } };
  });

  // Задача B: карточка, попытки, evidence, Context Pack (B3).
  const attemptId = uid('7000');
  const taskOf = (id: string) => [a, b, c].find((x) => x.id === id);
  api.route('GET', '/api/v1/tasks/{id}', 'getTask', ({ id }) => {
    const t = taskOf(id ?? '');
    return t ? { status: 200, body: { ...t, instructions: 'Экспорт в CSV', acceptance_criteria: ['Файл открывается в Excel'], review_loop_count: 0 } } : problem(404, 'not_found');
  });
  api.route('GET', '/api/v1/tasks/{id}/attempts', 'listTaskAttempts', ({ id }) => ({
    status: 200,
    body: {
      items:
        id === b.id
          ? [
              {
                id: attemptId,
                task_id: b.id,
                attempt_no: 1,
                status: 'RUNNING',
                provider: 'codex',
                agent_role: 'developer',
                started_at: AT(2),
                finished_at: null,
                executions: [{ id: uid('6000'), status: 'RUNNING', exit_code: null, output_artifact_id: uid('f000'), started_at: AT(2), finished_at: null }],
                routing_reason: 'профиль developer → codex',
              },
            ]
          : [],
      next_cursor: null,
    },
  }));
  api.route('GET', '/api/v1/tasks/{id}/evidence', 'listTaskEvidence', () => ({ status: 200, body: { items: [], next_cursor: null } }));
  api.route('GET', '/api/v1/tasks/{id}/attempts/[^/]+/context-pack', 'getAttemptContextPack', ({ id }) =>
    id === b.id
      ? {
          status: 200,
          body: {
            id: uid('5000'),
            task_id: b.id,
            attempt_id: attemptId,
            snapshot_id: uid('5000'),
            base_commit: 'b'.repeat(40),
            snapshot_commit: 'b'.repeat(40),
            policy_version: 1,
            documentation_mode: 'SMART',
            token_budget: 32000,
            size_bytes: 20480,
            content_hash: 'c'.repeat(64),
            artifact_id: uid('f000'),
            created_at: AT(2),
            schema: 1,
            stale_policy: 'mark',
            role: 'developer',
            docs: 'allowed',
            budget: { max_bytes: 65536, max_files: 40, max_fragment_bytes: 8192 },
            cache_hit: false,
            fragments: [
              { section: 'code', kind: 'exact', path: 'src/export/csv.py', lines: [1, 80], commit: 'b'.repeat(40), sha256: 'd'.repeat(64), bytes: 2400, stale: false, source_ref: null },
              { section: 'symbols_graph', kind: 'dependency', path: 'src/reports/model.py', lines: [10, 42], commit: 'b'.repeat(40), sha256: 'e'.repeat(64), bytes: 900, stale: false, source_ref: null },
            ],
            omitted: [{ section: 'docs', kind: 'docs', path: 'doc:csv-rfc', commit: null, reason: 'max_bytes' }],
            warnings: [],
            fragments_total: 2,
            omitted_total: 1,
          } satisfies S['AttemptContextPack'],
        }
      : problem(404, 'not_found'),
  );

  // Поручения: create → (impact) → confirm → dispatched → acknowledged → completed.
  const directives: S['Directive'][] = [];
  api.route('GET', '/api/v1/runs/{id}/directives', 'listDirectives', () => ({ status: 200, body: { items: [...directives].reverse(), next_cursor: null } }));
  api.route('POST', '/api/v1/runs/{id}/directives', 'createDirective', ({ body }) => {
    const req = body as S['DirectiveCreateRequest'];
    if (req.expected_run_version !== run.version) return problem(409, 'version_mismatch');
    const d: S['Directive'] = {
      id: uid('d000'),
      run_id: run.id,
      chat_message_id: uid('c000'),
      target: req.target,
      directive_type: req.directive_type,
      message: req.message,
      status: 'IMPACT_ANALYSIS_PENDING',
      impact_status: 'PENDING',
      impact_summary: null,
      impacts: [],
      requires_confirmation: false,
      approval_id: null,
      delivery: { state: 'NOT_DISPATCHED', acknowledgements: [] },
      completion: { state: 'NOT_STARTED', resulting_task_ids: [] },
      submitted_by: { id: '01900000-0000-7000-8000-0000000000aa', display_name: api.user.display_name },
      created_at: AT(30),
      updated_at: AT(30),
      version: 0,
    };
    directives.push(d);
    return { status: 202, body: { directive_id: d.id, status: d.status, chat_message_id: d.chat_message_id, status_url: `/api/v1/runs/${run.id}/directives/${d.id}` } };
  });
  const find = (req: MockRequest) => directives.find((d) => req.url.pathname.split('/')[6] === d.id);
  api.route('GET', '/api/v1/runs/{id}/directives/[^/]+', 'getDirective', (req) => {
    const d = find(req);
    return d ? { status: 200, body: d, headers: { ETag: `"v${d.version}"` } } : problem(404, 'not_found');
  });
  const update = (d: S['Directive'], patch: Partial<S['Directive']>) => {
    const from = d.status;
    Object.assign(d, patch, { version: d.version + 1 });
    api.addEvent(run, 'directive.state_changed', { directive_id: d.id, directive_type: d.directive_type, from_status: from, to_status: d.status });
  };
  api.route('POST', '/api/v1/runs/{id}/directives/[^/]+/confirm', 'confirmDirective', (req) => {
    const d = find(req);
    if (!d) return problem(404, 'not_found');
    if (req.headers['if-match'] !== `"v${d.version}"`) return problem(412, 'precondition_failed');
    update(d, { status: 'CONFIRMED' });
    return { status: 202, body: d };
  });
  return { ws, project, run, a, b, c, directives, update, cli };
}

async function openRun(page: Page, ws: S['Workspace'], run: S['Run'], query = '') {
  await page.goto(`/w/${ws.id}/mission-control?run=${run.id}${query}`);
  await expect(page.getByTestId('mc-layout')).toBeVisible();
}

test('run selection → three panes; DAG live update with verified progress (D-8); agent inspector with Context Pack; axe light/dark', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws, run, a, b, c } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'AI Mission Control' }).click();
  const runs = page.getByRole('list', { name: 'Активные запуски' });
  await expect(runs.getByRole('listitem')).toHaveCount(1); // завершённый run — не активный
  await runs.getByRole('button', { name: /Добавить экспорт отчётов в CSV/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/mission-control?run=${run.id}`);
  await expect(page.getByTestId('mc-layout')).toHaveAttribute('data-layout', 'three');
  await expect(page.getByTestId('run-status')).toHaveText('Выполняется');
  await expect(page.getByTestId('mc-stages').locator('[data-stage="RUNNING"]')).toHaveAttribute('aria-current', 'step');

  const canvas = page.getByTestId('run-graph-canvas');
  await expect(canvas.locator('.react-flow__node')).toHaveCount(3);
  const nodeA = canvas.locator(`[data-task-id="${a.id}"]`);
  // Прогресс — только проверки с VERIFIED evidence: PASS без подтверждения не засчитан.
  await expect(nodeA.getByTestId('node-progress')).toContainText('Подтверждено проверок: 1 из 2');
  await expect(nodeA.getByTestId('node-evidence')).toContainText('Evidence: 1/2 подтв.');
  await expect(nodeA.getByTestId('node-evidence')).toContainText('Тесты: 1/1');
  await expect(canvas.locator(`[data-task-id="${c.id}"]`).getByTestId('node-warnings')).toContainText('Ждёт зависимостей: 1');
  await expect(canvas.locator(`[data-task-id="${c.id}"]`).getByTestId('node-progress')).toHaveCount(0); // нет проверок — нет «0 %»
  await expect(canvas.locator(`[data-task-id="${b.id}"]`)).toHaveClass(/aicd-node-active/);
  await expect(page.getByTestId('context-panel').getByTestId('mc-run-inspector')).toBeVisible();

  // Live: QA начинает у Claude — узел и объявление обновляются без перезагрузки.
  api.addEvent(run, 'task.state_changed', { task_id: c.id, title: c.title, agent_role: 'qa', from_status: 'PENDING', to_status: 'RUNNING', task_version: 1, attempt_no: 1, provider: 'claude' });
  await expect(canvas.locator(`[data-task-id="${c.id}"]`)).toHaveAttribute('data-status', 'RUNNING');
  await expect(canvas.locator(`[data-task-id="${c.id}"]`)).toContainText('Claude');
  await expect(page.getByTestId('graph-announcement')).toHaveText('«Проверить экспорт»: Ожидает → Выполняется');
  await expect(page.getByTestId('current-executors')).toContainText('QA · Claude');

  // Выбор узла → Agent Inspector в контекстной панели.
  await canvas.locator(`[data-task-id="${b.id}"]`).getByRole('button', { name: b.title }).click();
  await expect(page).toHaveURL(new RegExp(`task=${b.id}`));
  const panel = page.getByTestId('context-panel');
  await expect(panel.getByRole('heading', { level: 2, name: 'Инспектор агента' })).toBeVisible();
  const inspector = panel.getByTestId('mc-task-inspector');
  await expect(inspector.getByTestId('inspector-task-title')).toHaveText(b.title);
  await expect(inspector.locator('[data-agent-role="developer"]')).toContainText('Codex');
  await expect(inspector.getByTestId('inspector-cli-status')).not.toHaveText('');
  await expect(inspector.getByTestId('inspector-gates')).toContainText('UNIT_TESTS');
  await expect(inspector.getByTestId('inspector-events')).toContainText('Пишу export/csv.py');
  const pack = inspector.getByTestId('context-pack');
  await expect(pack).toContainText('src/export/csv.py');
  await expect(pack).toContainText('строки 1–80');
  await expect(pack).toContainText('Пропущено фрагментов: 1');
  await expect(inspector.getByRole('link', { name: 'Карточка задачи' })).toHaveAttribute('href', `/w/${ws.id}/projects/${run.project_id}/runs/${run.id}/tasks/${b.id}`);
  await expect(inspector.getByRole('link', { name: 'Изменения (diff)' })).toHaveAttribute('href', new RegExp(`/tasks/${b.id}\\?tab=diff$`));
  const ctx = api.requests.find((r) => r.operationId === 'getAttemptContextPack');
  expect(ctx).toBeTruthy();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `mission control (${scheme})`);
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/mc-1366-${scheme}.png`, fullPage: true });
  }

  // Задача без Context Pack (404) — честное «не собирался», не ошибка и не пустой список.
  await canvas.locator(`[data-task-id="${a.id}"]`).getByRole('button', { name: a.title }).click();
  await expect(panel.getByTestId('mc-task-inspector').getByText('Попыток ещё не было — контекст агенту не передавался.')).toBeVisible();
});

test('live chat views: conversation (text only) with filters, CLI output (virtualized, redaction markers), system events, evidence', async ({ page, api }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  const { ws, run, a, cli } = seed(api);
  await login(page, api);
  await openRun(page, ws, run);
  const items = page.getByTestId('chat-timeline').getByTestId('chat-item');
  await expect(items).toHaveCount(2);
  await expect(items.nth(1).getByRole('heading')).toHaveText('Разработчик · Codex → Ревьюер');
  await expect(items.nth(1)).toContainText('Запрос ревью');
  await expect(items.nth(1).getByTestId('chat-item-body')).toHaveText('Экспорт готов, прошу ревью <img src=x onerror=alert(1)>');
  await expect(page.getByTestId('chat-timeline').locator('img')).toHaveCount(0);
  await expect(items.nth(0).getByRole('link', { name: `Задача: ${a.title}` })).toBeVisible();
  // Фильтр потока: Codex.
  await page.getByRole('button', { name: 'Фильтры', exact: true }).click();
  await page.getByRole('combobox', { name: 'Поток', exact: true }).selectOption({ label: 'Codex' });
  await expect(page).toHaveURL(/stream=codex/);
  await expect(items).toHaveCount(1);

  // Вывод CLI: моноширинный, виртуализированный, маркеры редакции секретов.
  await page.getByRole('tab', { name: 'Вывод CLI' }).click();
  await expect(page).toHaveURL(/view=cli/);
  const cliFeed = page.getByRole('feed', { name: 'Вывод CLI' });
  await expect(cliFeed.getByTestId('cli-line')).toHaveCount(2);
  await expect(cliFeed.getByTestId('cli-line').nth(1).getByTestId('cli-markers')).toHaveText('скрыто секретов: 1');
  await expect(cliFeed.getByTestId('cli-line').nth(1)).toHaveAttribute('data-stream', 'stderr');
  expect(await cliFeed.getByTestId('cli-line').first().evaluate((el) => getComputedStyle(el).fontFamily)).toMatch(/JetBrains Mono/);
  // Длинный вывод: 3000 строк — в DOM только видимое окно, лента следует за концом.
  for (let i = 3; i < 3003; i++) cli(i, `строка ${i}`);
  await expect(page.getByTestId('cli-count')).toContainText('Строк: 3002.', { timeout: 15_000 });
  await expect(cliFeed.locator('[aria-setsize="3002"]').first()).toBeAttached();
  expect(await cliFeed.getByTestId('cli-line').count()).toBeLessThan(80);
  await expect(cliFeed).toContainText('строка 3002');
  // Фильтр роли: у QA вывода нет.
  await page.getByLabel('Роль агента').selectOption({ label: 'QA' });
  await expect(page.getByText('Вывода CLI пока нет')).toBeVisible();
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();

  // Системные события: журнал run целиком, затем группа «Состояния».
  await page.getByRole('tab', { name: 'Системные события' }).click();
  await page.getByRole('button', { name: 'Состояния', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Состояния', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const journal = page.getByRole('list', { name: 'События run' });
  await expect(journal.getByRole('listitem')).toHaveCount(1);
  await expect(journal).toContainText('«Реализовать экспорт»: Готова → Выполняется');

  // Evidence run: статус проверки — серверный фильтр.
  await page.getByRole('tab', { name: 'Evidence' }).click();
  const ev = page.getByTestId('mc-evidence');
  await expect(ev.getByRole('listitem')).toHaveCount(2);
  await page.getByLabel('Статус проверки').selectOption('VERIFIED');
  await expect(ev.getByRole('listitem')).toHaveCount(1);
  expect(api.requests.some((r) => r.operationId === 'listRunEvidence')).toBe(true);
  await expect(ev.getByRole('link', { name: 'Открыть evidence' })).toHaveAttribute('href', new RegExp(`/w/${ws.id}/quality/evidence/`));
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `mission control evidence view (${scheme})`);
  }
});

test('command center: rework → CHANGE_REQUEST to the selected task with impact preview → confirm → delivered / acknowledged / completed; PAUSE needs confirmation; draft survives reload', async ({
  page,
  api,
}) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws, run, b, directives, update } = seed(api);
  await login(page, api);
  await openRun(page, ws, run, `&task=${b.id}`);
  const center = page.getByTestId('command-center');
  await center.getByRole('button', { name: 'Вернуть на доработку' }).click();
  await expect(center.getByRole('radio', { name: 'Задаче' })).toBeChecked();
  await expect(center.getByRole('combobox', { name: 'Задача' })).toHaveValue(b.id);
  const text = center.getByLabel('Текст поручения');
  await expect(text).toBeFocused();
  await expect(text).toHaveAttribute('placeholder', 'Дайте указание своей AI-команде инженеров…');
  await text.fill('Добавьте также экспорт в XLSX');
  await center.getByRole('button', { name: 'Отправить поручение' }).click();
  // Запрос уходит асинхронно после клика — ждать его появления, а не читать журнал сразу.
  await expect.poll(() => api.requests.some((r) => r.operationId === 'createDirective')).toBe(true);
  const create = api.requests.find((r) => r.operationId === 'createDirective');
  expect(create?.body).toEqual({
    target: { type: 'task', task_id: b.id },
    directive_type: 'CHANGE_REQUEST',
    message: 'Добавьте также экспорт в XLSX',
    expected_run_version: 3,
    requires_impact_preview: true,
  });
  expect(create?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);

  const track = page.getByRole('dialog', { name: 'Поручение «Изменение (CHANGE_REQUEST)»' });
  await expect(track).toBeVisible();
  const step = (id: string) => track.locator(`[data-step="${id}"]`);
  await expect(step('impact')).toHaveAttribute('data-state', 'current');
  const d = directives[0]!;
  const impacts: S['DirectiveImpact'][] = [{ task_id: b.id, impact_type: 'REPLAN_REQUIRED', resolution: 'Перепланирование в safe point' }];
  api.addEvent(run, 'directive.impact_ready', { directive_id: d.id, summary: 'Затронута 1 задача', impacts, requires_confirmation: true, expected_run_version: 3 });
  update(d, { status: 'AWAITING_CONFIRMATION', impact_status: 'READY', impact_summary: 'Затронута 1 задача', impacts, requires_confirmation: true });
  await expect(track.getByTestId('directive-impacts')).toContainText('Перепланирование');
  await expect(step('confirmation')).toHaveAttribute('data-state', 'current');
  await expectNoSeriousA11yViolations(page, 'command center impact preview');
  await track.getByRole('button', { name: 'Подтвердить и применить' }).click();
  await expect(track.getByText('Поручение подтверждено.')).toBeVisible();
  update(d, { status: 'DISPATCHED', delivery: { state: 'DISPATCHED', acknowledgements: [{ recipient_role: 'developer', status: 'DELIVERED', acknowledged_at: null }] } });
  await expect(step('delivered')).toHaveAttribute('data-state', 'done');
  await expect(step('completed')).toHaveAttribute('data-state', 'pending'); // доставлено ≠ выполнено
  update(d, {
    status: 'ACKNOWLEDGED',
    delivery: { state: 'ACKNOWLEDGED', acknowledgements: [{ recipient_role: 'developer', status: 'ACKNOWLEDGED', acknowledged_at: AT(40) }] },
    completion: { state: 'IN_PROGRESS', resulting_task_ids: [b.id] },
  });
  await expect(step('acknowledged')).toHaveAttribute('data-state', 'done');
  update(d, { status: 'COMPLETED', completion: { state: 'COMPLETED', resulting_task_ids: [b.id], completed_at: AT(50) } });
  await expect(step('completed')).toHaveAttribute('data-state', 'done');
  await expect(track.getByTestId('directive-completion')).toHaveText('выполнено');
  await track.getByRole('button', { name: 'Закрыть' }).first().click();
  await expect(center.getByRole('button', { name: 'Ход поручения «Изменение (CHANGE_REQUEST)»' })).toBeVisible();

  // Критическое действие: пауза — подтверждение до отправки (без запроса до подтверждения).
  await center.getByRole('button', { name: 'Пауза', exact: true }).click();
  await text.fill('Пауза на время проверки');
  await center.getByRole('button', { name: 'Отправить поручение' }).click();
  const confirm = page.getByRole('dialog', { name: 'Отправить «Пауза (PAUSE)»?' });
  await expect(confirm).toContainText('весь run');
  expect(api.requests.filter((r) => r.operationId === 'createDirective')).toHaveLength(1);
  await expectNoSeriousA11yViolations(page, 'command center critical confirmation');
  await confirm.getByRole('button', { name: 'Подтвердить и отправить' }).click();
  await expect(page.getByRole('dialog', { name: 'Поручение «Пауза (PAUSE)»' })).toBeVisible();
  const pause = api.requests.filter((r) => r.operationId === 'createDirective')[1];
  expect(pause?.body).toMatchObject({ directive_type: 'PAUSE', target: { type: 'run' }, expected_run_version: 3 });
  await page.keyboard.press('Escape');

  // Черновик (WCAG 3.3.7) сохраняется на устройстве.
  await text.fill('Черновик поручения');
  await page.reload();
  await expect(page.getByTestId('command-center').getByLabel('Текст поручения')).toHaveValue('Черновик поручения');
});

test('keyboard only: list view, select a task, quick action and send a directive; focus returns after the dialog', async ({ page, api }) => {
  await page.setViewportSize({ width: 1366, height: 900 });
  const { ws, run, b } = seed(api);
  await login(page, api);
  await openRun(page, ws, run);
  await expect(page.getByTestId('run-graph-canvas').locator('.react-flow__node')).toHaveCount(3);
  await focusDocumentStart(page);
  const list = page.getByRole('button', { name: 'Список' });
  await tabTo(page, list, { max: 80 });
  await page.keyboard.press('Enter');
  await expect(list).toHaveAttribute('aria-pressed', 'true');
  await expect(list).toBeFocused();
  const select = page.getByRole('table', { name: 'Задачи run и их зависимости' }).getByRole('button', { name: b.title });
  await tabTo(page, select, { max: 40 });
  await page.keyboard.press('Enter');
  await expect(select).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('context-panel').getByTestId('mc-task-inspector')).toBeVisible();

  const ask = page.getByTestId('command-center').getByRole('button', { name: 'Вопрос', exact: true });
  await tabTo(page, ask, { max: 120 });
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('command-center').getByLabel('Текст поручения')).toBeFocused();
  await page.keyboard.type('Что осталось сделать?');
  const send = page.getByTestId('command-center').getByRole('button', { name: 'Отправить поручение' });
  await tabTo(page, send, { max: 20 });
  await page.keyboard.press('Enter');
  const track = page.getByRole('dialog', { name: 'Поручение «Вопрос (ASK)»' });
  await expect(track).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(track).toHaveCount(0);
  await expect(send).toBeFocused();
});

test('responsive: 1920/1366 three panes, 768 two panes with tabs, 375 one pane — no horizontal overflow in both themes; reduced motion', async ({ page, api }) => {
  const { ws, run, b } = seed(api);
  await login(page, api);
  for (const [width, layout] of [
    [1920, 'three'],
    [1366, 'three'],
    [768, 'two'],
    [375, 'one'],
  ] as const) {
    await page.setViewportSize({ width, height: 900 });
    await openRun(page, ws, run);
    await expect(page.getByTestId('mc-layout')).toHaveAttribute('data-layout', layout);
    for (const scheme of ['dark', 'light'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
      if (SHOTS && width !== 768) await page.screenshot({ path: `${SHOTS}/mc-${width}-${scheme}.png`, fullPage: width === 375 });
    }
  }
  // 375: вкладки «Навигатор | Лента | Инспектор»; выбор задачи открывает инспектор.
  await page.getByRole('tab', { name: 'Навигатор' }).click();
  await page.getByRole('button', { name: 'Список' }).click();
  await page.getByRole('table', { name: 'Задачи run и их зависимости' }).getByRole('button', { name: b.title }).click();
  await expect(page.getByRole('tab', { name: 'Инспектор' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('mc-task-inspector')).toBeVisible();
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'mission control 375');

  // 768: две панели, «Навигатор | Инспектор» вкладками слева.
  await page.setViewportSize({ width: 768, height: 900 });
  await expect(page.getByTestId('mc-layout')).toHaveAttribute('data-layout', 'two');
  await expectNoSeriousA11yViolations(page, 'mission control 768');

  // Reduced motion: активный узел без пульсации, новых сообщений — без анимации.
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1366, height: 900 });
  await openRun(page, ws, run);
  await expect(page.getByTestId('run-graph-canvas').locator('.aicd-node-active')).toHaveCount(1);
  const running = await page.evaluate(() => document.getAnimations().filter((x) => Number(x.effect?.getComputedTiming().duration ?? 0) > 1).length);
  expect(running).toBe(0);
});
