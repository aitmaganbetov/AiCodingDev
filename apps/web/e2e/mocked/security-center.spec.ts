import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi, type Reply } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

type S = components['schemas'];

/**
 * F7 Центр безопасности (план §3.7): находки workspace (GET /findings, B1) с серверными фильтрами,
 * распределение по серьёзности, CWE/контроли только из данных, риски зависимостей (SCA/SBOM) и
 * секреты (SECRETS) по типу gate из getRunQuality, evidence проверок в пяти состояниях (D-8),
 * решение по находке (FSM, If-Match, step-up, RBAC), виртуализация большого списка, ширины.
 * Скриншоты — при AICD_E2E_SCREENSHOTS_DIR.
 */
const SHOTS = process.env.AICD_E2E_SCREENSHOTS_DIR;
const id = (n: number) => `01900000-0000-7000-8000-0000000e${String(n).padStart(4, '0')}`;
const SEVERITY_ORDER: S['FindingSeverity'][] = ['critical', 'high', 'medium', 'low', 'info'];
const XSS = '<img src=x onerror="window.__xss=1">SQL-инъекция в фильтре';
const GE = { sca: id(1), secrets: id(2), sast: id(3), sbom: id(4), lint: id(5) };
const EV = { sca: id(11), secrets: id(12), sast: id(13), sbom: id(14) };

function finding(n: number, runId: string, init: Partial<S['Finding']>): S['Finding'] {
  return {
    id: id(100 + n),
    run_id: runId,
    severity: 'low',
    status: 'OPEN',
    rule_id: `rule-${n}`,
    title: `Находка ${n}`,
    source: 'gate',
    file_path: null,
    line: null,
    cwe_id: null,
    control_refs: [],
    evidence_id: null,
    gate_execution_id: null,
    fixed_in_commit_sha: null,
    resolution_reason: null,
    resolved_by: null,
    created_at: `2026-11-08T08:${String(n % 60).padStart(2, '0')}:00Z`,
    updated_at: '2026-11-08T09:00:00Z',
    task_id: null,
    version: 1,
    ...init,
  };
}

function gate(geId: string, gate_type: string, status: S['GateExecutionStatus'], evidence_id: string | null): S['GateExecution'] {
  return { id: geId, gate_id: `${geId.slice(0, -4)}9${geId.slice(-3)}`, gate_type, required: true, status, evidence_id, commit_sha: 'a'.repeat(40), finished_at: '2026-11-08T08:55:00Z' };
}

function evidence(evId: string, runId: string, verification_status: S['EvidenceVerificationStatus']): S['Evidence'] {
  return {
    id: evId,
    run_id: runId,
    kind: 'scan_report',
    producer: 'verification-engine',
    verification_status,
    commit_sha: 'a'.repeat(40),
    command: 'make scan',
    exit_code: 1,
    created_at: '2026-11-08T08:55:00Z',
    artifact: { id: `${evId.slice(0, -4)}8${evId.slice(-3)}`, kind: 'scan_report', sha256: 'e'.repeat(64), size_bytes: 10, mime_type: 'application/json', download_url: '/api/v1/artifacts/x/download' },
  };
}

function seed(api: MockApi, role: S['WorkspaceRole'] = 'owner', opts: { bulk?: number; stepUp?: boolean } = {}) {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', role);
  const pay = api.addProject(ws.id, 'Платёжный шлюз');
  const portal = api.addProject(ws.id, 'Клиентский портал');
  const r1 = api.addRun(pay, { status: 'COMPLETED', objective: 'Экспорт отчётов в CSV', quality_profile: 'strict' });
  const r2 = api.addRun(portal, { status: 'COMPLETED', objective: 'Обновление зависимостей портала', quality_profile: 'critical' });
  const findings: S['Finding'][] = [
    finding(1, r1.id, {
      severity: 'critical',
      title: 'Уязвимая версия lodash (GHSA-35jh-r3h4-6jhm)',
      rule_id: 'osv.GHSA-35jh-r3h4-6jhm',
      file_path: 'pnpm-lock.yaml',
      cwe_id: 'CWE-1321',
      control_refs: ['SSDF-PW.4.4'],
      gate_execution_id: GE.sca,
      evidence_id: EV.sca,
    }),
    finding(2, r1.id, {
      severity: 'high',
      title: 'AWS access key в конфигурации',
      rule_id: 'gitleaks.aws-access-token',
      file_path: 'config/settings.py',
      line: 12,
      cwe_id: 'CWE-798',
      control_refs: ['ASVS-2.10.4'],
      gate_execution_id: GE.secrets,
    }),
    finding(3, r1.id, {
      severity: 'high',
      status: 'ACKNOWLEDGED',
      title: XSS,
      rule_id: 'semgrep.python.sqli',
      file_path: 'src/export/csv.py',
      line: 40,
      cwe_id: 'CWE-89',
      control_refs: ['ASVS-5.3.4'],
      gate_execution_id: GE.sast,
      version: 2,
    }),
    finding(4, r2.id, { severity: 'medium', status: 'FALSE_POSITIVE', source: 'agent_review', title: 'Нет проверки прав в API портала', cwe_id: 'CWE-862' }),
    finding(5, r2.id, { severity: 'low', status: 'FIXED', title: 'Компонент без лицензии в SBOM', rule_id: 'syft.license-missing', gate_execution_id: GE.sbom }),
    finding(6, r2.id, { severity: 'critical', title: 'Критическая находка портала', rule_id: 'semgrep.js.eval', cwe_id: 'CWE-95', gate_execution_id: GE.lint }),
  ];
  for (let i = 0; i < (opts.bulk ?? 0); i++) {
    findings.push(finding(200 + i, r2.id, { severity: 'info', source: 'manual', title: `Замечание ревью №${i + 1}`, rule_id: `manual-${i + 1}` }));
  }
  const quality: Record<string, S['QualityReport']> = {
    [r1.id]: {
      run_id: r1.id,
      quality_profile: 'strict',
      verdict: 'FAIL',
      gate_executions: [gate(GE.sca, 'SCA', 'FAIL', EV.sca), gate(GE.secrets, 'SECRETS', 'FAIL', EV.secrets), gate(GE.sast, 'SAST', 'PASS', EV.sast)],
      findings_summary: {},
      findings: [],
      findings_total: 3,
    },
    [r2.id]: {
      run_id: r2.id,
      quality_profile: 'critical',
      verdict: 'PASS',
      gate_executions: [gate(GE.sbom, 'SBOM', 'PASS', EV.sbom), gate(GE.lint, 'LINT', 'PASS', null)],
      findings_summary: {},
      findings: [],
      findings_total: 3,
    },
  };
  const evidences = [evidence(EV.sca, r1.id, 'VERIFIED'), evidence(EV.secrets, r1.id, 'VERIFIED'), evidence(EV.sast, r1.id, 'UNVERIFIED'), evidence(EV.sbom, r2.id, 'VERIFIED')];
  const transitions: { headers: Record<string, string>; body: unknown }[] = [];
  let attempts = 0;

  api.route('GET', '/api/v1/findings', 'listFindings', ({ url }) => {
    const sev = url.searchParams.getAll('severity');
    const st = url.searchParams.getAll('status');
    const src = url.searchParams.getAll('source');
    const project = url.searchParams.get('project_id');
    const run = url.searchParams.get('run_id');
    const projectRuns = project ? [r1, r2].filter((r) => r.project_id === project).map((r) => r.id) : null;
    const all = findings
      .filter(
        (f) =>
          (sev.length === 0 || sev.includes(f.severity)) &&
          (st.length === 0 || st.includes(f.status)) &&
          (src.length === 0 || src.includes(f.source ?? '')) &&
          (!projectRuns || projectRuns.includes(f.run_id ?? '')) &&
          (!run || f.run_id === run),
      )
      .sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) || (a.created_at ?? '').localeCompare(b.created_at ?? ''));
    const offset = Number(url.searchParams.get('cursor') ?? 0);
    const limit = Number(url.searchParams.get('limit') ?? 50);
    const items = all.slice(offset, offset + limit);
    return { status: 200, body: { items, next_cursor: offset + limit < all.length ? String(offset + limit) : null } };
  });
  api.route('GET', '/api/v1/runs/{id}/quality', 'getRunQuality', ({ id: runId }) => {
    const q = quality[runId ?? ''];
    return q ? { status: 200, body: q } : problem(404, 'not_found');
  });
  api.route('GET', '/api/v1/evidence/{id}', 'getEvidence', ({ id: evId }) => {
    const e = evidences.find((x) => x.id === evId);
    return e ? { status: 200, body: e } : problem(404, 'not_found');
  });
  api.route('GET', '/api/v1/findings/{id}', 'getFinding', ({ id: fid }) => {
    const f = findings.find((x) => x.id === fid);
    return f ? { status: 200, body: f, headers: { ETag: `"v${f.version}"` } } : problem(404, 'not_found');
  });
  api.route('POST', '/api/v1/findings/{id}/transitions', 'transitionFinding', ({ id: fid, headers, body }): Reply => {
    const f = findings.find((x) => x.id === fid);
    if (!f) return problem(404, 'not_found');
    transitions.push({ headers, body });
    attempts += 1;
    // Пароль вводился давно: первая попытка — 403 step_up_required (SC-IAM-08), повтор после reauthenticate.
    if (opts.stepUp && attempts === 1) return problem(403, 'step_up_required', { max_age_seconds: 600 });
    if (headers['if-match'] !== `"v${f.version}"`) return problem(412, 'precondition_failed');
    const req = body as S['FindingTransitionRequest'];
    f.status = req.to_status;
    f.resolution_reason = req.reason;
    f.resolved_by = { id: '01900000-0000-7000-8000-0000000000aa', display_name: 'Анна Петрова' };
    f.version = (f.version ?? 0) + 1;
    return { status: 200, body: f, headers: { ETag: `"v${f.version}"` } };
  });
  api.route('POST', '/api/v1/auth/reauthenticate', 'reauthenticate', ({ body }) =>
    (body as S['ReauthenticateRequest']).password === 'correct horse battery staple'
      ? { status: 200, body: { ...api.session(), reauthenticated_at: '2026-11-08T09:30:00Z' } }
      : problem(403, 'reauthentication_failed'),
  );
  return { ws, pay, portal, r1, r2, findings, transitions };
}

const list = (page: import('@playwright/test').Page) => page.getByRole('list', { name: 'Находки рабочего пространства' });

test('security center: workspace findings, severity, CWE/controls from data, dependency & secrets by gate type, evidence D-8, filters', async ({ page, api }) => {
  const { ws, pay, r1, findings } = seed(api);
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Центр безопасности' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/security`);
  await expect(page.getByRole('heading', { level: 1, name: 'Центр безопасности' })).toBeFocused();
  await expect(page).toHaveTitle('Центр безопасности — AiCodingDev');

  await expect(list(page).getByRole('listitem')).toHaveCount(6);
  await expect(page.getByTestId('security-list-count')).toHaveText('Показано 6 из 6 загруженных');
  // Недоверенный текст — только текст.
  await expect(list(page).getByRole('link', { name: XSS, exact: true })).toBeVisible();
  await expect(page.locator('img[src="x"]')).toHaveCount(0);

  const severity = page.getByRole('region', { name: 'Распределение по серьёзности' });
  await expect(severity.locator('[data-severity-count="critical"]')).toContainText('2');
  await expect(severity.locator('[data-severity-count="high"]')).toContainText('2');
  await expect(severity).toContainText('По 6 находкам выборки');
  const remediation = page.getByRole('region', { name: 'Ход исправления' });
  await expect(page.getByTestId('security-unresolved')).toHaveText('Требуют решения: 4');
  await expect(remediation.locator('[data-status-count="FALSE_POSITIVE"]')).toContainText('1');

  // CWE и контроли — ровно значения находок, без выдуманных категорий OWASP.
  await expect(page.getByTestId('cwe-mappings').getByRole('button')).toHaveCount(5);
  await expect(page.getByTestId('control-mappings').getByRole('button')).toHaveText([/ASVS-2\.10\.4/, /ASVS-5\.3\.4/, /SSDF-PW\.4\.4/]);
  await expect(page.getByRole('region', { name: 'CWE и контроли' })).not.toContainText('A03:2021');

  // Зависимости (SCA/SBOM) и секреты (SECRETS) — по типу gate отчёта качества; LINT-находка не попадает никуда.
  await expect(page.getByTestId('kind-dependency-count')).toHaveAttribute('data-count', '2');
  await expect(page.getByTestId('kind-dependency-list')).toContainText('Уязвимая версия lodash');
  await expect(page.getByTestId('kind-dependency-list')).toContainText('Компонент без лицензии в SBOM');
  await expect(page.getByTestId('kind-secrets-count')).toHaveAttribute('data-count', '1');
  await expect(page.getByTestId('kind-secrets-list')).toContainText('config/settings.py:12');
  await expect(page.getByRole('region', { name: 'Обнаруженные секреты' })).toContainText('Значение секрета не передаётся в интерфейс');
  await expect(page.getByTestId('kind-dependency-list')).not.toContainText('Критическая находка портала');

  // Evidence проверок: SCA FAIL; SAST PASS с UNVERIFIED evidence — «Недостаточно доказательств»; SBOM PASS + VERIFIED — «Пройдено».
  const ev = page.getByTestId('security-evidence-list');
  await expect(ev.locator('[data-gate="SCA"] [data-verification]')).toHaveAttribute('data-verification', 'FAIL');
  await expect(ev.locator('[data-gate="SAST"] [data-verification]')).toHaveAttribute('data-verification', 'INSUFFICIENT_EVIDENCE');
  await expect(ev.locator('[data-gate="SAST"] [data-verification]')).toContainText('Недостаточно доказательств');
  await expect(ev.locator('[data-gate="SBOM"] [data-verification]')).toHaveAttribute('data-verification', 'PASS');
  await expect(ev.locator('[data-gate="LINT"]')).toHaveCount(0);

  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `security center (${scheme})`);
  }
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/security-center-1366.png`, fullPage: true });

  // Серверные фильтры: серьёзность (повторяемый параметр), статус «требуют решения», проект, запуск.
  await page.getByRole('button', { name: 'Критическая', exact: true }).click();
  await expect(page).toHaveURL(/severity=critical/);
  await expect(list(page).getByRole('listitem')).toHaveCount(2);
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'listFindings').at(-1)?.url.searchParams.getAll('severity')).toEqual(['critical']);
  await expect(page.getByRole('button', { name: 'Критическая', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Высокая', exact: true }).click();
  await expect(list(page).getByRole('listitem')).toHaveCount(4);
  await page.getByLabel('Статус исправления').selectOption('unresolved');
  await expect(list(page).getByRole('listitem')).toHaveCount(4);
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'listFindings').at(-1)?.url.searchParams.getAll('status')).toEqual(['OPEN', 'ACKNOWLEDGED']);
  await page.getByLabel('Проект').selectOption({ label: 'Платёжный шлюз' });
  await expect(list(page).getByRole('listitem')).toHaveCount(3);
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'listFindings').at(-1)?.url.searchParams.get('project_id')).toBe(pay.id);
  await page.getByLabel('Запуск').selectOption({ label: 'Экспорт отчётов в CSV' });
  await expect.poll(() => api.requests.filter((r) => r.operationId === 'listFindings').at(-1)?.url.searchParams.get('run_id')).toBe(r1.id);
  await page.getByLabel('Источник').selectOption('gate');
  await expect(list(page).getByRole('listitem')).toHaveCount(3);
  // Клиентский фильтр CWE (в API нет параметра) — по загруженной выборке.
  await page.getByTestId('cwe-mappings').getByRole('button', { name: /CWE-89/ }).click();
  await expect(list(page).getByRole('listitem')).toHaveCount(1);
  await expect(page.getByTestId('chip-cwe')).toBeVisible();
  await expect(page.getByText('Фильтр по CWE и контролю применяется к загруженным находкам')).toBeVisible();
  // Состояние фильтров — в URL: после перезагрузки то же.
  await page.reload();
  await expect(list(page).getByRole('listitem')).toHaveCount(1);
  await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
  await expect(list(page).getByRole('listitem')).toHaveCount(6);
  await expect(page).toHaveURL(`/w/${ws.id}/security`);

  // Drill-down: карточка находки.
  await list(page).getByRole('link', { name: 'AWS access key в конфигурации' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/quality/findings/${findings[1]!.id}`);
  await expect(page.getByTestId('finding-title')).toHaveText('AWS access key в конфигурации');
});

test('security center: remediation from the detail panel — FSM transition with If-Match and step-up; reviewer cannot accept risk; viewer is read-only', async ({ page, api }) => {
  const { ws, findings, transitions } = seed(api, 'owner', { stepUp: true });
  await login(page, api);
  await page.goto(`/w/${ws.id}/security`);
  const row = list(page).getByRole('listitem').filter({ hasText: 'AWS access key в конфигурации' });
  await row.getByRole('button', { name: /Подробнее и решение/ }).click();
  await expect(page).toHaveURL(new RegExp(`finding=${findings[1]!.id}`));
  const detail = page.getByTestId('finding-detail');
  await expect(detail.getByRole('heading', { name: 'Выбранная находка' })).toBeFocused();
  await expect(detail.getByTestId('detail-status')).toHaveText('Открыта');
  const group = detail.getByRole('group', { name: 'Доступные решения' });
  await expect(group.getByRole('button')).toHaveText(['Подтвердить', 'Ложное срабатывание', 'Принять риск']);
  await expectNoSeriousA11yViolations(page, 'security detail');

  await group.getByRole('button', { name: 'Подтвердить' }).click();
  const dialog = page.getByRole('dialog', { name: 'Перевести в «Подтверждена»' });
  await dialog.getByLabel('Обоснование').fill('Ключ отозван, требуется удалить из истории');
  await dialog.getByRole('button', { name: 'Подтвердить' }).click();
  // Step-up: сервер требует пароль, после ввода тот же запрос повторяется (тот же Idempotency-Key, If-Match).
  const stepUp = page.getByRole('dialog', { name: 'Подтвердите пароль' });
  await expect(stepUp).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'security step-up');
  await stepUp.getByLabel('Пароль').fill('correct horse battery staple');
  await stepUp.getByRole('button', { name: 'Подтвердить' }).click();
  await expect(dialog).toBeHidden();
  await expect(detail.getByTestId('detail-status')).toHaveText('Подтверждена');
  expect(transitions).toHaveLength(2);
  expect(transitions[0]?.headers['if-match']).toBe('"v1"');
  expect(transitions[1]?.headers['if-match']).toBe('"v1"');
  expect(transitions[1]?.headers['idempotency-key']).toBe(transitions[0]?.headers['idempotency-key']);
  expect(transitions[1]?.body).toEqual({ to_status: 'ACKNOWLEDGED', reason: 'Ключ отозван, требуется удалить из истории' });
  expect(api.requests.filter((r) => r.operationId === 'reauthenticate')).toHaveLength(1);
  // Список перечитан: статус в строке обновился.
  await expect(row).toContainText('Подтверждена');
  // Профиль critical: критическую находку run портала нельзя принять как риск.
  await list(page).getByRole('listitem').filter({ hasText: 'Критическая находка портала' }).getByRole('button', { name: /Подробнее и решение/ }).click();
  await expect(detail.getByRole('button', { name: 'Принять риск' })).toBeDisabled();
  await expect(detail.getByText('Профиль critical: критическую находку нельзя принять как риск.')).toBeVisible();
  await detail.getByRole('button', { name: 'Закрыть' }).click();
  await expect(page.getByTestId('finding-detail')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Находки', exact: true })).toBeFocused();
});

test('security center RBAC: reviewer — no "accept risk"; viewer — no decisions', async ({ page, api }) => {
  const { ws, findings } = seed(api, 'reviewer');
  await login(page, api);
  await page.goto(`/w/${ws.id}/security?finding=${findings[0]!.id}`);
  const detail = page.getByTestId('finding-detail');
  await expect(detail.getByRole('group', { name: 'Доступные решения' }).getByRole('button')).toHaveText(['Подтвердить', 'Ложное срабатывание']);
  await expect(detail.getByText('Принять риск может только администратор или владелец.')).toBeVisible();
  // Открытие по ссылке с ?finding= не уводит фокус с заголовка страницы.
  await expect(page.getByRole('heading', { level: 1, name: 'Центр безопасности' })).toBeFocused();
  // FIXED выставляет Verification Engine.
  await page.goto(`/w/${ws.id}/security?finding=${findings[4]!.id}`);
  await expect(page.getByTestId('finding-detail')).toContainText('Статус выставляет Verification Engine');
});

test('security center RBAC: viewer sees findings without decisions', async ({ page, api }) => {
  const { ws, findings } = seed(api, 'viewer');
  await login(page, api);
  await page.goto(`/w/${ws.id}/security?finding=${findings[1]!.id}`);
  const detail = page.getByTestId('finding-detail');
  await expect(detail).toContainText('У вашей роли нет права принимать решения по этой находке.');
  await expect(detail.getByRole('group', { name: 'Доступные решения' })).toHaveCount(0);
  await expect(list(page).getByRole('listitem')).toHaveCount(6);
});

test('security center: large list is virtualized with load more; error state with retry; no overflow 375/768/1366/1920 in both themes; reduced motion', async ({ page, api }) => {
  const { ws } = seed(api, 'owner', { bulk: 260 });
  await login(page, api);
  await page.goto(`/w/${ws.id}/security`);
  await expect(page.getByTestId('security-list-count')).toHaveText('Показано 100 из 100 загруженных, есть ещё');
  const rendered = await list(page).getByRole('listitem').count();
  expect(rendered).toBeGreaterThan(0);
  expect(rendered).toBeLessThan(100);
  await expect(list(page).getByRole('listitem').first()).toHaveAttribute('aria-setsize', '100');
  await expect(page.getByRole('region', { name: 'Распределение по серьёзности' })).toContainText('есть ещё');
  await page.getByRole('button', { name: /Загрузить ещё/ }).click();
  await expect(page.getByTestId('security-list-count')).toHaveText('Показано 200 из 200 загруженных, есть ещё');
  // Клавиатура: End — последний элемент списка.
  await list(page).focus();
  await page.keyboard.press('End');
  await expect(list(page).getByRole('listitem').last()).toBeFocused();
  await expect(list(page).getByRole('listitem').last()).toHaveAttribute('aria-posinset', '200');

  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const width of [375, 768, 1366, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await expectNoHorizontalScroll(page);
    }
    if (SHOTS) await page.screenshot({ path: `${SHOTS}/security-center-${width}.png`, fullPage: false });
  }
});

test('security center: findings API error shows retry; empty workspace shows empty state', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  let fail = true;
  api.route('GET', '/api/v1/findings', 'listFindings', () => (fail ? problem(503, 'service_unavailable') : { status: 200, body: { items: [], next_cursor: null } }));
  await login(page, api);
  await page.goto(`/w/${ws.id}/security`);
  await expect(page.getByTestId('error-state')).toBeVisible({ timeout: 15_000 });
  fail = false;
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('heading', { name: 'Находок нет' })).toBeVisible();
  await expect(page.getByTestId('kind-secrets-count')).toHaveAttribute('data-count', '0');
  await expectNoSeriousA11yViolations(page, 'security empty');
});
