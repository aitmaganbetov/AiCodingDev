import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { emptyAnalyticsSummary, problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

/**
 * F7 «Гейты качества» (план §3.7, §5.2; D-6, D-8): обзор workspace по analytics gates в пяти
 * состояниях — «Пройдено» только PASS с VERIFIED evidence, «Недостаточно доказательств», FAIL,
 * ERROR → «Заблокировано», SKIPPED → «Не запускалось»; группы lint … security и отчёт run.
 * Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;
const id = (n: number) => `01900000-0000-7000-8000-0000000f${String(n).padStart(4, '0')}`;

type GateStats = S['AnalyticsSummary']['gates'][number];
const stats = (gate_type: string, s: Partial<GateStats>): GateStats => ({
  gate_type,
  total: 0,
  passed: 0,
  passed_verified: 0,
  passed_unverified: 0,
  failed: 0,
  errored: 0,
  skipped: 0,
  pass_rate: null,
  avg_duration_seconds: null,
  ...s,
});

function gate(n: number, gate_type: string, status: S['GateExecutionStatus'], evidence_id: string | null = null): S['GateExecution'] {
  return {
    id: id(n),
    gate_id: id(100 + n),
    gate_type,
    required: true,
    status,
    evidence_id,
    commit_sha: 'a'.repeat(40),
    error_code: status === 'ERROR' ? 'TOOL_UNAVAILABLE' : null,
    not_applicable_reason: status === 'SKIPPED' ? 'Нет браузера в окружении' : null,
    finished_at: '2026-11-08T08:55:00Z',
  };
}

function evidence(n: number, runId: string, verification_status: S['EvidenceVerificationStatus']): S['Evidence'] {
  return {
    id: id(n),
    run_id: runId,
    kind: 'test_report',
    producer: 'verification-engine',
    verification_status,
    commit_sha: 'a'.repeat(40),
    command: 'make check',
    exit_code: 0,
    created_at: '2026-11-08T08:55:00Z',
    artifact: { id: id(n + 50), kind: 'test_report', sha256: 'f'.repeat(64), size_bytes: 10, mime_type: 'application/xml', download_url: `/api/v1/artifacts/${id(n + 50)}/download` },
  };
}

function seed(api: MockApi) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const run = api.addRun(project, { status: 'COMPLETED', objective: 'Экспорт отчётов в CSV' });
  const analyticsQueries: URLSearchParams[] = [];
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', ({ url }) => {
    analyticsQueries.push(url.searchParams);
    return {
      status: 200,
      body: {
        ...emptyAnalyticsSummary(),
        project_id: url.searchParams.get('project_id'),
        gates: [
          stats('LINT', { total: 10, passed: 6, passed_verified: 4, passed_unverified: 2, failed: 1, errored: 1, skipped: 2, pass_rate: 0.75 }),
          stats('UNIT', { total: 5, passed: 5, passed_verified: 5, pass_rate: 1 }),
          stats('E2E', { total: 3, skipped: 3 }),
          stats('SAST', { total: 2, failed: 2, pass_rate: 0 }),
          stats('DOCS_CHECK', { total: 1, passed: 1, passed_unverified: 1, pass_rate: 1 }),
        ],
      },
    };
  });
  // Отчёт run: по одному gate на каждое из пяти состояний (+ неприменимый), evidence — из listRunEvidence.
  const evidences = [evidence(20, run.id, 'VERIFIED'), evidence(21, run.id, 'UNVERIFIED'), evidence(22, run.id, 'VERIFIED')];
  const gates = [
    gate(1, 'LINT', 'PASS', id(20)),
    gate(2, 'TYPECHECK', 'PASS', id(21)),
    gate(3, 'BUILD', 'ERROR'),
    gate(4, 'UNIT', 'FAIL'),
    gate(5, 'E2E', 'SKIPPED'),
    gate(6, 'COVERAGE', 'PASS'),
    gate(7, 'SECRETS', 'PASS', id(22)),
  ];
  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', ({ id: runId }) =>
    runId === run.id
      ? {
          status: 200,
          body: { run_id: run.id, quality_profile: 'strict', verdict: 'FAIL', gate_executions: gates, findings_summary: {}, findings: [], findings_total: 0 } satisfies S['QualityReport'],
        }
      : problem(404, 'not_found'),
  );
  api.route('GET', '/api/v1/runs/{id}/evidence', 'listRunEvidence', () => ({ status: 200, body: { items: evidences, next_cursor: null } }));
  api.route('GET', '/api/v1/evidence/{id}', 'getEvidence', ({ id: evId }) => {
    const e = evidences.find((x) => x.id === evId);
    return e ? { status: 200, body: e } : problem(404, 'not_found');
  });
  api.route('GET', '/api/v1/runs/{id}/findings', 'listRunFindings', () => ({ status: 200, body: { items: [], next_cursor: null } }));
  return { ws, project, run, analyticsQueries };
}

test('quality gates overview: five verification states from analytics (INSUFFICIENT EVIDENCE for PASS without verified evidence), groups and types, filters, a11y', async ({ page, api }) => {
  const { ws, project, analyticsQueries } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Гейты качества' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/quality`);
  await expect(page.getByRole('heading', { level: 1, name: 'Гейты качества' })).toBeFocused();

  const outcomes = page.getByTestId('quality-outcomes');
  await expect(outcomes.locator('[data-segment="PASS"]')).toContainText('9');
  await expect(outcomes.locator('[data-segment="INSUFFICIENT_EVIDENCE"]')).toContainText('Недостаточно доказательств');
  await expect(outcomes.locator('[data-segment="INSUFFICIENT_EVIDENCE"]')).toContainText('3');
  await expect(outcomes.locator('[data-segment="FAIL"]')).toContainText('3');
  await expect(outcomes.locator('[data-segment="BLOCKED"]')).toContainText('1');
  await expect(outcomes.locator('[data-segment="NOT_RUN"]')).toContainText('5');
  // «Пройдено» — только PASS с evidence; исходный статус — в подсказке бейджа.
  await expect(outcomes.locator('[data-segment="PASS"] [data-verification]')).toHaveAttribute('data-source', 'PASS + VERIFIED evidence');

  const groups = page.getByRole('table', { name: 'По группам проверок' });
  const lint = groups.locator('tr[data-category="lint"]');
  await expect(lint.locator('[data-state="PASS"]')).toHaveText('4');
  await expect(lint.locator('[data-state="INSUFFICIENT_EVIDENCE"]')).toHaveText('2');
  await expect(lint.locator('[data-state="BLOCKED"]')).toHaveText('1');
  await expect(lint.locator('[data-state="NOT_RUN"]')).toHaveText('2');
  await expect(lint).toContainText('50 %');
  // E2E только SKIPPED — «Не запускалось», доли нет (никогда не «успех»).
  const e2e = groups.locator('tr[data-category="e2e"]');
  await expect(e2e.locator('[data-state="NOT_RUN"]')).toHaveText('3');
  await expect(e2e.locator('[data-state="PASS"]')).toHaveText('0');
  await expect(e2e).toContainText('нет данных');
  await expect(groups.locator('tr[data-category="build"]')).toContainText('нет выполнений в окне');
  // Тип вне перечисления контракта — в «Прочие проверки», без угадывания группы.
  await expect(groups.locator('tr[data-category="other"]')).toContainText('DOCS_CHECK');
  await expect(groups.locator('tr[data-category="other"] [data-state="INSUFFICIENT_EVIDENCE"]')).toHaveText('1');
  const types = page.getByRole('table', { name: 'По типам gates' });
  await expect(types.locator('tr[data-gate-type="SAST"] [data-state="FAIL"]')).toHaveText('2');
  await expect(page.getByRole('region', { name: 'Исходы проверок' })).toContainText('GET /analytics/summary, окно');

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `quality overview (${scheme})`);
  }
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/quality-overview-1366.png`, fullPage: true });

  // Фильтры → параметры analytics (project_id, окно).
  await page.getByLabel('Проект').selectOption({ label: 'Платёжный шлюз' });
  await expect.poll(() => analyticsQueries.at(-1)?.get('project_id')).toBe(project.id);
  await page.getByLabel('Окно').selectOption('7');
  await expect(page).toHaveURL(/days=7/);
  await expect
    .poll(() => {
      const q = analyticsQueries.at(-1);
      return q ? (Date.parse(q.get('to') ?? '') - Date.parse(q.get('from') ?? '')) / 86_400_000 : null;
    })
    .toBe(7);
});

test('run quality report: groups lint…security in five states (INSUFFICIENT EVIDENCE, BLOCKED, NOT RUN), D-8 column, evidence detail; widths and themes', async ({ page, api }) => {
  const { ws, run } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/quality`);
  await page.getByRole('link', { name: /^Качество и evidence/ }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/quality/runs/${run.id}`);

  const cats = page.getByRole('list', { name: 'Группы проверок' });
  const state = (c: string) => cats.locator(`[data-category="${c}"] [data-verification]`);
  await expect(state('lint')).toHaveAttribute('data-verification', 'PASS');
  await expect(state('typecheck')).toHaveAttribute('data-verification', 'INSUFFICIENT_EVIDENCE');
  await expect(state('typecheck')).toContainText('Недостаточно доказательств');
  await expect(state('build')).toHaveAttribute('data-verification', 'BLOCKED');
  await expect(state('unit')).toHaveAttribute('data-verification', 'FAIL');
  await expect(state('e2e')).toHaveAttribute('data-verification', 'NOT_RUN');
  await expect(state('coverage')).toHaveAttribute('data-verification', 'INSUFFICIENT_EVIDENCE');
  await expect(state('security')).toHaveAttribute('data-verification', 'PASS');
  await expect(state('integration')).toHaveAttribute('data-verification', 'none');
  await expect(state('review')).toHaveAttribute('data-verification', 'none');

  const gates = page.getByRole('table', { name: 'Выполнения quality gates' });
  await expect(gates.locator('tr[data-verification-state="PASS"]')).toHaveCount(2);
  await expect(gates.getByRole('row', { name: /^TYPECHECK/ })).toContainText('Недостаточно доказательств');
  await expect(gates.getByRole('row', { name: /^E2E/ })).toContainText('Не запускалось');
  await expect(gates.getByRole('row', { name: /^BUILD/ })).toContainText('Заблокировано');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `run quality (${scheme})`);
  }
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/quality-run-1366.png`, fullPage: true });

  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const path of [`quality`, `quality/runs/${run.id}`, `quality/evidence/${id(21)}`]) {
    await page.goto(`/w/${ws.id}/${path}`);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    for (const width of [375, 768, 1366, 1920]) {
      await page.setViewportSize({ width, height: 900 });
      for (const scheme of ['light', 'dark'] as const) {
        await page.emulateMedia({ colorScheme: scheme });
        await expectNoHorizontalScroll(page);
      }
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/quality-${path.split('/')[1] ?? 'overview'}-${width}.png`, fullPage: false });
    }
  }
});

test('evidence detail: unverified evidence is flagged and never counted as proof', async ({ page, api }) => {
  const { ws } = seed(api);
  await login(page, api);
  await page.goto(`/w/${ws.id}/quality/evidence/${id(21)}`);
  await expect(page.getByTestId('evidence-verification')).toHaveText('Не подтверждено');
  await expect(page.getByText('Evidence не подтверждено Verification Engine')).toBeVisible();
  await expect(page.getByText('Evidence засчитывается как доказательство прохождения gate только в статусе «Подтверждено»')).toBeVisible();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `evidence detail (${scheme})`);
  }
});

test('quality overview: analytics error has retry, empty window says no gates (not zero success)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'viewer');
  let fail = true;
  api.route('GET', '/api/v1/analytics/summary', 'getAnalyticsSummary', () => (fail ? problem(503, 'service_unavailable') : { status: 200, body: emptyAnalyticsSummary() }));
  await login(page, api);
  await page.goto(`/w/${ws.id}/quality`);
  const tile = page.getByRole('region', { name: 'Исходы проверок' });
  await expect(tile.getByRole('alert')).toBeVisible({ timeout: 15_000 });
  fail = false;
  await tile.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('quality-no-gates')).toHaveText('За последние 30 дн. gates не выполнялись.');
  await expect(page.getByRole('table', { name: 'По группам проверок' }).locator('[data-state="PASS"]').first()).toHaveText('0');
});
