import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { Provider, ProviderInstallation } from '../api/providers';
import type { SearchResult } from '../api/search';
import type { StreamEvent } from '../api/sse';
import { CONTEXT_WIDTH_KEY, SIDEBAR_KEY, clampContextWidth, resetLayout } from '../lib/layoutPrefs';
import { json, mockFetch, problem, renderApp, session, WS_ID } from '../test/utils';
import { installationState, providerChipModel } from './providerStatus';
import { groupResults, normalizeQuery, searchResultHref } from './searchModel';
import { countUnread } from './shellData';

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

describe('notifications and layout preferences (this device only)', () => {
  const ev = (ts: string): StreamEvent => ({ event_id: ts, run_id: 'r', seq: 1, type: 'run.completed', timestamp: ts, schema_version: '1.0', payload: {} });

  it('counts events newer than the read cursor (server time)', () => {
    const events = [ev('2026-11-08T09:00:00Z'), ev('2026-11-08T09:00:01Z'), ev('2026-11-08T09:00:02Z')];
    expect(countUnread(events, null)).toBe(3);
    expect(countUnread(events, '2026-11-08T09:00:01Z')).toBe(1);
  });

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
});
