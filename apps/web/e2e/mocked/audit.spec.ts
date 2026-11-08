import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login } from '../support/session';
import { expect, test } from './fixtures';

/** P7-13 Audit: журнал с фильтрами (в URL) и cursor-пагинацией; доступ — owner/admin. */
type S = components['schemas'];

function events(workspaceId: string, count: number): S['AuditEvent'][] {
  return Array.from({ length: count }, (_, i) => ({
    id: `01900000-0000-7000-c000-${String(i + 1).padStart(12, '0')}`,
    workspace_id: workspaceId,
    actor_type: i % 3 === 2 ? ('system' as const) : ('user' as const),
    actor_id: i % 3 === 2 ? null : '01900000-0000-7000-8000-0000000000aa',
    action: i % 2 === 0 ? 'project.create' : 'member.role_change',
    resource_type: i % 2 === 0 ? 'project' : 'workspace_member',
    resource_id: `01900000-0000-7000-9000-${String(i + 1).padStart(12, '0')}`,
    metadata: { outcome: i === 1 ? 'denied' : 'success', request_id: `req-${i + 1}` } as unknown as S['AuditEvent']['metadata'],
    created_at: `2026-11-0${(i % 8) + 1}T10:00:00Z`,
  }));
}

function mockAudit(api: MockApi, all: S['AuditEvent'][], pageSize = 3) {
  api.route('GET', '/api/v1/audit-events', 'listAuditEvents', ({ url }) => {
    const q = url.searchParams;
    const items = all.filter(
      (e) =>
        (!q.get('action') || e.action === q.get('action')) &&
        (!q.get('resource_type') || e.resource_type === q.get('resource_type')) &&
        (!q.get('actor_id') || e.actor_id === q.get('actor_id')) &&
        (!q.get('from') || e.created_at >= (q.get('from') ?? '')),
    );
    const start = Number(q.get('cursor') ?? 0);
    const end = start + pageSize;
    return { status: 200, body: { items: items.slice(start, end), next_cursor: end < items.length ? String(end) : null } };
  });
}

test('audit: list, pagination, filters reflected in the URL, metadata details (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'admin');
  mockAudit(api, events(ws.id, 5));
  await login(page, api);
  await page.getByRole('navigation', { name: 'Основная навигация' }).getByRole('link', { name: 'Аудит' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Аудит' })).toBeFocused();
  const table = page.getByRole('table', { name: 'События аудита' });
  await expect(table.getByRole('row')).toHaveCount(4);
  await expect(table).toContainText('Отказано');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `audit (${scheme})`);
  }

  await page.getByRole('button', { name: 'Загрузить ещё' }).click();
  await expect(table.getByRole('row')).toHaveCount(6);
  await expect(page.getByRole('button', { name: 'Загрузить ещё' })).toHaveCount(0);

  const firstRow = table.getByRole('row').nth(1);
  await firstRow.getByText('Подробности').click();
  await expect(firstRow.getByText('"request_id": "req-1"')).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'audit metadata expanded');

  // Невалидный UUID — ошибка у поля, запрос не уходит.
  const before = api.requests.filter((r) => r.operationId === 'listAuditEvents').length;
  await page.getByLabel('Идентификатор пользователя').fill('anna');
  await page.getByRole('button', { name: 'Применить' }).click();
  await expect(page.getByLabel('Идентификатор пользователя')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByLabel('Идентификатор пользователя')).toBeFocused();
  expect(api.requests.filter((r) => r.operationId === 'listAuditEvents').length).toBe(before);

  await page.getByLabel('Идентификатор пользователя').fill('');
  await page.getByLabel('Действие').fill('member.role_change');
  await page.getByLabel('Действие').press('Enter');
  await expect(page).toHaveURL(`/w/${ws.id}/audit?action=member.role_change`);
  await expect(table.getByRole('row')).toHaveCount(3);
  const last = api.requests.filter((r) => r.operationId === 'listAuditEvents').at(-1);
  expect(last?.headers['x-workspace-id']).toBe(ws.id);

  // Фильтр сохраняется при перезагрузке (URL), «Сбросить» очищает.
  await page.reload();
  await expect(page.getByLabel('Действие')).toHaveValue('member.role_change');
  await expect(page.getByRole('table', { name: 'События аудита' }).getByRole('row')).toHaveCount(3);
  await page.getByLabel('Действие').fill('nothing.matches');
  await page.getByRole('button', { name: 'Применить' }).click();
  await expect(page.getByTestId('empty-state')).toContainText('Ничего не найдено');
  await expectNoSeriousA11yViolations(page, 'audit empty (filtered)');
  await page.getByRole('button', { name: 'Сбросить' }).click();
  await expect(page).toHaveURL(`/w/${ws.id}/audit`);
  await expect(page.getByRole('table', { name: 'События аудита' }).getByRole('row')).toHaveCount(4);
});

test('audit: forbidden for developer (no nav item), error state with retry, 360px', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const dev = api.addWorkspace('Разработка', 'dev', 'developer');
  const ws = api.addWorkspace('Админы', 'adm', 'owner');
  let fail = true;
  api.route('GET', '/api/v1/audit-events', 'listAuditEvents', () =>
    fail ? problem(500, 'internal_error') : { status: 200, body: { items: [], next_cursor: null } },
  );
  await login(page, api);
  await page.goto(`/w/${dev.id}/audit`);
  await expect(page.getByRole('heading', { level: 1, name: 'Недостаточно прав' })).toBeVisible();
  expect(api.requests.some((r) => r.operationId === 'listAuditEvents')).toBe(false);

  await page.goto(`/w/${ws.id}/audit`);
  const error = page.getByTestId('error-state');
  await expect(error).toContainText('Внутренняя ошибка сервера');
  await expectNoSeriousA11yViolations(page, 'audit error 360px');
  fail = false;
  await error.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByTestId('empty-state')).toContainText('Событий пока нет');
  await expectNoHorizontalScroll(page);
});
