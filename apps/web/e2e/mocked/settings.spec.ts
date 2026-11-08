import type { components } from '../../src/api/schema';
import { expectNoHorizontalScroll, expectNoSeriousA11yViolations } from '../support/a11y';
import { problem, type MockApi } from '../support/mockApi';
import { login, tabTo } from '../support/session';
import { expect, test } from './fixtures';

/** P7-13 Settings: рабочее пространство, участники и роли (owner управляет, остальные читают). */
type S = components['schemas'];
const ME = '01900000-0000-7000-8000-0000000000aa';
const BORIS = '01900000-0000-7000-8000-0000000000bb';

function mockMembers(api: MockApi, members: S['Member'][]) {
  api.route('GET', '/api/v1/workspaces/{id}/members', 'listMembers', () => ({ status: 200, body: { items: members, next_cursor: null } }));
  api.route('POST', '/api/v1/workspaces/{id}/members', 'addMember', ({ body }) => {
    const b = body as S['MemberCreateRequest'];
    if (members.some((m) => m.email === b.email)) return problem(409, 'conflict');
    const m: S['Member'] = {
      user: { id: '01900000-0000-7000-8000-0000000000cc', display_name: 'Вера Смирнова' },
      email: b.email,
      role: b.role,
      joined_at: '2026-11-08T09:00:00Z',
      version: 0,
    };
    members.push(m);
    return { status: 201, body: m, headers: { ETag: '"v0"', Location: `/api/v1/workspaces/x/members/${m.user.id}` } };
  });
  api.route('PATCH', '/api/v1/workspaces/{id}/members/[^/]+', 'updateMember', ({ url, body, headers }) => {
    const userId = url.pathname.split('/').at(-1);
    const m = members.find((x) => x.user.id === userId);
    if (!m) return problem(404, 'not_found');
    if (headers['if-match'] !== `"v${m.version}"`) return problem(412, 'precondition_failed');
    const b = body as S['MemberUpdateRequest'];
    if (m.role === 'owner' && b.role !== 'owner' && members.filter((x) => x.role === 'owner').length === 1) return problem(409, 'last_owner');
    Object.assign(m, { role: b.role, version: m.version + 1 });
    return { status: 200, body: m, headers: { ETag: `"v${m.version}"` } };
  });
  api.route('DELETE', '/api/v1/workspaces/{id}/members/[^/]+', 'removeMember', ({ url, headers }) => {
    const userId = url.pathname.split('/').at(-1);
    const i = members.findIndex((x) => x.user.id === userId);
    const m = members[i];
    if (!m) return problem(404, 'not_found');
    if (headers['if-match'] !== `"v${m.version}"`) return problem(412, 'precondition_failed');
    members.splice(i, 1);
    return { status: 204 };
  });
}

function seedMembers(): S['Member'][] {
  return [
    { user: { id: ME, display_name: 'Анна Петрова' }, email: 'anna@example.com', role: 'owner', joined_at: '2026-11-01T09:00:00Z', version: 0 },
    { user: { id: BORIS, display_name: 'Борис Иванов' }, email: 'boris@example.com', role: 'developer', joined_at: '2026-11-02T09:00:00Z', version: 2 },
  ];
}

test('settings (owner): members, change role (keyboard), last owner protected, add and remove member (light/dark axe)', async ({ page, api }) => {
  const ws = api.addWorkspace('Команда Альфа', 'alpha', 'owner');
  const members = seedMembers();
  mockMembers(api, members);
  await login(page, api);
  await expect(page.getByRole('heading', { level: 1, name: 'Обзор · Команда Альфа' })).toBeFocused();
  await page.keyboard.press('g');
  await page.keyboard.press('o');
  await expect(page).toHaveURL(`/w/${ws.id}/settings`);
  await expect(page.getByRole('heading', { level: 1, name: 'Настройки' })).toBeFocused();
  await expect(page.locator('#workspace-info')).toContainText('alpha');
  await expect(page.locator('#workspace-info')).toContainText('Изменение названия и политик рабочего пространства пока недоступно');

  const table = page.getByRole('table', { name: /Участники/ });
  await expect(table.getByRole('row')).toHaveCount(3);
  await expect(table.getByRole('row', { name: /Анна Петрова/ })).toContainText('Вы');
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousA11yViolations(page, `settings (${scheme})`);
  }

  // Смена роли с клавиатуры.
  const changeBoris = table.getByRole('button', { name: 'Изменить роль: Борис Иванов' });
  await tabTo(page, changeBoris, { max: 60 });
  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Роль участника «Борис Иванов»' });
  await expect(dialog).toBeVisible();
  await expectNoSeriousA11yViolations(page, 'change role dialog');
  await dialog.getByRole('radio', { name: 'Ревьюер' }).check();
  await dialog.getByRole('button', { name: 'Сохранить роль' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('Роль участника «Борис Иванов» изменена: Ревьюер.')).toBeVisible();
  await expect(table.getByRole('row', { name: /Борис Иванов/ })).toContainText('Ревьюер');
  const patch = api.requests.find((r) => r.operationId === 'updateMember');
  expect(patch?.headers['if-match']).toBe('"v2"');
  expect(patch?.body).toEqual({ role: 'reviewer' });

  // Последний владелец.
  await table.getByRole('button', { name: 'Изменить роль: Анна Петрова' }).click();
  const own = page.getByRole('dialog', { name: 'Роль участника «Анна Петрова»' });
  await own.getByRole('radio', { name: 'Администратор' }).check();
  await own.getByRole('button', { name: 'Сохранить роль' }).click();
  await expect(own.getByRole('alert')).toHaveText('Нельзя удалить или понизить последнего владельца.');
  await page.keyboard.press('Escape');

  // Добавление участника.
  await page.getByRole('button', { name: 'Добавить участника' }).click();
  const add = page.getByRole('dialog', { name: 'Новый участник' });
  await add.getByLabel('Электронная почта').fill('vera@example.com');
  await add.getByRole('radio', { name: 'Наблюдатель' }).check();
  await add.getByRole('button', { name: 'Добавить' }).click();
  await expect(page.getByText('Участник «Вера Смирнова» добавлен.')).toBeVisible();
  await expect(table.getByRole('row')).toHaveCount(4);
  expect(api.requests.find((r) => r.operationId === 'addMember')?.body).toEqual({ email: 'vera@example.com', role: 'viewer' });

  // Удаление с подтверждением.
  await table.getByRole('button', { name: 'Удалить участника Борис Иванов' }).click();
  const remove = page.getByRole('dialog', { name: 'Удалить участника «Борис Иванов»?' });
  await expectNoSeriousA11yViolations(page, 'remove member dialog');
  await remove.getByRole('button', { name: 'Удалить участника' }).click();
  await expect(page.getByText('Участник «Борис Иванов» удалён.')).toBeVisible();
  await expect(table.getByRole('row')).toHaveCount(3);
  expect(api.requests.find((r) => r.operationId === 'removeMember')?.headers['if-match']).toBe('"v3"');
});

test('settings (developer): read-only members, no audit link; 360px', async ({ page, api }) => {
  await page.setViewportSize({ width: 360, height: 740 });
  const ws = api.addWorkspace('Разработка', 'dev', 'developer');
  mockMembers(api, seedMembers());
  await login(page, api);
  await page.goto(`/w/${ws.id}/settings`);
  await expect(page.getByRole('table', { name: /Участники/ }).getByRole('row')).toHaveCount(3);
  await expect(page.getByRole('button', { name: /Изменить роль/ })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Добавить участника' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Журнал аудита' })).toHaveCount(0);
  await expect(page.getByText('Добавлять участников и менять роли может владелец')).toBeVisible();
  await expectNoHorizontalScroll(page);
  await expectNoSeriousA11yViolations(page, 'settings developer 360px');
});
