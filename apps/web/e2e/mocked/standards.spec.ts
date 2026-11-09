import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

/**
 * F7 «Стандарты и соответствие» (план §3.8, ТЗ §13): постоянный баннер «не сертификация», реестр
 * стандартов из контролей проекта, фильтры стандарт/контроль/применимость/блокирование/утверждение,
 * матрица run с сопоставлением evidence и подсветкой отсутствующих доказательств (PASS только с
 * VERIFIED evidence), отчёты JSON/Markdown, ширины и темы. Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;
const id = (n: number) => `01900000-0000-7000-8000-0000000a${String(n).padStart(4, '0')}`;
const SERVER_DISCLAIMER = 'Автоматические проверки не являются официальной сертификацией (ТЗ §13).';
const EV_OK = id(40);
const EV_CLAIM = id(41);

function control(n: number, init: Partial<S['ProjectControl']> & Pick<S['ProjectControl'], 'standard_code' | 'control_code'>): S['ProjectControl'] {
  return {
    control_id: id(n),
    edition: '4.0.3',
    standard_title: 'Application Security Verification Standard',
    clause_ref: init.control_code,
    description: `Контроль ${init.control_code}`,
    verification_method: 'automated_gate',
    gate_type: null,
    approval_state: 'approved',
    applicability: 'APPLICABLE',
    proposed_applicability: 'applicable',
    rationale: null,
    agent_role: null,
    default_agent_role: 'security',
    blocking: null,
    effective_blocking: false,
    approved_by: null,
    approved_at: null,
    updated_at: null,
    ...init,
  };
}

function row(n: number, init: Partial<S['ComplianceRow']> & Pick<S['ComplianceRow'], 'standard_code' | 'control_code' | 'status'>): S['ComplianceRow'] {
  return { control_id: id(n), edition: '4.0.3', clause_ref: init.control_code, applicability: 'APPLICABLE', verification_method: 'automated_gate', evidence_id: null, ...init };
}

function evidence(evId: string, runId: string, status: S['EvidenceVerificationStatus'], kind: string): S['Evidence'] {
  return {
    id: evId,
    run_id: runId,
    kind,
    producer: 'verification-engine',
    verification_status: status,
    created_at: '2026-11-08T08:55:00Z',
    artifact: { id: `${evId.slice(0, -4)}9${evId.slice(-3)}`, kind: 'scan_report', sha256: 'e'.repeat(64), size_bytes: 1024, mime_type: 'application/json', download_url: '/api/v1/artifacts/x/download' },
  };
}

function seed(api: MockApi, role: S['WorkspaceRole'] = 'owner') {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  // Мок отдаёт проекты в обратном порядке добавления: первым в списке будет «Платёжный шлюз».
  const second = api.addProject(ws.id, 'Клиентский портал');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'COMPLETED', objective: 'Экспорт отчётов в CSV' });
  const controls: S['ProjectControl'][] = [
    control(1, { standard_code: 'OWASP-ASVS', control_code: 'ASVS-5.3.4', gate_type: 'SAST', effective_blocking: true }),
    control(2, { standard_code: 'OWASP-ASVS', control_code: 'ASVS-2.10.4', gate_type: 'SECRETS', blocking: true, effective_blocking: true }),
    control(3, {
      standard_code: 'NIST-SSDF',
      edition: 'SP 800-218 v1.1',
      standard_title: 'Secure Software Development Framework',
      control_code: 'SSDF-PW.7.2',
      verification_method: 'agent_review',
      approval_state: 'draft',
      applicability: 'PENDING_REVIEW',
    }),
    control(4, { standard_code: 'ISO-25010', edition: '2023', standard_title: 'Product quality model', control_code: 'Q-MAINT-01', verification_method: 'manual' }),
    control(5, {
      standard_code: 'ISO-29148',
      edition: '2018',
      standard_title: 'Requirements engineering',
      control_code: 'REQ-01',
      verification_method: 'document_check',
      applicability: 'NOT_APPLICABLE',
      proposed_applicability: 'not_applicable',
      rationale: 'Библиотека без пользовательских требований',
    }),
  ];
  const rows: S['ComplianceRow'][] = [
    row(1, { standard_code: 'OWASP-ASVS', control_code: 'ASVS-5.3.4', status: 'PASS', gate_type: 'SAST', gate_execution_id: id(30), evidence_id: EV_OK, blocking: true }),
    row(2, { standard_code: 'OWASP-ASVS', control_code: 'ASVS-2.10.4', status: 'PASS', gate_type: 'SECRETS', gate_execution_id: id(31), evidence_id: EV_CLAIM, blocking: true }),
    row(3, { standard_code: 'NIST-SSDF', edition: 'SP 800-218 v1.1', control_code: 'SSDF-PW.7.2', status: 'UNVERIFIED', verification_method: 'agent_review' }),
    row(4, { standard_code: 'ISO-25010', edition: '2023', control_code: 'Q-MAINT-01', status: 'MANUAL_REVIEW', verification_method: 'manual' }),
    row(5, { standard_code: 'ISO-29148', edition: '2018', control_code: 'REQ-01', status: 'NOT_APPLICABLE', applicability: 'NOT_APPLICABLE', verification_method: 'document_check' }),
  ];
  const controlQueries: URLSearchParams[] = [];
  const state = { failControls: false };
  api.route('GET', '/api/v1/projects/{id}/controls', 'listProjectControls', ({ id: pid, url }) => {
    if (state.failControls) return problem(503, 'service_unavailable');
    controlQueries.push(url.searchParams);
    const approval = url.searchParams.get('approval_state');
    const items = pid === project.id ? controls.filter((c) => !approval || c.approval_state === approval) : [];
    return { status: 200, body: { items, next_cursor: null, quality_profile: 'strict', disclaimer: SERVER_DISCLAIMER } };
  });
  api.route('GET', '/api/v1/runs/{id}/compliance', 'getRunCompliance', ({ url }) => {
    const status = url.searchParams.get('status');
    const standard = url.searchParams.get('standard_code');
    const items = rows.filter((r) => (!status || r.status === status) && (!standard || r.standard_code === standard));
    const summary: Record<string, number> = {};
    for (const r of rows) summary[r.status] = (summary[r.status] ?? 0) + 1;
    return { status: 200, body: { items, next_cursor: null, summary, disclaimer: SERVER_DISCLAIMER } };
  });
  api.route('GET', '/api/v1/runs/{id}/evidence', 'listRunEvidence', () => ({
    status: 200,
    body: { items: [evidence(EV_OK, run.id, 'VERIFIED', 'scan_report'), evidence(EV_CLAIM, run.id, 'UNVERIFIED', 'agent_claim')], next_cursor: null },
  }));
  api.route('GET', '/api/v1/runs/{id}/report', 'getRunReport', ({ headers }) =>
    (headers.accept ?? '').startsWith('text/markdown')
      ? { status: 200, raw: `# Отчёт о соответствии ${run.id}\n\n${SERVER_DISCLAIMER}\n`, headers: { 'content-type': 'text/markdown; charset=utf-8', Vary: 'Accept' } }
      : {
          status: 200,
          body: {
            run,
            quality: { run_id: run.id, quality_profile: 'strict', verdict: 'PASS', gate_executions: [], findings_summary: {}, findings: [], findings_total: 0 },
            tasks: [],
            approvals: [],
            evidence: [],
            generated_at: '2026-11-08T09:20:00Z',
            disclaimer: SERVER_DISCLAIMER,
            report_artifact: null,
          } satisfies S['RunReport'],
        },
  );
  return { ws, project, second, run, controlQueries, state };
}

test('standards & compliance: certification disclaimer, standards registry from project controls, control filters', async ({ page, api }) => {
  const { ws, project, second, controlQueries } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Стандарты и соответствие' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Стандарты и соответствие' })).toBeFocused();
  await expect(page.getByTestId('certification-disclaimer')).toContainText('Не является официальной сертификацией');
  await expect(page.getByRole('note', { name: 'Не является официальной сертификацией' })).toBeVisible();

  // Реестр — по первому проекту (глобального реестра в API нет).
  await expect(page.getByLabel('Проект', { exact: true })).toHaveValue(project.id);
  const registry = page.getByTestId('standards-registry');
  await expect(registry.getByRole('listitem')).toHaveCount(4);
  const asvs = registry.locator('[data-standard="OWASP-ASVS"]');
  await expect(asvs).toContainText('Application Security Verification Standard');
  await expect(asvs.locator('dd').first()).toHaveText('2');
  await expect(registry.locator('[data-standard="NIST-SSDF"] [data-testid="registry-pending"]')).toHaveText('1');
  await expect(page.getByRole('region', { name: 'Реестр стандартов' })).toContainText(SERVER_DISCLAIMER);

  const table = page.getByRole('table', { name: 'Контроли стандартов выбранного проекта' });
  await expect(table.locator('tbody tr')).toHaveCount(5);
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `standards index (${scheme})`);
  }
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/standards-index-1366.png`, fullPage: true });

  // Фильтры: стандарт (из реестра), блокирование, применимость, поиск, утверждение (серверный).
  await asvs.getByRole('button', { name: /Показать контроли/ }).click();
  await expect(page).toHaveURL(/standard=OWASP-ASVS/);
  await expect(table.locator('tbody tr')).toHaveCount(2);
  await page.getByLabel('Контроль или пункт').fill('2.10');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table.locator('tr[data-control="ASVS-2.10.4"] [data-testid="blocking"]')).toContainText('задано в проекте');
  await page.getByLabel('Контроль или пункт').fill('');
  await page.getByLabel('Стандарт', { exact: true }).selectOption('');
  await page.getByLabel('Блокирующий').selectOption('yes');
  await expect(table.locator('tbody tr')).toHaveCount(2);
  await page.getByLabel('Блокирующий').selectOption('');
  await page.getByLabel('Применимость').selectOption('NOT_APPLICABLE');
  await expect(table.locator('tbody tr')).toHaveCount(1);
  await expect(table.locator('tr[data-control="REQ-01"]')).toBeVisible();
  await page.getByLabel('Применимость').selectOption('');
  await page.getByLabel('Состояние утверждения').selectOption('draft');
  await expect.poll(() => controlQueries.at(-1)?.get('approval_state')).toBe('draft');
  await expect(table.locator('tbody tr')).toHaveCount(1);

  // Другой проект — пустой реестр.
  await page.getByLabel('Проект', { exact: true }).selectOption(second.id);
  await expect(page.getByText('Для проекта нет контролей стандартов.')).toBeVisible();
  await page.getByLabel('Проект', { exact: true }).selectOption(project.id);
  await page.getByRole('link', { name: 'Утверждение применимости' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/compliance/projects/${project.id}/controls`);
  await expect(page.getByTestId('certification-disclaimer')).toBeVisible();
  const controls = page.getByRole('table', { name: 'Контроли стандартов проекта' });
  await page.getByLabel('Стандарт', { exact: true }).selectOption('NIST-SSDF');
  await expect(controls.locator('tbody tr')).toHaveCount(1);
  await expectNoSeriousA11yViolations(page, 'project controls with filters');
});

test('compliance matrix: evidence mapping, missing evidence highlighted, PASS only with VERIFIED evidence, filters, reports', async ({ page, api }) => {
  const { ws, run } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/compliance`);
  await page.getByRole('link', { name: /^Матрица соответствия/ }).first().click();
  await expect(page).toHaveURL(`/w/${ws.id}/compliance/runs/${run.id}`);
  await expect(page.getByTestId('certification-disclaimer')).toContainText('Это не официальная сертификация');
  await expect(page.getByTestId('compliance-disclaimer')).toHaveText(SERVER_DISCLAIMER);

  const matrix = page.getByRole('table', { name: 'Матрица соответствия запуска' });
  await expect(matrix.locator('tbody tr')).toHaveCount(5);
  const gaps = page.getByTestId('evidence-gaps');
  await expect(gaps).toHaveAttribute('data-missing', '2');
  await expect(gaps).toHaveAttribute('data-unverified', '1');
  await expect(gaps).toContainText('2 применимых контролей без evidence, 1 — с неподтверждённым evidence');

  const r = (code: string) => matrix.locator(`tr[data-control="${code}"]`);
  await expect(r('ASVS-5.3.4')).toHaveAttribute('data-evidence-gap', 'verified');
  await expect(r('ASVS-5.3.4').locator('[data-verification]')).toHaveAttribute('data-verification', 'PASS');
  // PASS с неподтверждённым evidence — не «Пройдено».
  await expect(r('ASVS-2.10.4')).toHaveAttribute('data-evidence-gap', 'unverified');
  await expect(r('ASVS-2.10.4')).toContainText('Evidence не подтверждено (Не подтверждено)');
  await expect(r('ASVS-2.10.4').locator('[data-verification]')).toHaveAttribute('data-verification', 'INSUFFICIENT_EVIDENCE');
  await expect(r('SSDF-PW.7.2')).toHaveAttribute('data-evidence-gap', 'missing');
  await expect(r('SSDF-PW.7.2')).toContainText('Нет evidence');
  await expect(r('Q-MAINT-01')).toContainText('Нет evidence');
  await expect(r('Q-MAINT-01').locator('[data-verification]')).toHaveAttribute('data-verification', 'MANUAL_REVIEW');
  await expect(r('REQ-01')).toHaveAttribute('data-evidence-gap', 'not_required');
  await expect(r('REQ-01')).not.toContainText('Нет evidence');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `compliance matrix (${scheme})`);
  }
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/compliance-matrix-1366.png`, fullPage: true });

  // Фильтры: «только без доказательств» и контроль — по загруженной матрице; стандарт — сервер.
  const missingOnly = page.getByRole('button', { name: 'Только без доказательств' });
  await missingOnly.click();
  await expect(missingOnly).toHaveAttribute('aria-pressed', 'true');
  await expect(matrix.locator('tbody tr')).toHaveCount(3);
  await missingOnly.click();
  await page.getByLabel('Контроль или пункт').fill('5.3');
  await expect(matrix.locator('tbody tr')).toHaveCount(1);
  await page.getByLabel('Контроль или пункт').fill('');
  await page.getByLabel('Стандарт', { exact: true }).selectOption('OWASP-ASVS');
  await expect(matrix.locator('tbody tr')).toHaveCount(2);

  // Отчёты о соответствии (существующие JSON/Markdown).
  const reports = page.getByRole('region', { name: 'Отчёты о соответствии' });
  const json = page.waitForEvent('download');
  await reports.getByRole('button', { name: 'Скачать отчёт (JSON)' }).click();
  expect((await json).suggestedFilename()).toBe(`run-${run.id}-report.json`);
  const md = page.waitForEvent('download');
  await reports.getByRole('button', { name: 'Скачать отчёт (Markdown)' }).click();
  const mdFile = await md;
  expect(mdFile.suggestedFilename()).toBe(`run-${run.id}-report.md`);
  expect(Buffer.concat(await (await mdFile.createReadStream()).toArray()).toString('utf8')).toContain('Отчёт о соответствии');
  await expect(reports.getByText(SERVER_DISCLAIMER)).toBeVisible();
});

test('standards & compliance: no horizontal overflow 375/768/1366/1920 in both themes; reduced motion; controls API error with retry', async ({ page, api }) => {
  const { ws, project, run, state } = seed(api);
  await login(page, api);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const path of ['compliance', `compliance/runs/${run.id}`, `compliance/projects/${project.id}/controls`]) {
    await page.goto(`/w/${ws.id}/${path}`);
    await expect(page.getByTestId('certification-disclaimer')).toBeVisible();
    for (const width of [375, 768, 1366, 1920]) {
      await page.setViewportSize({ width, height: 900 });
      for (const scheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoHorizontalScroll(page);
      }
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/standards-${path.split('/')[1] ?? 'index'}-${width}.png`, fullPage: false });
    }
  }

  state.failControls = true;
  await page.goto(`/w/${ws.id}/compliance`);
  const registry = page.getByRole('region', { name: 'Реестр стандартов' });
  await expect(registry.getByRole('alert')).toBeVisible({ timeout: 15_000 });
  state.failControls = false;
  await registry.getByRole('button', { name: 'Повторить' }).click();
  await expect(registry.getByTestId('standards-registry').getByRole('listitem')).toHaveCount(4);
});
