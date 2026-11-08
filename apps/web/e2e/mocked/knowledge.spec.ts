import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

/** P7-12 Knowledge/Documentation: источники, refresh с согласием, регистрация, режим OFF/SMART/STRICT. */
type S = components['schemas'];

const TECH_VERSION = '01900000-0000-7000-a200-000000000002';

function source(n: number, init: Partial<S['DocumentationSource']> = {}, workspaceId = ''): S['DocumentationSource'] {
  return {
    id: `01900000-0000-7000-d000-${String(n).padStart(12, '0')}`,
    workspace_id: workspaceId,
    technology_version_id: TECH_VERSION,
    source_type: 'url',
    uri_or_file_ref: `https://docs.python.org/3.12/${n}`,
    trust_level: 'official',
    license_info: 'PSF',
    refresh_policy: 'manual',
    enabled: true,
    latest_snapshot: null,
    created_at: '2026-11-01T00:00:00Z',
    ...init,
  };
}

function mockKnowledge(api: MockApi, sources: S['DocumentationSource'][], policies: Map<string, S['DocumentationPolicy']>) {
  api.route('GET', '/api/v1/documentation/sources', 'listDocumentationSources', ({ url, workspaceId }) => {
    const enabled = url.searchParams.get('enabled');
    const items = sources.filter((s) => s.workspace_id === workspaceId && (enabled === null || String(s.enabled) === enabled));
    return { status: 200, body: { items, next_cursor: null } };
  });
  api.route('POST', '/api/v1/documentation/sources', 'registerDocumentationSource', ({ body, workspaceId }) => {
    const b = body as S['DocumentationSourceCreateRequest'];
    const created = source(sources.length + 1, { ...b, license_info: b.license_info ?? null, refresh_policy: b.refresh_policy ?? 'manual', enabled: b.enabled ?? true }, workspaceId);
    sources.push(created);
    return { status: 201, body: created, headers: { ETag: '"v0"', Location: `/api/v1/documentation/sources/${created.id}` } };
  });
  api.route('POST', '/api/v1/documentation/sources/{id}/refresh', 'refreshDocumentationSource', ({ id }) => {
    const s = sources.find((x) => x.id === id);
    if (!s) return problem(404, 'not_found');
    if (!s.enabled) return problem(409, 'invalid_state_transition');
    return { status: 202, body: { source_id: s.id, external_action_id: '01900000-0000-7000-f000-000000000001', status: 'QUEUED', accepted_at: '2026-11-08T09:00:00Z' } };
  });
  api.route('GET', '/api/v1/projects/{id}/documentation-policy', 'getDocumentationPolicy', ({ id }) => {
    const p = id ? policies.get(id) : undefined;
    return p ? { status: 200, body: p, headers: { ETag: `"v${p.settings_version}"` } } : problem(404, 'not_found');
  });
  api.route('PATCH', '/api/v1/projects/{id}/documentation-policy', 'updateDocumentationPolicy', ({ id, body, headers }) => {
    const p = id ? policies.get(id) : undefined;
    if (!p) return problem(404, 'not_found');
    if (headers['if-match'] !== `"v${p.settings_version}"`) return problem(412, 'precondition_failed');
    const next = { ...p, ...(body as S['DocumentationPolicyUpdateRequest']), settings_version: p.settings_version + 1 };
    policies.set(p.project_id, next);
    return { status: 200, body: next, headers: { ETag: `"v${next.settings_version}"` } };
  });
  api.route('GET', '/api/v1/projects/{id}/technologies', 'listTechnologies', () => ({
    status: 200,
    body: {
      items: [
        {
          technology_id: '01900000-0000-7000-a200-000000000001',
          name: 'Python',
          ecosystem: 'pypi',
          technology_version_id: TECH_VERSION,
          version_label: '3.12',
          support_status: 'supported',
          detected_from: 'pyproject.toml',
          confirmed_by: null,
        },
      ],
      next_cursor: null,
    },
  }));
}

test('knowledge: sources table, refresh needs explicit consent, register source, documentation policy (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'admin');
  const project = api.addProject(ws.id, 'Платёжный шлюз');
  const sources = [
    source(1, { latest_snapshot: { id: '01900000-0000-7000-d100-000000000001', content_hash: 'sha256:ab', fetched_at: '2026-11-07T10:00:00Z', status: 'READY' } }, ws.id),
    source(2, { enabled: false, uri_or_file_ref: 'https://legacy.example.com/docs', trust_level: 'community' }, ws.id),
  ];
  const policies = new Map([[project.id, { project_id: project.id, documentation_mode: 'SMART' as const, settings_version: 5 }]]);
  mockKnowledge(api, sources, policies);
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Команда Альфа' })).toBeFocused();
  await page.keyboard.press('g');
  await page.keyboard.press('k');
  await expect(page).toHaveURL(`/w/${ws.id}/knowledge`);
  await expect(page.getByRole('heading', { level: 1, name: 'Знания и документация' })).toBeFocused();

  const table = page.getByRole('table', { name: 'Источники документации' });
  await expect(table.getByRole('row')).toHaveCount(3);
  await expect(table.locator(`[data-source-id="${sources[1]?.id}"]`)).toContainText('Источник выключен — обновление недоступно');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `knowledge (${scheme})`);
  }

  // Фильтр по состоянию.
  await page.getByLabel('Показать').selectOption('disabled');
  await expect(table.getByRole('row')).toHaveCount(2);
  expect(api.requests.filter((r) => r.operationId === 'listDocumentationSources').length).toBeGreaterThanOrEqual(2);
  await page.getByLabel('Показать').selectOption('all');

  // Refresh: без подтверждения запрос не уходит; Escape отменяет, фокус возвращается на кнопку.
  const refresh = table.getByRole('button', { name: 'Обновить кэш источника https://docs.python.org/3.12/1' });
  await tabTo(page, refresh, { max: 80 });
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Загрузить документацию?' });
  await expect(dialog).toContainText('согласие на загрузку');
  await expectNoSeriousA11yViolations(page, 'refresh consent dialog');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(refresh).toBeFocused();
  expect(api.requests.some((r) => r.operationId === 'refreshDocumentationSource')).toBe(false);
  await page.keyboard.press('Enter');
  await dialog.getByRole('button', { name: 'Подтвердить загрузку' }).click();
  await expect(page.getByText(/поставлено в очередь \(действие 01900000-0000-7000-f000-000000000001\)/)).toBeVisible();
  expect(api.requests.find((r) => r.operationId === 'refreshDocumentationSource')?.headers['idempotency-key']).toBeTruthy();

  // Регистрация источника: валидация, затем успех.
  await page.getByRole('button', { name: 'Зарегистрировать источник' }).click();
  const reg = page.getByRole('dialog', { name: 'Новый источник документации' });
  await reg.getByRole('button', { name: 'Зарегистрировать' }).click();
  await expect(reg.getByLabel('Проект', { exact: true })).toHaveAttribute('aria-invalid', 'true');
  await reg.getByLabel('Проект', { exact: true }).selectOption(project.id);
  await reg.getByLabel('Технология и версия').selectOption(TECH_VERSION);
  await reg.getByLabel('Адрес или ссылка').fill('http://insecure.example.com');
  await reg.getByRole('button', { name: 'Зарегистрировать' }).click();
  await expect(reg.getByText('Укажите адрес, начинающийся с https://.')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'register source (errors)');
  await reg.getByLabel('Адрес или ссылка').fill('https://fastapi.tiangolo.com/');
  await reg.getByRole('radio', { name: 'Внутренняя' }).check();
  await reg.getByRole('button', { name: 'Зарегистрировать' }).click();
  await expect(reg).toHaveCount(0);
  await expect(page.getByText('Источник документации зарегистрирован.')).toBeVisible();
  await expect(table.getByRole('row')).toHaveCount(4);
  expect(api.requests.find((r) => r.operationId === 'registerDocumentationSource')?.body).toEqual({
    technology_version_id: TECH_VERSION,
    source_type: 'url',
    uri_or_file_ref: 'https://fastapi.tiangolo.com/',
    trust_level: 'internal',
    refresh_policy: 'manual',
    enabled: true,
  });

  // Режим документации проекта.
  await page.getByLabel('Проект', { exact: true }).selectOption(project.id);
  const strict = page.getByRole('radio', { name: 'Строгий (STRICT)' });
  await expect(page.getByRole('radio', { name: 'Умный (SMART)' })).toBeChecked();
  await strict.check();
  await page.getByRole('button', { name: 'Сохранить режим' }).click();
  await expect(page.getByText('Режим документации изменён: Строгий (STRICT).')).toBeVisible();
  const patch = api.requests.find((r) => r.operationId === 'updateDocumentationPolicy');
  expect(patch?.headers['if-match']).toBe('"v5"');
  expect(patch?.body).toEqual({ documentation_mode: 'STRICT' });
  await expectNoSeriousA11yViolations(page, 'documentation policy');
});

test('knowledge: viewer read-only (no register/refresh, policy read-only), empty state, 360px', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ro = api.addWorkspace('Наблюдатели', 'ro', 'viewer');
  const project = api.addProject(ro.id, 'Сервис');
  mockKnowledge(api, [], new Map([[project.id, { project_id: project.id, documentation_mode: 'OFF' as const, settings_version: 1 }]]));
  await login(page, api);
  await page.goto(`/w/${ro.id}/knowledge`);
  await expect(page.getByTestId('empty-state')).toContainText('Источники не зарегистрированы');
  await expect(page.getByRole('button', { name: 'Зарегистрировать источник' })).toHaveCount(0);
  await page.getByLabel('Проект', { exact: true }).selectOption(project.id);
  await expect(page.getByText('Выключен (OFF)')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Сохранить режим' })).toHaveCount(0);
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'knowledge viewer 360px');
});
