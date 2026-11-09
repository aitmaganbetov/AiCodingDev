import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { json, mockFetch, renderApp, session, WS_ID } from '../../test/utils';
import { filtersFromDraft, filtersFromSearch, isoToLocalInput, localInputToIso, searchFromFilters } from './filters';

const EVENT = {
  id: '01900000-0000-7000-c000-000000000001',
  workspace_id: WS_ID,
  actor_type: 'user',
  actor_id: '01900000-0000-7000-8000-0000000000aa',
  action: 'project.create',
  resource_type: 'project',
  resource_id: '01900000-0000-7000-9000-000000000001',
  metadata: { outcome: 'success', request_id: 'req-1' },
  created_at: '2026-11-08T08:00:00Z',
};

describe('audit filters', () => {
  it('round-trips through the query string and validates the draft', () => {
    const params = searchFromFilters({ action: 'project.create', from: '2026-11-01T00:00:00.000Z' });
    expect(params.toString()).toBe('action=project.create&from=2026-11-01T00%3A00%3A00.000Z');
    expect(filtersFromSearch(params)).toEqual({ action: 'project.create', from: '2026-11-01T00:00:00.000Z' });
    const local = isoToLocalInput('2026-11-01T00:00:00.000Z');
    expect(localInputToIso(local)).toBe('2026-11-01T00:00:00.000Z');
    expect(
      filtersFromDraft({ action: ' run.create ', resource_type: '', resource_id: '', actor_id: 'nope', from: '2026-11-02T00:00', to: '2026-11-01T00:00' })
        .errors,
    ).toEqual({ actor_id: 'uuid', to: 'range' });
    expect(filtersFromDraft({ action: ' run.create ', resource_type: '', resource_id: '', actor_id: '', from: '', to: '' }).filters).toEqual({
      action: 'run.create',
    });
  });
});

describe('AuditPage', () => {
  it('lists events and sends filters as query parameters', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/audit-events': () => json(200, { items: [EVENT], next_cursor: null }),
    });
    renderApp(`/w/${WS_ID}/audit`);
    expect(await screen.findByText('project.create', { selector: 'th span' })).toBeInTheDocument();
    expect(screen.getByText('Успешно')).toBeInTheDocument();
    await user.type(screen.getByLabelText('Действие'), 'member.add');
    await user.type(screen.getByLabelText('Идентификатор пользователя'), 'not-a-uuid');
    await user.click(screen.getByRole('button', { name: 'Применить' }));
    expect(screen.getByLabelText('Идентификатор пользователя')).toHaveAttribute('aria-invalid', 'true');
    await user.clear(screen.getByLabelText('Идентификатор пользователя'));
    await user.click(screen.getByRole('button', { name: 'Применить' }));
    await waitFor(() => expect(calls.filter((c) => c.url.pathname === '/api/v1/audit-events')).toHaveLength(2));
    const last = calls.filter((c) => c.url.pathname === '/api/v1/audit-events').at(-1);
    expect(last?.url.searchParams.get('action')).toBe('member.add');
    expect(last?.headers.get('X-Workspace-Id')).toBe(WS_ID);
  });

  it('is forbidden for developers', async () => {
    mockFetch({ 'GET /api/v1/auth/session': () => json(200, session('developer')) });
    renderApp(`/w/${WS_ID}/audit`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Недостаточно прав' })).toBeInTheDocument();
  });
});
