import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Provider, ProviderInstallation } from '../api/providers';
import type { SearchResult } from '../api/search';
import { notificationFromEvent, notificationHref, type Notification, type NotificationPage } from '../api/notifications';
import type { StreamEvent } from '../api/sse';
import { CONTEXT_WIDTH_KEY, SIDEBAR_KEY, clampContextWidth, resetLayout } from '../lib/layoutPrefs';
import { json, mockFetch, problem, renderApp, session, WS_ID } from '../test/utils';
import { installationState, providerChipModel } from './providerStatus';
import { groupResults, normalizeQuery, searchResultHref } from './searchModel';
import { applyMarkRead, bumpUnread, prependNotifications } from './NotificationsData';

function inst(over: Partial<ProviderInstallation> = {}): ProviderInstallation {
  return {
    installation_id: '01900000-0000-7000-b000-000000000011',
    host: 'worker-exec-1',
    detected_version: '0.42.0',
    compatibility: 'SUPPORTED',
    auth_status: 'AUTHENTICATED',
    availability: 'AVAILABLE',
    capabilities: {},
    last_error_category: null,
    checked_at: '2026-11-08T08:00:00Z',
    ...over,
  };
}

function provider(code: Provider['code'], installations: ProviderInstallation[], over: Partial<Provider> = {}): Provider {
  return {
    provider_id: code === 'codex' ? '01900000-0000-7000-b000-000000000001' : '01900000-0000-7000-b000-000000000002',
    code,
    display_name: code === 'codex' ? 'Codex CLI' : 'Claude Code CLI',
    enabled_globally: true,
    installations,
    workspace_settings: { enabled: true, max_parallel_sessions: 2, default_timeout_seconds: 1800, allowed_roles: [], settings_version: 1 },
    ...over,
  };
}

describe('provider chip status (only measured fields, no quota)', () => {
  it('maps auth/availability/compatibility of one installation', () => {
    expect(installationState(inst())).toBe('ready');
    expect(installationState(inst({ auth_status: 'AUTH_REQUIRED' }))).toBe('auth_required');
    expect(installationState(inst({ auth_status: 'EXPIRED' }))).toBe('auth_required');
    expect(installationState(inst({ auth_status: 'UNKNOWN' }))).toBe('unknown');
    expect(installationState(inst({ checked_at: null }))).toBe('unknown');
    expect(installationState(inst({ availability: 'RATE_LIMITED' }))).toBe('rate_limited');
    expect(installationState(inst({ availability: 'UNAVAILABLE' }))).toBe('unavailable');
    expect(installationState(inst({ compatibility: 'UNSUPPORTED' }))).toBe('unsupported');
    expect(installationState(inst({ auth_status: 'ERROR' }))).toBe('error');
  });

  it('aggregates hosts (any ready host → ready), missing provider/no installations/disabled are never "ready"', () => {
    expect(providerChipModel(undefined).state).toBe('not_installed');
    expect(providerChipModel(provider('codex', [])).state).toBe('not_installed');
    expect(providerChipModel(provider('codex', [inst()], { enabled_globally: false })).state).toBe('disabled');
    expect(providerChipModel(provider('codex', [inst({ auth_status: 'AUTH_REQUIRED' }), inst()])).state).toBe('ready');
    expect(providerChipModel(provider('codex', [inst({ auth_status: 'AUTH_REQUIRED' }), inst({ availability: 'UNAVAILABLE' })])).state).toBe('auth_required');
    const model = providerChipModel(provider('claude', [inst({ checked_at: '2026-11-08T07:00:00Z' }), inst({ checked_at: '2026-11-08T09:00:00Z' })]));
    expect(model.checkedAt).toBe('2026-11-08T09:00:00Z');
  });
});

describe('global search model', () => {
  const base: SearchResult = {
    type: 'project',
    id: 'p-1',
    title: 'Проект',
    snippet: null,
    score: 1,
    created_at: '2026-11-08T08:00:00Z',
    project_id: null,
    run_id: null,
    task_id: null,
    api_path: '/api/v1/projects/p-1',
  };

  it('normalizes the query (trim, ≥ 2 chars, ≤ 200)', () => {
    expect(normalizeQuery(' a ')).toBeNull();
    expect(normalizeQuery('  ab ')).toBe('ab');
    expect(normalizeQuery('x'.repeat(250))).toHaveLength(200);
  });

  it('maps every result type to an SPA route; runs without project_id go through the Mission Control resolver', () => {
    const w = `/w/${WS_ID}`;
    expect(searchResultHref(WS_ID, base)).toBe(`${w}/projects/p-1`);
    expect(searchResultHref(WS_ID, { ...base, type: 'run', id: 'r-1', project_id: 'p-1' })).toBe(`${w}/projects/p-1/runs/r-1`);
    expect(searchResultHref(WS_ID, { ...base, type: 'run', id: 'r-1' })).toBe(`${w}/mission-control?run=r-1`);
    expect(searchResultHref(WS_ID, { ...base, type: 'task', id: 't-1', run_id: 'r-1', project_id: 'p-1' })).toBe(`${w}/projects/p-1/runs/r-1/tasks/t-1`);
    expect(searchResultHref(WS_ID, { ...base, type: 'finding', id: 'f-1' })).toBe(`${w}/quality/findings/f-1`);
    expect(searchResultHref(WS_ID, { ...base, type: 'approval', id: 'a-1' })).toBe(`${w}/approvals/a-1`);
    expect(searchResultHref(WS_ID, { ...base, type: 'documentation_source', id: 'd-1' })).toBe(`${w}/knowledge`);
    expect(searchResultHref(WS_ID, { ...base, type: 'chat_message', id: 'c-1', run_id: 'r-1', project_id: 'p-1' })).toBe(`${w}/projects/p-1/runs/r-1/chat`);
  });

  it('groups results by type in contract order, keeping server order inside a group', () => {
    const groups = groupResults([
      { ...base, type: 'task', id: 't-2' },
      { ...base, id: 'p-2' },
      { ...base, type: 'task', id: 't-1' },
    ]);
    expect(groups.map((g) => g.type)).toEqual(['project', 'task']);
    expect(groups[1]?.items.map((i) => i.id)).toEqual(['t-2', 't-1']);
  });
});

function note(n: number, over: Partial<Notification> = {}): Notification {
  return {
    id: `01900000-0000-7000-8000-0000000b000${n}`,
    kind: 'run.completed',
    severity: 'success',
    title: `Уведомление ${n}`,
    body: '',
    project_id: '01900000-0000-7000-9000-000000000001',
    run_id: '01900000-0000-7000-8000-0000000c0001',
    task_id: null,
    resource_type: 'run',
    resource_id: '01900000-0000-7000-8000-0000000c0001',
    data: {},
    created_at: `2026-11-08T09:0${n}:00Z`,
    read_at: null,
    read: false,
    ...over,
  };
}

const page = (items: Notification[], unread = items.filter((n) => !n.read).length): NotificationPage => ({ items, next_cursor: null, unread_count: unread });

describe('server notifications (B8): live ingestion, unread counter, mark read', () => {
  it('notification.created frame → Notification; other frames and broken payloads are ignored', () => {
    const frame: StreamEvent = { event_id: note(1).id, run_id: '', seq: 1762590000001000, type: 'notification.created', timestamp: note(1).created_at, schema_version: '1.0', payload: { ...note(1) } };
    expect(notificationFromEvent(frame)?.id).toBe(note(1).id);
    expect(notificationFromEvent({ ...frame, type: 'run.completed' })).toBeNull();
    expect(notificationFromEvent({ ...frame, payload: { id: 'x' } })).toBeNull();
  });

  it('live notifications are prepended once (dedup by id), newest first', () => {
    const p = page([note(2), note(1, { read: true, read_at: '2026-11-08T09:30:00Z' })]);
    const next = prependNotifications(p, [note(3), note(2)]);
    expect(next?.items.map((n) => n.id)).toEqual([note(3).id, note(2).id, note(1).id]);
    expect(next?.unread_count).toBe(2);
    expect(prependNotifications(undefined, [note(3)])).toBeUndefined();
  });

  it('optimistic unread counter ignores replayed frames (not newer than latest_unread_at)', () => {
    const count = { unread_count: 2, latest_unread_at: note(2).created_at };
    expect(bumpUnread(count, [note(2), note(1)])).toBe(count);
    expect(bumpUnread(count, [note(3), note(4, { read: true })])).toEqual({ unread_count: 3, latest_unread_at: note(3).created_at });
    expect(bumpUnread({ unread_count: 0, latest_unread_at: null }, [note(1)])).toEqual({ unread_count: 1, latest_unread_at: note(1).created_at });
  });

  it('mark read: by ids, up_to (that one and older) and all', () => {
    const p = page([note(3), note(2), note(1)]);
    const read = (q: NotificationPage | undefined) => q?.items.filter((n) => n.read).map((n) => n.title);
    expect(read(applyMarkRead(p, { notification_ids: [note(2).id] }, 2, 'now'))).toEqual(['Уведомление 2']);
    expect(read(applyMarkRead(p, { up_to: note(2).id }, 1, 'now'))).toEqual(['Уведомление 2', 'Уведомление 1']);
    expect(read(applyMarkRead(p, { all: true }, 0, 'now'))).toHaveLength(3);
    expect(applyMarkRead(p, { all: true }, 0, 'now')?.unread_count).toBe(0);
  });

  it('links: approval → approval card, critical finding → run quality report, run → run page', () => {
    expect(notificationHref(WS_ID, { ...note(1), resource_type: 'approval', resource_id: 'a1' })).toBe(`/w/${WS_ID}/approvals/a1`);
    expect(notificationHref(WS_ID, { ...note(1), resource_type: 'gate_execution' })).toBe(`/w/${WS_ID}/quality/runs/${note(1).run_id}`);
    expect(notificationHref(WS_ID, note(1))).toBe(`/w/${WS_ID}/projects/${note(1).project_id}/runs/${note(1).run_id}`);
    expect(notificationHref(WS_ID, { ...note(1), project_id: null, run_id: null })).toBe(`/w/${WS_ID}/dashboard`);
  });
});

describe('layout preferences (this device only)', () => {
  it('clamps the context panel width and resets every saved layout key', () => {
    expect(clampContextWidth(10)).toBe(280);
    expect(clampContextWidth(10_000)).toBe(560);
    window.localStorage.setItem(SIDEBAR_KEY, 'collapsed');
    window.localStorage.setItem(CONTEXT_WIDTH_KEY, '400');
    window.localStorage.setItem('aicd.layout.mission-control.chooser', '{}');
    window.localStorage.setItem('aicd.theme', 'light');
    expect(resetLayout()).toBe(3);
    expect(window.localStorage.getItem('aicd.theme')).toBe('light');
  });
});

describe('app shell', () => {
  it('top bar: CLI chips from listProviders (AUTH_REQUIRED, not detected), approvals badge, collapsible sidebar persisted', async () => {
    const user = userEvent.setup();
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/providers': () => json(200, { items: [provider('codex', [inst({ auth_status: 'AUTH_REQUIRED' })])] }),
      'GET /api/v1/approvals': () =>
        json(200, {
          items: [
            {
              id: '01900000-0000-7000-8000-0000000a0001',
              workspace_id: WS_ID,
              run_id: null,
              action_type: 'DOCS_DOWNLOAD',
              status: 'PENDING',
              risk_level: 'low',
              summary: 'Загрузка документации',
              requested_by: { actor_type: 'agent', user: null, role: 'documentation' },
              can_decide: true,
              created_at: '2026-11-08T08:00:00Z',
              version: 0,
            },
          ],
          next_cursor: null,
        }),
    });
    renderApp(`/w/${WS_ID}/projects`);
    await screen.findByTestId('empty-state');
    const chips = await screen.findByRole('list', { name: 'Состояние CLI-провайдеров' });
    const codex = await within(chips).findByRole('link', { name: /Codex CLI · требуется вход/ });
    expect(codex).toHaveAttribute('href', `/w/${WS_ID}/settings/providers`);
    expect(codex).toHaveAttribute('data-state', 'auth_required');
    expect(within(chips).getByRole('link', { name: /Claude Code CLI · не обнаружен/ })).toHaveAttribute('data-state', 'not_installed');
    expect(chips.textContent).not.toMatch(/квот|лимит подписки|%/i);

    const nav = screen.getByRole('navigation', { name: 'Основная навигация' });
    await waitFor(() => expect(within(nav).getByRole('link', { name: /Одобрения/ })).toHaveTextContent('ожидают вашего решения: 1'));

    const toggle = screen.getByRole('button', { name: 'Свернуть навигацию' });
    await user.click(toggle);
    expect(window.localStorage.getItem(SIDEBAR_KEY)).toBe('collapsed');
    expect(screen.getByRole('button', { name: 'Развернуть навигацию' })).toBeInTheDocument();
    expect(screen.getByTestId('sidebar').closest('[data-sidebar]')).toHaveAttribute('data-sidebar', 'rail');
  });

  it('CLI chips: load error is shown as "status unavailable", not as ready', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/providers': () => problem(500, 'internal_error'),
    });
    renderApp(`/w/${WS_ID}/projects`);
    const chips = await screen.findByRole('list', { name: 'Состояние CLI-провайдеров' }, { timeout: 5000 });
    expect(within(chips).getByRole('link', { name: /Codex CLI · статус недоступен/ })).toHaveAttribute('data-state', 'error');
  });

  it('Ctrl+K opens the command palette (lazy cmdk); navigation command moves to the section; Escape closes', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('owner')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/runs': () => json(200, { items: [], next_cursor: null }),
    });
    renderApp(`/w/${WS_ID}/projects`);
    await screen.findByTestId('empty-state');
    act(() => {
      fireEvent.keyDown(document, { key: 'k', ctrlKey: true });
    });
    const dialog = await screen.findByRole('dialog', { name: 'Командная палитра' });
    const input = within(dialog).getByRole('combobox');
    await waitFor(() => expect(input).toHaveFocus());
    fireEvent.change(input, { target: { value: 'Аналит' } });
    const option = await within(dialog).findByRole('option', { name: /Перейти: Аналитика/ });
    fireEvent.click(option);
    expect(await screen.findByRole('heading', { level: 1, name: 'Аналитика' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: 'Командная палитра' })).toBeNull();
  });

  it('notifications: unread badge and list from the server; mark one / all read with Idempotency-Key; legacy localStorage cursor is ignored', async () => {
    const user = userEvent.setup();
    // Курсор «прочитано» F2 в localStorage не влияет на серверный счётчик.
    window.localStorage.setItem(`aicd.notifications.read.${WS_ID}`, '2030-01-01T00:00:00Z');
    let items = [note(2), note(1)];
    const unreadOf = () => items.filter((n) => !n.read).length;
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/projects': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/notifications/unread-count': () => json(200, { unread_count: unreadOf(), latest_unread_at: items.find((n) => !n.read)?.created_at ?? null }),
      'GET /api/v1/notifications': () => json(200, { items, next_cursor: null, unread_count: unreadOf() }),
      'POST /api/v1/notifications/mark-read': (req) => {
        const body = req.body as { notification_ids?: string[]; up_to?: string; all?: boolean };
        const before = unreadOf();
        items = items.map((n) => (body.all || body.up_to || body.notification_ids?.includes(n.id) ? { ...n, read: true, read_at: '2026-11-08T10:00:00Z' } : n));
        return json(200, { marked: before - unreadOf(), unread_count: unreadOf() });
      },
    });
    renderApp(`/w/${WS_ID}/projects`);
    const bell = await screen.findByRole('button', { name: 'Уведомления: 2 непрочитанных' });
    expect(screen.getByTestId('notifications-count')).toHaveTextContent('2');
    await user.click(bell);
    const panel = await screen.findByRole('dialog', { name: 'Уведомления' });
    const list = await within(panel).findByRole('list', { name: 'Список уведомлений' });
    expect(within(list).getAllByTestId('notification-item')).toHaveLength(2);
    expect(panel).not.toHaveTextContent(/только в этом браузере/);
    expect(within(list).getByRole('link', { name: /Уведомление 2/ })).toHaveAttribute('href', `/w/${WS_ID}/projects/${note(1).project_id}/runs/${note(1).run_id}`);

    await user.click(within(list).getByRole('button', { name: 'Отметить прочитанным: Уведомление 2' }));
    await waitFor(() => expect(bell).toHaveAccessibleName('Уведомления: 1 непрочитанное'));
    const first = calls.find((c) => c.method === 'POST' && c.url.pathname === '/api/v1/notifications/mark-read');
    expect(first?.body).toEqual({ notification_ids: [note(2).id] });
    expect(first?.headers.get('Idempotency-Key')).toMatch(/^[0-9a-f-]{36}$/);
    expect(first?.headers.get('X-Workspace-Id')).toBe(WS_ID);

    await user.click(within(panel).getByRole('button', { name: 'Отметить все прочитанными' }));
    await waitFor(() => expect(bell).toHaveAccessibleName('Уведомления'));
    const posts = calls.filter((c) => c.method === 'POST' && c.url.pathname === '/api/v1/notifications/mark-read');
    expect(posts.at(-1)?.body).toEqual({ up_to: note(2).id });
    expect(posts.at(-1)?.headers.get('Idempotency-Key')).not.toBe(first?.headers.get('Idempotency-Key'));
    expect(screen.queryByTestId('notifications-count')).toBeNull();
    expect(within(list).getAllByTestId('notification-item').map((li) => li.getAttribute('data-read'))).toEqual(['true', 'true']);
  });
});
