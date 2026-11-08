import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Member } from '../../api/settings';
import { json, mockFetch, problem, renderApp, session, WS_ID } from '../../test/utils';

const ME = '01900000-0000-7000-8000-0000000000aa';
function member(id: string, name: string, role: Member['role'], version = 1): Member {
  return { user: { id, display_name: name }, email: `${name.toLowerCase()}@example.com`, role, joined_at: '2026-11-01T00:00:00Z', version };
}
const OTHER = '01900000-0000-7000-8000-0000000000bb';

describe('SettingsPage', () => {
  it('owner changes a role with If-Match; last_owner conflict is shown in the dialog', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'owner'), member(OTHER, 'Boris', 'developer', 4)], next_cursor: null }),
      [`PATCH /api/v1/workspaces/${WS_ID}/members/${OTHER}`]: () => json(200, member(OTHER, 'Boris', 'reviewer', 5)),
      [`PATCH /api/v1/workspaces/${WS_ID}/members/${ME}`]: () => problem(409, 'last_owner'),
    });
    renderApp(`/w/${WS_ID}/settings`);
    const table = await screen.findByRole('table', { name: /Участники/ });
    expect(within(table).getByText('Вы')).toBeInTheDocument();

    await user.click(within(table).getByRole('button', { name: 'Изменить роль: Boris' }));
    let dialog = await screen.findByRole('dialog', { name: 'Роль участника «Boris»' });
    await user.click(within(dialog).getByRole('radio', { name: 'Ревьюер' }));
    await user.click(within(dialog).getByRole('button', { name: 'Сохранить роль' }));
    expect(await screen.findByText('Роль участника «Boris» изменена: Ревьюер.')).toBeInTheDocument();
    const patch = calls.find((c) => c.method === 'PATCH');
    expect(patch?.headers.get('If-Match')).toBe('"v4"');
    expect(patch?.body).toEqual({ role: 'reviewer' });

    await user.click(within(table).getByRole('button', { name: 'Изменить роль: Anna' }));
    dialog = await screen.findByRole('dialog', { name: 'Роль участника «Anna»' });
    await user.click(within(dialog).getByRole('radio', { name: 'Администратор' }));
    await user.click(within(dialog).getByRole('button', { name: 'Сохранить роль' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Нельзя удалить или понизить последнего владельца.');
  });

  it('non-owners see members read-only', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'admin')], next_cursor: null }),
    });
    renderApp(`/w/${WS_ID}/settings`);
    await screen.findByRole('table', { name: /Участники/ });
    expect(screen.queryByRole('button', { name: /Изменить роль/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Добавить участника' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Журнал аудита' })).toHaveAttribute('href', `/w/${WS_ID}/audit`);
  });

  it('owner adds a member (client email validation first)', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'owner')], next_cursor: null }),
      [`POST /api/v1/workspaces/${WS_ID}/members`]: () => json(201, member(OTHER, 'Vera', 'viewer')),
    });
    renderApp(`/w/${WS_ID}/settings`);
    await user.click(await screen.findByRole('button', { name: 'Добавить участника' }));
    const dialog = await screen.findByRole('dialog', { name: 'Новый участник' });
    await user.type(within(dialog).getByLabelText('Электронная почта'), 'vera');
    await user.click(within(dialog).getByRole('button', { name: 'Добавить' }));
    expect(within(dialog).getByLabelText('Электронная почта')).toHaveAttribute('aria-invalid', 'true');
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    await user.type(within(dialog).getByLabelText('Электронная почта'), '@example.com');
    await user.click(within(dialog).getByRole('radio', { name: 'Наблюдатель' }));
    await user.click(within(dialog).getByRole('button', { name: 'Добавить' }));
    expect(await screen.findByText('Участник «Vera» добавлен.')).toBeInTheDocument();
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')?.body).toEqual({ email: 'vera@example.com', role: 'viewer' }));
  });
});
