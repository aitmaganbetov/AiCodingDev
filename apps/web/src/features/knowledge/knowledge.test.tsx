import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { DocumentationSource } from '../../api/knowledge';
import { json, mockFetch, project, renderApp, session, WS_ID } from '../../test/utils';
import { validateSourceUri } from './RegisterSourceForm';

const P1 = project(1);
function source(n: number, enabled = true): DocumentationSource {
  return {
    id: `01900000-0000-7000-d000-${String(n).padStart(12, '0')}`,
    workspace_id: WS_ID,
    technology_version_id: '01900000-0000-7000-e000-000000000001',
    source_type: 'url',
    uri_or_file_ref: `https://docs.example.com/${n}`,
    trust_level: 'official',
    license_info: null,
    refresh_policy: 'manual',
    enabled,
    latest_snapshot: null,
    created_at: '2026-11-01T00:00:00Z',
  };
}

describe('validateSourceUri', () => {
  it('checks per source type', () => {
    expect(validateSourceUri('url', '')).toBe('required');
    expect(validateSourceUri('url', 'http://x.example')).toBe('https');
    expect(validateSourceUri('url', 'https://x.example/docs')).toBeNull();
    expect(validateSourceUri('git', 'git@github.com:o/r.git')).toBeNull();
    expect(validateSourceUri('git', 'ftp://x')).toBe('git');
    expect(validateSourceUri('file', 'artifact:123')).toBeNull();
  });
});

describe('KnowledgePage', () => {
  it('refresh requires explicit confirmation; disabled sources cannot be refreshed', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('developer')),
      'GET /api/v1/documentation/sources': () => json(200, { items: [source(1), source(2, false)], next_cursor: null }),
      'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
      [`POST /api/v1/documentation/sources/${source(1).id}/refresh`]: () =>
        json(202, { source_id: source(1).id, external_action_id: '01900000-0000-7000-f000-000000000001', status: 'QUEUED', accepted_at: '2026-11-08T09:00:00Z' }),
    });
    renderApp(`/w/${WS_ID}/knowledge`);
    const table = await screen.findByRole('table', { name: 'Источники документации' });
    expect(within(table).getByText('Источник выключен — обновление недоступно')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Зарегистрировать источник' })).toBeNull();
    await user.click(within(table).getByRole('button', { name: 'Обновить кэш источника https://docs.example.com/1' }));
    const dialog = await screen.findByRole('dialog', { name: 'Загрузить документацию?' });
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    await user.click(within(dialog).getByRole('button', { name: 'Подтвердить загрузку' }));
    expect(await screen.findByText(/поставлено в очередь \(действие 01900000-0000-7000-f000-000000000001\)/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === 'POST')?.headers.get('Idempotency-Key')).toBeTruthy();
  });

  it('changes the documentation policy with If-Match', async () => {
    const user = userEvent.setup();
    const { calls } = mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('admin')),
      'GET /api/v1/documentation/sources': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
      [`GET /api/v1/projects/${P1.id}/documentation-policy`]: () => json(200, { project_id: P1.id, documentation_mode: 'SMART', settings_version: 7 }),
      [`PATCH /api/v1/projects/${P1.id}/documentation-policy`]: () => json(200, { project_id: P1.id, documentation_mode: 'STRICT', settings_version: 8 }),
    });
    renderApp(`/w/${WS_ID}/knowledge`);
    expect(await screen.findByText('Источники не зарегистрированы')).toBeInTheDocument();
    await user.selectOptions(await screen.findByLabelText('Проект'), P1.id);
    await user.click(await screen.findByRole('radio', { name: 'Строгий (STRICT)' }));
    await user.click(screen.getByRole('button', { name: 'Сохранить режим' }));
    expect(await screen.findByText('Режим документации изменён: Строгий (STRICT).')).toBeInTheDocument();
    const patch = calls.find((c) => c.method === 'PATCH');
    expect(patch?.headers.get('If-Match')).toBe('"v7"');
    expect(patch?.body).toEqual({ documentation_mode: 'STRICT' });
  });

  it('viewer sees the policy read-only', async () => {
    mockFetch({
      'GET /api/v1/auth/session': () => json(200, session('viewer')),
      'GET /api/v1/documentation/sources': () => json(200, { items: [], next_cursor: null }),
      'GET /api/v1/projects': () => json(200, { items: [P1], next_cursor: null }),
      [`GET /api/v1/projects/${P1.id}/documentation-policy`]: () => json(200, { project_id: P1.id, documentation_mode: 'OFF', settings_version: 1 }),
    });
    const user = userEvent.setup();
    renderApp(`/w/${WS_ID}/knowledge`);
    await user.selectOptions(await screen.findByLabelText('Проект'), P1.id);
    expect(await screen.findByText('Выключен (OFF)')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Сохранить режим' })).toBeNull();
    await waitFor(() => expect(screen.queryByRole('radio')).toBeNull());
  });
});
