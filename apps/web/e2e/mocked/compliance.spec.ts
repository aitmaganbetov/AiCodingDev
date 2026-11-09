import type { components } from '../../src/api/schema';
import { expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type Reply } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

const id = (n: number) => `01900000-0000-7000-8000-0000000d${String(n).padStart(4, '0')}`;
const DISCLAIMER = 'Автоматические проверки не являются официальной сертификацией (ТЗ §13).';
const VERIFIED_EV = id(40);

function evidence(n: number, runId: string, status: S['EvidenceVerificationStatus'], kind: string): S['Evidence'] {
  return {
    id: id(n),
    run_id: runId,
    kind,
    producer: 'verification-engine',
    verification_status: status,
    created_at: '2026-11-08T08:55:00Z',
    commit_sha: 'a'.repeat(40),
    command: 'make check',
    exit_code: 0,
    artifact: { id: id(n + 10), kind: 'scan_report', sha256: 'e'.repeat(64), size_bytes: 1024, mime_type: 'application/json', download_url: `/api/v1/artifacts/${id(n + 10)}/download` },
  };
}

/** Матрица run: ASVS (gate PASS, блокирующий), ISO 25010 (ручная проверка), SSDF (UNVERIFIED), ISO 29148 (N/A). */
function seed(api: MockApi, role: S['WorkspaceRole'] = 'owner') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'VERIFYING' });
  const rows: S['ComplianceRow'][] = [
    {
      control_id: id(1),
      standard_code: 'OWASP-ASVS',
      edition: '4.0.3',
      clause_ref: 'V5.3.4',
      control_code: 'ASVS-5.3.4',
      applicability: 'APPLICABLE',
      agent_role: 'security',
      verification_method: 'automated_gate',
      gate_type: 'SAST',
      gate_execution_id: id(30),
      evidence_id: VERIFIED_EV,
      status: 'PASS',
      blocking: true,
      checked_at: '2026-11-08T08:55:00Z',
      notes: null,
      reviewed_by: null,
      result_id: id(60),
    },
    {
      control_id: id(2),
      standard_code: 'ISO-25010',
      edition: '2023',
      clause_ref: '4.2.7',
      control_code: 'Q-MAINT-01',
      applicability: 'APPLICABLE',
      agent_role: 'reviewer',
      verification_method: 'manual',
      gate_type: null,
      evidence_id: null,
      status: 'MANUAL_REVIEW',
      blocking: false,
      checked_at: null,
      notes: null,
      reviewed_by: null,
      result_id: null,
    },
    {
      control_id: id(3),
      standard_code: 'NIST-SSDF',
      edition: 'SP 800-218 v1.1',
      clause_ref: 'PW.7.2',
      control_code: 'SSDF-PW.7.2',
      applicability: 'APPLICABLE',
      agent_role: 'security',
      verification_method: 'automated_gate',
      gate_type: 'SCA',
      evidence_id: null,
      status: 'UNVERIFIED',
      blocking: true,
      checked_at: null,
      notes: 'Gate SCA: ERROR (TOOL_UNAVAILABLE)',
      reviewed_by: null,
      result_id: null,
    },
    {
      control_id: id(4),
      standard_code: 'ISO-29148',
      edition: '2018',
      clause_ref: '5.2.5',
      control_code: 'REQ-01',
      applicability: 'NOT_APPLICABLE',
      agent_role: null,
      verification_method: 'document_check',
      status: 'NOT_APPLICABLE',
      blocking: false,
    },
  ];
  const controls: S['ProjectControl'][] = [
    {
      control_id: id(1),
      standard_code: 'OWASP-ASVS',
      standard_title: 'Application Security Verification Standard',
      edition: '4.0.3',
      clause_ref: 'V5.3.4',
      control_code: 'ASVS-5.3.4',
      description: 'Параметризованные запросы к БД',
      verification_method: 'automated_gate',
      gate_type: 'SAST',
      approval_state: 'approved',
      applicability: 'APPLICABLE',
      proposed_applicability: 'applicable',
      rationale: null,
      agent_role: null,
      default_agent_role: 'security',
      blocking: null,
      effective_blocking: true,
      approved_by: { id: id(90), display_name: 'Борис Иванов' },
      approved_at: '2026-11-05T10:00:00Z',
      updated_at: '2026-11-05T10:00:00Z',
    },
    {
      control_id: id(4),
      standard_code: 'ISO-29148',
      standard_title: 'Requirements engineering',
      edition: '2018',
      clause_ref: '5.2.5',
      control_code: 'REQ-01',
      description: 'Требования проверяемы и трассируемы',
      verification_method: 'document_check',
      gate_type: null,
      approval_state: 'draft',
      applicability: 'PENDING_REVIEW',
      proposed_applicability: 'applicable',
      rationale: null,
      agent_role: null,
      default_agent_role: 'requirements',
      blocking: false,
      effective_blocking: false,
      approved_by: null,
      approved_at: null,
      updated_at: null,
    },
  ];
  const reviews: { headers: Record<string, string>; body: unknown }[] = [];
  const approvals: { headers: Record<string, string>; body: unknown }[] = [];
  const evidenceList = [evidence(41, run.id, 'UNVERIFIED', 'agent_claim'), evidence(40, run.id, 'VERIFIED', 'scan_report')];

  api.route('GET', '/api/v1/runs/{id}/compliance', 'getRunCompliance', ({ url }) => {
    const status = url.searchParams.get('status');
    const standard = url.searchParams.get('standard_code');
    const items = rows.filter((r) => (!status || r.status === status) && (!standard || r.standard_code === standard));
    const summary: Record<string, number> = {};
    for (const r of rows) summary[r.status] = (summary[r.status] ?? 0) + 1;
    return { status: 200, body: { items, next_cursor: null, summary, disclaimer: DISCLAIMER } };
  });
  // OQ-45 (c): кандидаты evidence вердикта — listRunEvidence (а не evidence из отчёта run).
  api.route('GET', '/api/v1/runs/{id}/evidence', 'listRunEvidence', ({ id }) =>
    id === run.id ? { status: 200, body: { items: evidenceList, next_cursor: null } } : problem(404, 'not_found'),
  );
  // Два id в пути: второй сегмент — регулярное выражение (control_id берём из URL).
  api.route('POST', '/api/v1/runs/{id}/compliance/[^/]+/review', 'reviewComplianceControl', ({ url, headers, body }): Reply => {
    const controlId = url.pathname.split('/')[6];
    const row = rows.find((r) => r.control_id === controlId);
    if (!row) return problem(404, 'not_found');
    reviews.push({ headers, body });
    const req = body as S['ComplianceReviewRequest'];
    row.status = req.status;
    row.notes = req.notes;
    row.evidence_id = req.evidence_ids[0] ?? null;
    row.reviewed_by = { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна Петрова' };
    row.checked_at = '2026-11-08T09:15:00Z';
    return { status: 200, body: row };
  });
  api.route('GET', '/api/v1/projects/{id}/controls', 'listProjectControls', ({ url }) => {
    const state = url.searchParams.get('approval_state');
    const items = controls.filter((c) => !state || c.approval_state === state);
    return { status: 200, body: { items, next_cursor: null, quality_profile: 'strict', disclaimer: DISCLAIMER } };
  });
  api.route('POST', '/api/v1/projects/{id}/controls/[^/]+/approval', 'approveProjectControl', ({ url, headers, body }): Reply => {
    const controlId = url.pathname.split('/')[6];
    const c = controls.find((x) => x.control_id === controlId);
    if (!c) return problem(404, 'not_found');
    approvals.push({ headers, body });
    const req = body as S['ProjectControlApprovalRequest'];
    c.approval_state = 'approved';
    c.proposed_applicability = req.applicability;
    c.applicability = req.applicability === 'not_applicable' ? 'NOT_APPLICABLE' : 'APPLICABLE';
    c.rationale = req.rationale ?? null;
    c.blocking = req.blocking ?? null;
    c.effective_blocking = req.blocking ?? false;
    c.approved_by = { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна Петрова' };
    c.approved_at = '2026-11-08T09:20:00Z';
    return { status: 200, body: c };
  });
  return { ws, project, run, rows, controls, reviews, approvals };
}

test('compliance matrix: standard → control → applicability → role → check → evidence → verdict; manual review with evidence', async ({ page, api }) => {
  const { ws, project, run, reviews } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Стандарты и соответствие' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/compliance`);
  await expect(page.getByRole('heading', { level: 1, name: 'Стандарты и соответствие' })).toBeFocused();
  await expect(page.getByRole('list', { name: 'Проекты' })).toContainText('Платёжный шлюз');
  await expectNoSeriousA11yViolations(page, 'compliance index');
  await page.getByRole('link', { name: /^Матрица соответствия/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/compliance/runs/${run.id}`);

  const matrix = page.getByRole('table', { name: 'Матрица соответствия запуска' });
  await expect(matrix.locator('tbody tr')).toHaveCount(4);
  await expect(page.getByTestId('compliance-disclaimer')).toHaveText(DISCLAIMER);
  const asvs = matrix.locator('tr[data-control="ASVS-5.3.4"]');
  for (const text of ['OWASP-ASVS', '4.0.3', 'ASVS-5.3.4', 'V5.3.4', 'Применим', 'Безопасность', 'Автоматический gate', 'SAST', 'Открыть evidence', 'Соответствует (PASS)', 'Блокирует завершение']) {
    await expect(asvs).toContainText(text);
  }
  await expect(matrix.locator('tr[data-control="SSDF-PW.7.2"]')).toContainText('Не проверено');
  await expect(matrix.locator('tr[data-control="REQ-01"]')).toContainText('Неприменим');
  // Вердикт можно вынести только для ручных/UNVERIFIED контролей.
  await expect(asvs.getByRole('button')).toHaveCount(0);
  await expect(matrix.locator('tr[data-control="REQ-01"]').getByRole('button')).toHaveCount(0);
  await expectNoSeriousA11yViolations(page, 'compliance matrix');
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await expectNoSeriousA11yViolations(page, 'compliance matrix (dark)');

  // Фильтр по вердикту — на сервер.
  await page.getByLabel('Вердикт', { exact: true }).selectOption('UNVERIFIED');
  await expect(matrix.locator('tbody tr')).toHaveCount(1);
  await page.getByLabel('Вердикт', { exact: true }).selectOption('');
  await expect(matrix.locator('tbody tr')).toHaveCount(4);

  await matrix.locator('tr[data-control="Q-MAINT-01"]').getByRole('button', { name: /Вынести вердикт/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Вердикт по контролю Q-MAINT-01' });
  await expect(dialog.getByRole('checkbox')).toHaveCount(2);
  // PASS с неподтверждённым первым evidence — ошибка на клиенте, запрос не уходит.
  await dialog.getByRole('checkbox', { name: /agent_claim/ }).check();
  await dialog.getByLabel('Обоснование').fill('Цикломатическая сложность модулей экспорта ≤ 10, проверено вручную');
  await dialog.getByRole('button', { name: 'Сохранить вердикт' }).click();
  await expect(dialog.getByText('Для PASS первое evidence должно быть подтверждено (VERIFIED).')).toBeVisible();
  expect(reviews).toHaveLength(0);
  await expectNoSeriousA11yViolations(page, 'compliance review dialog');
  await dialog.getByRole('checkbox', { name: /agent_claim/ }).uncheck();
  await dialog.getByRole('checkbox', { name: /scan_report/ }).check();
  await dialog.getByRole('button', { name: 'Сохранить вердикт' }).click();
  await expect(dialog).toBeHidden();
  expect(reviews[0]?.body).toEqual({
    status: 'PASS',
    notes: 'Цикломатическая сложность модулей экспорта ≤ 10, проверено вручную',
    evidence_ids: [VERIFIED_EV],
  });
  expect(reviews[0]?.headers['idempotency-key']).toMatch(/^[0-9a-f-]{36}$/);
  const row = matrix.locator('tr[data-control="Q-MAINT-01"]');
  await expect(row).toContainText('Соответствует (PASS)');
  await expect(row).toContainText('Проверил: Анна Петрова');
  await expect(row.getByRole('button')).toHaveCount(0);

  // К контролям проекта.
  await page.getByRole('link', { name: 'Контроли проекта' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/compliance/projects/${project.id}/controls`);
});

test('project controls: blocking flag (effective + source), applicability approval with rationale', async ({ page, api }) => {
  const { ws, project, approvals } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/compliance/projects/${project.id}/controls`);
  await expect(page.getByRole('heading', { level: 1, name: 'Контроли проекта · Платёжный шлюз' })).toBeVisible();
  const table = page.getByRole('table', { name: 'Контроли стандартов проекта' });
  const asvs = table.locator('tr[data-control="ASVS-5.3.4"]');
  await expect(asvs.getByTestId('blocking')).toContainText('Блокирует');
  await expect(asvs.getByTestId('blocking')).toContainText('умолчание платформы');
  await expect(asvs).toContainText('Утверждено');
  await expect(asvs).toContainText('Борис Иванов');
  const req = table.locator('tr[data-control="REQ-01"]');
  await expect(req.getByTestId('blocking')).toContainText('Не блокирует');
  await expect(req.getByTestId('blocking')).toContainText('задано в проекте');
  await expect(req).toContainText('Черновик системы');
  await expect(page.getByTestId('compliance-disclaimer')).toHaveText(DISCLAIMER);
  await expectNoSeriousA11yViolations(page, 'project controls');

  await page.getByLabel('Состояние утверждения').selectOption('draft');
  await expect(table.locator('tbody tr')).toHaveCount(1);

  await req.getByRole('button', { name: /Утвердить/ }).click();
  const dialog = page.getByRole('dialog', { name: 'Применимость контроля REQ-01' });
  await dialog.getByLabel('Неприменим').check();
  await dialog.getByRole('button', { name: 'Утвердить применимость' }).click();
  await expect(dialog.getByText('Укажите обоснование.')).toBeVisible();
  await expect(dialog.getByLabel('Обоснование')).toBeFocused();
  expect(approvals).toHaveLength(0);
  await dialog.getByLabel('Обоснование').fill('Проект — библиотека без пользовательских требований');
  await dialog.getByLabel('Умолчание платформы').check();
  await expectNoSeriousA11yViolations(page, 'control approval dialog');
  await dialog.getByRole('button', { name: 'Утвердить применимость' }).click();
  await expect(dialog).toBeHidden();
  expect(approvals[0]?.body).toEqual({
    applicability: 'not_applicable',
    rationale: 'Проект — библиотека без пользовательских требований',
    blocking: null,
  });
  await page.getByLabel('Состояние утверждения').selectOption('');
  await expect(table.locator('tr[data-control="REQ-01"]')).toContainText('Утверждено');
  await expect(table.locator('tr[data-control="REQ-01"]').getByTestId('blocking')).toContainText('умолчание платформы');
});

test('roles: viewer sees matrix without review, reviewer cannot approve controls', async ({ page, api }) => {
  const { ws, project, run } = seed(api, 'viewer');
  await login(page, api);
  await page.goto(`/w/${ws.id}/compliance/runs/${run.id}`);
  const matrix = page.getByRole('table', { name: 'Матрица соответствия запуска' });
  await expect(matrix.locator('tbody tr')).toHaveCount(4);
  await expect(matrix.locator('tbody').getByRole('button')).toHaveCount(0);
  await expect(page.getByText('Выносить вердикт по контролям может роль «Ревьюер» и выше.')).toBeVisible();
  await page.goto(`/w/${ws.id}/compliance/projects/${project.id}/controls`);
  await expect(page.getByText('Утверждать применимость может администратор или владелец.')).toBeVisible();
  await expect(page.getByRole('table', { name: 'Контроли стандартов проекта' }).getByRole('button', { name: /Утвердить|Пересмотреть/ })).toHaveCount(0);
});
