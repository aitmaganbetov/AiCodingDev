import type { components } from '../../src/api/schema';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type Reply } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

const SHA = 'a'.repeat(40);
const ARTIFACT_SHA = '3f'.repeat(32);
const id = (n: number) => `01900000-0000-7000-8000-0000000c${String(n).padStart(4, '0')}`;
const E2E_GATE = id(4);
const DOCS_GATE = id(6);
const UNIT_EVIDENCE = id(20);
const XSS = '<img src=x onerror="window.__xss=1">SQL-инъекция в фильтре отчёта';

function gate(n: number, gate_type: string, status: S['GateExecutionStatus'], init: Partial<S['GateExecution']> = {}): S['GateExecution'] {
  return {
    id: id(n),
    gate_id: id(100 + n),
    gate_type,
    required: true,
    status,
    commit_sha: SHA,
    error_code: null,
    not_applicable_reason: null,
    evidence_id: null,
    started_at: '2026-11-08T08:50:00Z',
    finished_at: status === 'RUNNING' || status === 'PENDING' ? null : '2026-11-08T08:55:00Z',
    task_id: null,
    ...init,
  };
}

function finding(n: number, severity: S['FindingSeverity'], status: S['FindingStatus'], runId: string, init: Partial<S['Finding']> = {}): S['Finding'] {
  return {
    id: id(50 + n),
    run_id: runId,
    severity,
    status,
    rule_id: `semgrep.rule-${n}`,
    title: `Находка ${n}`,
    source: 'gate',
    file_path: 'src/export/csv.py',
    line: 10 * n,
    cwe_id: 'CWE-89',
    control_refs: ['ASVS-5.3.4'],
    evidence_id: null,
    gate_execution_id: id(3),
    fixed_in_commit_sha: null,
    resolution_reason: null,
    resolved_by: null,
    created_at: '2026-11-08T08:56:00Z',
    updated_at: '2026-11-08T08:56:00Z',
    task_id: null,
    version: 2,
    ...init,
  };
}

/**
 * Run в VERIFYING: обязательные LINT — SKIPPED, SAST — ERROR (оба НЕ PASS), E2E ещё RUNNING.
 * По SSE приходят gate.completed: E2E FAIL (seq 1) и DOCS NOT_APPLICABLE (seq 2).
 */
function seed(api: MockApi, role: S['WorkspaceRole'] = 'owner') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'VERIFYING' });
  const task = api.addTask(run, 'Реализовать экспорт', { status: 'VERIFYING' });
  api.addEvent(run, 'gate.completed', { gate_execution_id: E2E_GATE, gate_type: 'BROWSER_TESTS', status: 'FAIL', required: true, commit_sha: SHA });
  api.addEvent(run, 'gate.completed', { gate_execution_id: DOCS_GATE, gate_type: 'DOCS_CHECK', status: 'NOT_APPLICABLE', required: false, commit_sha: SHA });

  const gatesBefore: S['GateExecution'][] = [
    gate(1, 'UNIT_TESTS', 'PASS', { evidence_id: UNIT_EVIDENCE, task_id: task.id }),
    gate(2, 'LINT', 'SKIPPED', { not_applicable_reason: 'ruff не найден в окружении' }),
    gate(3, 'SAST', 'ERROR', { error_code: 'TOOL_UNAVAILABLE' }),
    gate(4, 'BROWSER_TESTS', 'RUNNING'),
  ];
  const gatesAfter: S['GateExecution'][] = [
    ...gatesBefore.slice(0, 3),
    gate(4, 'BROWSER_TESTS', 'FAIL', { error_code: 'TESTS_FAILED' }),
    gate(6, 'DOCS_CHECK', 'NOT_APPLICABLE', { required: false, not_applicable_reason: 'Документация не изменялась' }),
  ];
  const findings: S['Finding'][] = [
    finding(1, 'high', 'OPEN', run.id, { title: XSS }),
    finding(2, 'medium', 'ACKNOWLEDGED', run.id),
    finding(3, 'low', 'FIXED', run.id),
  ];
  const transitions: { headers: Record<string, string>; body: unknown }[] = [];
  const quality = (): S['QualityReport'] => {
    const final = api.sseConnections.length >= 2;
    const open = findings.filter((f) => f.status === 'OPEN' || f.status === 'ACKNOWLEDGED');
    const summary: Record<string, number> = {};
    for (const f of open) summary[f.severity] = (summary[f.severity] ?? 0) + 1;
    const byStatus: Record<string, number> = {};
    for (const f of findings) byStatus[f.status] = (byStatus[f.status] ?? 0) + 1;
    return {
      run_id: run.id,
      quality_profile: 'strict',
      verdict: final ? 'FAIL' : 'RUNNING',
      gate_executions: final ? gatesAfter : gatesBefore,
      findings_summary: summary,
      findings_by_status: byStatus,
      findings,
      findings_total: findings.length,
    };
  };
  const evidence: S['Evidence'] = {
    id: UNIT_EVIDENCE,
    run_id: run.id,
    task_id: task.id,
    kind: 'test_report',
    producer: 'verification-engine/pytest',
    verification_status: 'VERIFIED',
    commit_sha: SHA,
    command: 'uv run pytest -q tests/unit',
    exit_code: 0,
    started_at: '2026-11-08T08:50:00Z',
    finished_at: '2026-11-08T08:55:00Z',
    environment: { python: '3.12.7', os: 'linux' } as unknown as S['Evidence']['environment'],
    created_at: '2026-11-08T08:55:01Z',
    links: [{ target_type: 'gate_execution', target_id: id(1), relation: 'produced_by' }],
    artifact: {
      id: id(30),
      kind: 'test_report',
      sha256: ARTIFACT_SHA,
      size_bytes: 20480,
      mime_type: 'application/xml',
      download_url: `/api/v1/artifacts/${id(30)}/download`,
    },
  };
  const bareEvidence: S['Evidence'] = {
    ...evidence,
    id: id(21),
    kind: 'cli_output',
    producer: 'verification-engine/claims',
    verification_status: 'UNVERIFIED',
    commit_sha: null,
    command: null,
    exit_code: null,
    environment: {},
    links: [],
  };

  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', ({ id: runId }) =>
    runId === run.id ? { status: 200, body: quality() } : problem(404, 'not_found'),
  );
  api.route('GET', '/api/v1/runs/{id}/findings', 'listRunFindings', ({ url }) => {
    const sev = url.searchParams.getAll('severity');
    const st = url.searchParams.getAll('status');
    const items = findings.filter((f) => (sev.length === 0 || sev.includes(f.severity)) && (st.length === 0 || st.includes(f.status)));
    return { status: 200, body: { items, next_cursor: null } };
  });
  api.route('GET', '/api/v1/findings/{id}', 'getFinding', ({ id: fid }) => {
    const f = findings.find((x) => x.id === fid);
    return f ? { status: 200, body: f, headers: { ETag: `"v${f.version}"` } } : problem(404, 'not_found');
  });
  api.route('POST', '/api/v1/findings/{id}/transitions', 'transitionFinding', ({ id: fid, headers, body }): Reply => {
    const f = findings.find((x) => x.id === fid);
    if (!f) return problem(404, 'not_found');
    transitions.push({ headers, body });
    if (headers['if-match'] !== `"v${f.version}"`) return problem(412, 'precondition_failed');
    const req = body as S['FindingTransitionRequest'];
    f.status = req.to_status;
    f.resolution_reason = req.reason;
    f.resolved_by = req.to_status === 'OPEN' ? null : { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна Петрова' };
    f.version = (f.version ?? 0) + 1;
    return { status: 200, body: f, headers: { ETag: `"v${f.version}"` } };
  });
  api.route('GET', '/api/v1/evidence/{id}', 'getEvidence', ({ id: eid }) =>
    eid === evidence.id ? { status: 200, body: evidence } : eid === bareEvidence.id ? { status: 200, body: bareEvidence } : problem(404, 'not_found'),
  );
  api.route('GET', '/api/v1/tasks/{id}/evidence', 'listTaskEvidence', ({ id: tid, url }) => {
    const v = url.searchParams.get('verification_status');
    const items = [evidence, bareEvidence].filter((e) => e.task_id === tid && (!v || e.verification_status === v));
    return { status: 200, body: { items, next_cursor: null } };
  });
  api.route('GET', '/api/v1/runs/{id}/report', 'getRunReport', () => ({
    status: 200,
    body: {
      run,
      quality: quality(),
      tasks: [task],
      approvals: [],
      evidence: [evidence],
      findings,
      generated_at: '2026-11-08T09:20:00Z',
      disclaimer: 'Автоматические проверки не являются официальной сертификацией.',
      report_artifact: null,
    } satisfies S['RunReport'],
  }));
  return { ws, project, run, task, findings, transitions, evidence, bareEvidence };
}

test('quality: SKIPPED/ERROR are not PASS; gate results arrive via SSE, reconnect with Last-Event-ID without duplicates (AC-08, AC-11)', async ({ page, api }) => {
  const { ws, run } = seed(api);
  // 1-е подключение: событие 1 и обрыв посреди кадра 2; 2-е — «сервер» повторяет 1 и досылает 2.
  api.sseScripts = [{ toSeq: 1, cut: true }, { fromSeq: 1 }];
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Evidence и качество' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/quality`);
  await expect(page.getByRole('heading', { level: 1, name: 'Evidence и качество' })).toBeFocused();
  await expectNoSeriousA11yViolations(page, 'quality index');
  await page.getByRole('link', { name: /^Качество и evidence/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/quality/runs/${run.id}`);

  const gates = page.getByRole('table', { name: 'Выполнения quality gates' });
  // После доставки по SSE (с переподключением) — итоговое состояние: 5 gates, verdict FAIL.
  await expect(gates.getByRole('row')).toHaveCount(6);
  await expect(page.getByTestId('quality-verdict')).toHaveText('Не пройден');
  expect(api.sseConnections[0]?.lastEventId).toBeUndefined();
  expect(api.sseConnections[1]?.lastEventId).toBe('1');
  await expect.poll(() => api.sseConnections[2]?.lastEventId).toBe('2');
  const gateIds = await gates.locator('tbody tr').evaluateAll((rows) => rows.map((r) => r.textContent));
  expect(new Set(gateIds).size).toBe(gateIds.length);

  // SKIPPED и ERROR — явно «не PASS».
  await expect(page.getByTestId('required-gates')).toHaveText('пройдено 1 из 4');
  await expect(page.getByTestId('not-pass-warning')).toContainText('LINT (Пропущен (не PASS))');
  await expect(page.getByTestId('not-pass-warning')).toContainText('SAST (Ошибка (не PASS))');
  const lint = gates.getByRole('row', { name: /^LINT/ });
  await expect(lint).toContainText('Нет — пропущен (SKIPPED ≠ PASS)');
  await expect(lint).toContainText('ruff не найден в окружении');
  await expect(lint.locator('[data-status="SKIPPED"]')).toBeVisible();
  await expect(gates.getByRole('row', { name: /^SAST/ })).toContainText('Нет — ошибка выполнения (ERROR ≠ PASS)');
  await expect(gates.getByRole('row', { name: /^SAST/ })).toContainText('TOOL_UNAVAILABLE');
  await expect(gates.getByRole('row', { name: /^BROWSER_TESTS/ })).toContainText('Не пройден');
  await expect(gates.getByRole('row', { name: /^DOCS_CHECK/ })).toContainText('Не требуется — неприменим');
  await expect(gates.getByRole('row', { name: /^UNIT_TESTS/ })).toContainText('Да');
  await expect(page.getByTestId('live-state')).not.toHaveAttribute('data-state', 'failed');
  await expectNoSeriousA11yViolations(page, 'run quality gates');
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await expectNoSeriousA11yViolations(page, 'run quality gates (dark)');

  // Evidence gate (AC-14): commit, команда, exit code, время, окружение, sha256 артефакта.
  await gates.getByRole('row', { name: /^UNIT_TESTS/ }).getByRole('link', { name: /Открыть evidence/ }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Evidence' })).toBeVisible();
  await expect(page.getByTestId('evidence-commit')).toHaveText(SHA);
  await expect(page.getByTestId('evidence-exit-code')).toHaveText('0');
  await expect(page.getByTestId('evidence-sha256')).toHaveText(ARTIFACT_SHA);
  await expect(page.getByRole('figure').filter({ hasText: 'Команда' })).toContainText('uv run pytest -q tests/unit');
  await expect(page.getByTestId('evidence-complete')).toBeVisible();
  await expect(page.getByTestId('evidence-verification')).toHaveText('Подтверждено');
  await expect(page.getByRole('link', { name: 'Скачать артефакт' })).toHaveAttribute('href', `/api/v1/artifacts/${id(30)}/download`);
  await expectNoSeriousA11yViolations(page, 'evidence detail');
});

test('quality: findings list with filters, finding detail renders untrusted text, FSM transition with If-Match', async ({ page, api }) => {
  const { ws, run, transitions } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/quality/runs/${run.id}?tab=findings`);
  const table = page.getByRole('table', { name: 'Находки запуска' });
  await expect(table.locator('tbody tr')).toHaveCount(3);
  await page.getByLabel('Критичность').selectOption('high');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  // Фильтр — серверный (новый запрос); SSE может добавить перечитывания, поэтому «не меньше двух».
  expect(api.requests.filter((r) => r.operationId === 'listRunFindings').length).toBeGreaterThanOrEqual(2);
  await expectNoSeriousA11yViolations(page, 'findings tab');

  // Недоверенный текст находки — только как текст.
  await table.getByRole('link', { name: XSS }).click();
  await expect(page.getByTestId('finding-title')).toHaveText(XSS);
  await expect(page.locator('img[src="x"]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  await expect(page.getByTestId('finding-status')).toHaveText('Открыта');

  // OPEN → допустимые переходы FSM (owner — все три).
  const group = page.getByRole('group', { name: 'Доступные решения' });
  await expect(group.getByRole('button')).toHaveText(['Подтвердить', 'Ложное срабатывание', 'Принять риск']);
  await expectNoSeriousA11yViolations(page, 'finding detail');
  await group.getByRole('button', { name: 'Принять риск' }).click();
  const dialog = page.getByRole('dialog', { name: 'Перевести в «Риск принят»' });
  await dialog.getByRole('button', { name: 'Принять риск' }).click();
  await expect(dialog.getByText('Обязательное поле.')).toBeVisible();
  expect(transitions).toHaveLength(0);
  await dialog.getByLabel('Обоснование').fill('Эндпоинт только для внутренних администраторов, исправление в v2.4');
  await expectNoSeriousA11yViolations(page, 'finding transition dialog');
  await dialog.getByRole('button', { name: 'Принять риск' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId('finding-status')).toHaveText('Риск принят');
  expect(transitions[0]?.headers['if-match']).toBe('"v2"');
  expect(transitions[0]?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
  expect(transitions[0]?.body).toEqual({ to_status: 'ACCEPTED_RISK', reason: 'Эндпоинт только для внутренних администраторов, исправление в v2.4' });
  // ACCEPTED_RISK → только повторное открытие.
  await expect(group.getByRole('button')).toHaveText(['Открыть повторно']);
  await expect(page.getByText('Эндпоинт только для внутренних администраторов, исправление в v2.4')).toBeVisible();
});

test('quality: reviewer cannot accept risk; FIXED is engine-only; task evidence tab; incomplete evidence is flagged; report download', async ({ page, api }) => {
  const { ws, run, findings, bareEvidence } = seed(api, 'reviewer');
  await login(page, api);
  await page.goto(`/w/${ws.id}/quality/findings/${findings[1]!.id}`);
  const group = page.getByRole('group', { name: 'Доступные решения' });
  await expect(group.getByRole('button')).toHaveText(['Открыть повторно', 'Ложное срабатывание']);
  await expect(page.getByText('Принять риск может только администратор или владелец.')).toBeVisible();
  await page.goto(`/w/${ws.id}/quality/findings/${findings[2]!.id}`);
  await expect(page.getByText('Статус выставляет Verification Engine: ручных переходов из него нет.')).toBeVisible();

  await page.goto(`/w/${ws.id}/quality/runs/${run.id}?tab=evidence`);
  await page.getByLabel('Задача').selectOption({ label: 'Реализовать экспорт' });
  const table = page.getByRole('table', { name: 'Evidence задачи' });
  await expect(table.locator('tbody tr')).toHaveCount(2);
  await page.getByLabel('Проверка').selectOption('VERIFIED');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expectNoSeriousA11yViolations(page, 'evidence tab');

  await page.goto(`/w/${ws.id}/quality/evidence/${bareEvidence.id}`);
  await expect(page.getByTestId('evidence-missing')).toHaveText('Commit, Команда, Exit code, Окружение');
  await expect(page.getByText('Evidence не подтверждено Verification Engine')).toBeVisible();

  await page.goto(`/w/${ws.id}/quality/runs/${run.id}?tab=report`);
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Скачать отчёт (JSON)' }).click();
  expect((await download).suggestedFilename()).toBe(`run-${run.id}-report.json`);
  await expect(page.getByText('Автоматические проверки не являются официальной сертификацией.')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'report tab');
});

test('quality: error state with retry and viewer has read-only findings', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'viewer');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'COMPLETED' });
  let fail = true;
  const f = finding(1, 'high', 'OPEN', run.id);
  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', () =>
    fail
      ? problem(503, 'service_unavailable')
      : {
          status: 200,
          body: { run_id: run.id, quality_profile: 'basic', verdict: 'PASS', gate_executions: [], findings_summary: {}, findings: [], findings_total: 0 },
        },
  );
  api.route('GET', '/api/v1/findings/{id}', 'getFinding', () => ({ status: 200, body: f }));
  await login(page, api);
  await page.goto(`/w/${ws.id}/quality/runs/${run.id}`);
  await expect(page.getByTestId('error-state')).toBeVisible({ timeout: 15_000 });
  fail = false;
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('quality-verdict')).toHaveText('Пройден');
  await expect(page.getByText('Gates ещё не выполнялись.')).toBeVisible();
  await expect(page.getByTestId('live-state')).toHaveAttribute('data-state', 'closed');
  await page.goto(`/w/${ws.id}/quality/findings/${f.id}`);
  await expect(page.getByText('У вашей роли нет права принимать решения по этой находке.')).toBeVisible();
});
