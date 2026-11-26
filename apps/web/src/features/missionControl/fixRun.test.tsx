import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import type { Run } from '../../api/runs';
import { createQueryClient } from '../../queryClient';
import { json, mockFetch, problem, WS_ID } from '../../test/utils';
import { FixRunAction } from './FixRunAction';

const RUN_ID = '01900000-0000-7000-8000-0000000000c1';
const FIX_ID = '01900000-0000-7000-8000-0000000000c2';

function show(status: Run['status']) {
  const run = { id: RUN_ID, status, version: 5 } as Run;
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <FixRunAction workspaceId={WS_ID} run={run} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('«Передать ИИ на исправление»', () => {
  it('доступно только для run в статусе FAILED', () => {
    show('RUNNING');
    expect(screen.queryByTestId('run-fix')).toBeNull();
  });

  it('создаёт run-исправление с уточнением и даёт ссылку на него', async () => {
    const api = mockFetch({
      [`POST /api/v1/runs/${RUN_ID}/fix`]: () => json(201, { run_id: FIX_ID, status: 'CREATED', event_stream: `/api/v1/runs/${FIX_ID}/events` }),
    });
    show('FAILED');
    await userEvent.click(screen.getByRole('button', { name: 'Передать ИИ на исправление' }));
    await userEvent.type(screen.getByRole('textbox'), ' сначала Dockerfile ');
    await userEvent.click(screen.getByRole('button', { name: 'Создать запуск-исправление' }));
    const link = await screen.findByTestId('run-fix-link');
    expect(link.getAttribute('href')).toBe(`/w/${WS_ID}/mission-control?run=${FIX_ID}`);
    const call = api.calls.find((c) => c.method === 'POST');
    expect(call?.body).toEqual({ note: 'сначала Dockerfile' });
    expect(call?.headers.get('Idempotency-Key')).toBeTruthy();
  });

  it('показывает отказ API, когда исправлять нечего', async () => {
    mockFetch({ [`POST /api/v1/runs/${RUN_ID}/fix`]: () => problem(409, 'invalid_state_transition') });
    show('FAILED');
    await userEvent.click(screen.getByRole('button', { name: 'Передать ИИ на исправление' }));
    await userEvent.click(screen.getByRole('button', { name: 'Создать запуск-исправление' }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.queryByTestId('run-fix-link')).toBeNull();
  });
});
