import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { frame, mockSseFetch, RUN_ID, sseResponse } from '../test/sse';
import { StreamManager } from './streamManager';
import { StreamManagerProvider } from './useEventStream';
import { useRunEvents } from './useRunEvents';

let manager: StreamManager;
afterEach(() => manager.dispose());

function wrapper({ children }: { children: ReactNode }) {
  return <StreamManagerProvider manager={manager}>{children}</StreamManagerProvider>;
}

describe('useRunEvents', () => {
  it('two screens on the same run share one SSE connection; a filtered view opens its own stream', async () => {
    manager = new StreamManager({ lingerMs: 0, environment: null });
    const calls = mockSseFetch((c) => sseResponse([frame(1), frame(2, 'agent.message')], 'hang', c.signal));
    const onEvents = vi.fn();
    const feed = renderHook(() => useRunEvents(RUN_ID, { onEvents }), { wrapper });
    const graph = renderHook(() => useRunEvents(RUN_ID), { wrapper });
    await vi.waitFor(() => expect(feed.result.current.events.map((e) => e.seq)).toEqual([1, 2]));
    expect(graph.result.current.events.map((e) => e.seq)).toEqual([1, 2]);
    expect(feed.result.current.state).toBe('open');
    expect(calls).toHaveLength(1);
    expect(onEvents.mock.calls.flatMap(([events]) => events as unknown[])).toHaveLength(2);

    const chat = renderHook(() => useRunEvents(RUN_ID, { filters: { types: ['agent.message'] } }), { wrapper });
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]!.url.searchParams.getAll('types')).toEqual(['agent.message']);
    chat.unmount();

    feed.unmount();
    expect(calls[0]!.signal.aborted).toBe(false); // graph ещё подписан
    graph.unmount();
    await vi.waitFor(() => expect(calls[0]!.signal.aborted).toBe(true));
  });

  it('reconnect() restarts a failed stream from the beginning and clears the feed', async () => {
    manager = new StreamManager({ lingerMs: 0, environment: null });
    let n = 0;
    mockSseFetch((c) => {
      n += 1;
      return n === 1 ? sseResponse([frame(1)], 'close') : sseResponse([frame(1), frame(2)], 'hang', c.signal);
    });
    const { result } = renderHook(() => useRunEvents(RUN_ID, { isTerminal: () => n === 1 }), { wrapper });
    await vi.waitFor(() => expect(result.current.state).toBe('closed'));
    expect(result.current.events).toHaveLength(1);
    act(() => result.current.reconnect());
    await vi.waitFor(() => expect(result.current.events.map((e) => e.seq)).toEqual([1, 2]));
  });
});
