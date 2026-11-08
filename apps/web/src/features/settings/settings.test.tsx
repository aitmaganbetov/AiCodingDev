import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Member } from '../../api/settings';
import { json, mockFetch, problem, renderApp, session, WS_ID } from '../../test/utils';
import { buildWorkspacePatch } from './WorkspacePanel';

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

const WS = { id: WS_ID, name: 'Команда 1', slug: 'team-1', role: 'owner' as const, settings: {}, version: 2, created_at: '2026-11-01T00:00:00Z' };

function wsResponse(body: object, version: number): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json', ETag: `"v${version}"` } });
}

describe('workspace settings (OQ-45 (f))', () => {
  it('buildWorkspacePatch sends only changed fields; policies only when allowed', () => {
    expect(buildWorkspacePatch(WS, { name: ' Команда 1 ', selfApproval: false }, true)).toEqual({ error: null, patch: null });
    expect(buildWorkspacePatch(WS, { name: '', selfApproval: false }, true).error).toBe('required');
    expect(buildWorkspacePatch(WS, { name: 'x'.repeat(201), selfApproval: false }, true).error).toBe('max');
    expect(buildWorkspacePatch(WS, { name: 'Новая', selfApproval: true }, true).patch).toEqual({
      name: 'Новая',
      settings: { allow_single_member_self_approval: true },
    });
    expect(buildWorkspacePatch(WS, { name: 'Новая', selfApproval: true }, false).patch).toEqual({ name: 'Новая' });
  });

  it('owner renames the workspace and enables the policy with If-Match from the ETag; 412 reloads the form', async () => {
    const user = userEvent.setup();
    let patches = 0;
    let current: object = WS;
    let etagVersion = 7;
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'owner')], next_cursor: null }),
      [`GET /api/v1/workspaces/${WS_ID}`]: () => wsResponse(current, etagVersion),
      [`PATCH /api/v1/workspaces/${WS_ID}`]: (req) => {
        patches += 1;
        if (patches === 2) return problem(412, 'precondition_failed');
        etagVersion = 8;
        current = { ...WS, ...(req.body as object), settings: { allow_single_member_self_approval: true }, version: 8 };
        return wsResponse(current, 8);
      },
    });
    renderApp(`/w/${WS_ID}/settings`);
    const form = await screen.findByRole('form', { name: 'Параметры рабочего пространства' });
    expect(screen.queryByText(/пока недоступно/)).toBeNull();
    const name = within(form).getByLabelText('Название');
    await user.clear(name);
    await user.type(name, 'Платформа');
    await user.click(within(form).getByRole('checkbox', { name: /утверждать собственные критические действия/ }));
    await user.click(within(form).getByRole('button', { name: 'Сохранить' }));
    expect(await screen.findByText('Параметры рабочего пространства сохранены.', { selector: 'p' })).toBeInTheDocument();
    const patch = calls.find((c) => c.method === 'PATCH');
    // ETag ответа (v7) приоритетнее поля version (2) — сервер мог не вернуть version в старых ответах.
    expect(patch?.headers.get('If-Match')).toBe('"v7"');
    expect(patch?.body).toEqual({ name: 'Платформа', settings: { allow_single_member_self_approval: true } });
    // Следующее изменение — с ETag ответа PATCH; 412 показывает сообщение об устаревших данных.
    const form2 = await screen.findByRole('form', { name: 'Параметры рабочего пространства' });
    const name2 = within(form2).getByLabelText('Название');
    await user.clear(name2);
    await user.type(name2, 'Платформа 2');
    await user.click(within(form2).getByRole('button', { name: 'Сохранить' }));
    expect(await screen.findByText(/изменил другой участник/)).toBeInTheDocument();
    expect(calls.filter((c) => c.method === 'PATCH')[1]?.headers.get('If-Match')).toBe('"v8"');
  });

  it('admin edits the name but not the owner-only policy; viewer sees read-only parameters', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'admin')], next_cursor: null }),
      [`GET /api/v1/workspaces/${WS_ID}`]: () => wsResponse({ ...WS, role: 'admin' }, 2),
    });
    const { unmount } = renderApp(`/w/${WS_ID}/settings`);
    const form = await screen.findByRole('form', { name: 'Параметры рабочего пространства' });
    expect(within(form).getByRole('checkbox', { name: /утверждать собственные/ })).toBeDisabled();
    expect(within(form).getByText('Политики рабочего пространства меняет только владелец.')).toBeInTheDocument();
    unmount();
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'viewer')], next_cursor: null }),
      [`GET /api/v1/workspaces/${WS_ID}`]: () => wsResponse({ ...WS, role: 'viewer' }, 2),
    });
    renderApp(`/w/${WS_ID}/settings`);
    expect(await screen.findByText('Изменять параметры могут владелец и администраторы рабочего пространства.')).toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Параметры рабочего пространства' })).toBeNull();
  });
});

describe('step-up re-auth (SC-IAM-08)', () => {
  it('step_up_required opens the password dialog; wrong password keeps it open; success rotates CSRF and retries the same request', async () => {
    const user = userEvent.setup();
    let patches = 0;
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'owner')], next_cursor: null }),
      [`GET /api/v1/workspaces/${WS_ID}`]: () => wsResponse(WS, 2),
      [`PATCH /api/v1/workspaces/${WS_ID}`]: (req) => {
        patches += 1;
        if (patches === 1) return problem(403, 'step_up_required', { max_age_seconds: 600 });
        return wsResponse({ ...WS, ...(req.body as object), settings: { allow_single_member_self_approval: true }, version: 3 }, 3);
      },
      'POST /api/v1/auth/reauthenticate': (req) =>
        (req.body as { password: string }).password === 'right-password-1'
          ? json(200, { ...session('owner'), csrf_token: 'rotated-csrf-token' })
          : problem(403, 'reauthentication_failed'),
    });
    renderApp(`/w/${WS_ID}/settings`);
    const form = await screen.findByRole('form', { name: 'Параметры рабочего пространства' });
    await user.click(within(form).getByRole('checkbox', { name: /утверждать собственные критические действия/ }));
    await user.click(within(form).getByRole('button', { name: 'Сохранить' }));
    const dialog = await screen.findByRole('dialog', { name: 'Подтвердите пароль' });
    await user.type(within(dialog).getByLabelText('Пароль'), 'wrong-password');
    await user.click(within(dialog).getByRole('button', { name: 'Подтвердить' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Неверный пароль. Повторите ввод.');
    expect(patches).toBe(1);
    await user.clear(within(dialog).getByLabelText('Пароль'));
    await user.type(within(dialog).getByLabelText('Пароль'), 'right-password-1');
    await user.click(within(dialog).getByRole('button', { name: 'Подтвердить' }));
    expect(await screen.findByText('Параметры рабочего пространства сохранены.', { selector: 'p' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Подтвердите пароль' })).toBeNull();
    const [first, retry] = calls.filter((c) => c.method === 'PATCH');
    expect(retry?.body).toEqual(first?.body);
    expect(retry?.headers.get('If-Match')).toBe(first?.headers.get('If-Match'));
    expect(retry?.headers.get('X-CSRF-Token')).toBe('rotated-csrf-token');
    const reauth = calls.filter((c) => c.method === 'POST' && c.url.pathname.endsWith('/auth/reauthenticate'));
    expect(reauth).toHaveLength(2);
  });

  it('cancelling the dialog shows step_up_required as a form error without retrying', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      [`GET /api/v1/workspaces/${WS_ID}/members`]: () => json(200, { items: [member(ME, 'Anna', 'owner'), member(OTHER, 'Boris', 'developer', 4)], next_cursor: null }),
      [`GET /api/v1/workspaces/${WS_ID}`]: () => wsResponse(WS, 2),
      [`DELETE /api/v1/workspaces/${WS_ID}/members/${OTHER}`]: () => problem(403, 'step_up_required', { max_age_seconds: 600 }),
    });
    renderApp(`/w/${WS_ID}/settings`);
    const table = await screen.findByRole('table', { name: /Участники/ });
    await user.click(within(table).getByRole('button', { name: 'Удалить участника Boris' }));
    const removeDialog = await screen.findByRole('dialog', { name: /Boris/ });
    await user.click(within(removeDialog).getByRole('button', { name: 'Удалить участника' }));
    const stepUp = await screen.findByRole('dialog', { name: 'Подтвердите пароль' });
    await user.click(within(stepUp).getByRole('button', { name: 'Отмена' }));
    expect(await within(removeDialog).findByRole('alert')).toHaveTextContent('Подтвердите пароль: действие требует повторного входа.');
    expect(calls.filter((c) => c.method === 'DELETE')).toHaveLength(1);
  });
});
