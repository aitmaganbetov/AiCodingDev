import { afterEach, describe, expect, it, vi } from 'vitest';
import { problem } from '../test/utils';
import { frame, mockSseFetch, RUN_ID, sseResponse } from '../test/sse';
import { runEventsSpec } from './runEvents';
import type { StreamEvent, StreamState } from './sse';
import { StreamManager, streamKey, type StreamManagerOptions } from './streamManager';

const managers: StreamManager[] = [];
afterEach(() => {
  for (const m of managers.splice(0)) m.dispose();
});

function manager(opts: StreamManagerOptions = {}) {
  const m = new StreamManager({ lingerMs: 10, environment: null, streamOptions: { retryMs: 1 }, ...opts });
  managers.push(m);
  return m;
}

function recorder() {
  const events: StreamEvent[] = [];
  const states: StreamState[] = [];
  const resync = vi.fn();
  const reset = vi.fn();
  return {
    events,
    states,
    resync,
    reset,
    sub: {
      onEvents: (e: StreamEvent[]) => events.push(...e),
      onState: (s: StreamState) => states.push(s),
      onResync: resync,
      onReset: reset,
    },
  };
}

/** Окружение для online/offline/visibilitychange. */
function fakeEnvironment() {
  const win = new EventTarget() as EventTarget & { navigator: { onLine: boolean } };
  win.navigator = { onLine: true };
  const doc = new EventTarget() as EventTarget & { visibilityState: DocumentVisibilityState };
  doc.visibilityState = 'visible';
  return { window: win as unknown as Window, document: doc as unknown as Document, win, doc };
}

const spec = runEventsSpec(RUN_ID);
const seqs = (events: StreamEvent[]) => events.map((e) => e.seq);

describe('streamKey', () => {
  it('identifies a stream by path and sorted filters', () => {
    expect(streamKey(runEventsSpec('r1'))).toBe('/runs/r1/events');
    expect(streamKey(runEventsSpec('r1', { types: ['b', 'a'], task_id: 't' }))).toBe('/runs/r1/events?task_id=t&types=a,b');
    expect(streamKey(runEventsSpec('r1', { types: ['a', 'b'] }))).toBe(streamKey(runEventsSpec('r1', { types: ['b', 'a'] })));
  });
});

describe('StreamManager', () => {
  it('shares one connection between subscribers; a late subscriber gets the buffered events', async () => {
    const calls = mockSseFetch((c) => sseResponse([frame(1), frame(2)], 'hang', c.signal));
    const m = manager();
    const a = recorder();
    m.subscribe(spec, a.sub);
    await vi.waitFor(() => expect(seqs(a.events)).toEqual([1, 2]));

    const b = recorder();
    m.subscribe(spec, b.sub);
    expect(seqs(b.events)).toEqual([1, 2]);
    expect(b.states).toEqual(['open']);
    expect(calls).toHaveLength(1);
    expect(m.activeCount).toBe(1);
  });

  it('closes the connection after the last subscriber leaves (with linger) and reuses it within linger', async () => {
    const calls = mockSseFetch((c) => sseResponse([frame(1)], 'hang', c.signal));
    const m = manager({ lingerMs: 30 });
    const a = recorder();
    const unsubA = m.subscribe(spec, a.sub);
    await vi.waitFor(() => expect(a.events).toHaveLength(1));
    unsubA();
    const b = recorder();
    const unsubB = m.subscribe(spec, b.sub); // в пределах linger — то же соединение
    expect(calls).toHaveLength(1);
    expect(seqs(b.events)).toEqual([1]);
    unsubB();
    await vi.waitFor(() => expect(calls[0]!.signal.aborted).toBe(true));
    expect(m.activeCount).toBe(0);
    expect(m.stateOf(spec)).toBeUndefined();
  });

  it('respects the per-user stream limit: extra streams wait in the queue until a slot frees up', async () => {
    const calls = mockSseFetch((c) => sseResponse([frame(1, 'task.state_changed', c.url.pathname.split('/')[3])], 'hang', c.signal));
    const m = manager({ maxStreams: 2, lingerMs: 0 });
    const specs = ['r1', 'r2', 'r3'].map((id) => runEventsSpec(id));
    const recs = specs.map(() => recorder());
    const unsubs = specs.map((s, i) => m.subscribe(s, recs[i]!.sub));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(recs[2]!.states).toEqual(['queued']);
    expect(m.activeCount).toBe(2);

    unsubs[0]!();
    await vi.waitFor(() => expect(calls).toHaveLength(3));
    expect(calls[2]!.url.pathname).toBe('/api/v1/runs/r3/events');
    await vi.waitFor(() => expect(recs[2]!.states.at(-1)).toBe('open'));
    expect(m.activeCount).toBe(2);
  });

  it('default limit is 5 concurrent streams (API_DESIGN §13)', async () => {
    const calls = mockSseFetch((c) => sseResponse([], 'hang', c.signal));
    const m = manager();
    for (let i = 1; i <= 6; i++) m.subscribe(runEventsSpec(`r${i}`), recorder().sub);
    await vi.waitFor(() => expect(calls).toHaveLength(5));
    expect(m.stateOf(runEventsSpec('r6'))).toBe('queued');
  });

  it('a finished stream (run.completed) frees its slot', async () => {
    const calls = mockSseFetch((c, i) =>
      i === 0 ? sseResponse([frame(1, 'run.completed', 'r1')], 'close') : sseResponse([], 'hang', c.signal),
    );
    const m = manager({ maxStreams: 1 });
    const r1 = recorder();
    const r2 = recorder();
    m.subscribe(runEventsSpec('r1'), r1.sub);
    m.subscribe(runEventsSpec('r2'), r2.sub);
    await vi.waitFor(() => expect(r1.states.at(-1)).toBe('closed'));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(r2.states).toEqual(['queued', 'connecting', 'open']);
  });

  it('offline → online: drops the connection, then resumes immediately with Last-Event-ID; no duplicates, no gaps', async () => {
    const env = fakeEnvironment();
    const calls = mockSseFetch((c, i) =>
      i === 0
        ? sseResponse([frame(1), frame(2)], 'hang', c.signal)
        : // сервер повторяет 2 (at-least-once) и досылает пропущенное
          sseResponse([frame(2), frame(3), frame(4)], 'hang', c.signal),
    );
    const m = manager({ environment: { window: env.window, document: env.document }, streamOptions: { retryMs: 60_000 } });
    const r = recorder();
    m.subscribe(spec, r.sub);
    await vi.waitFor(() => expect(seqs(r.events)).toEqual([1, 2]));

    env.win.dispatchEvent(new Event('offline'));
    expect(r.states.at(-1)).toBe('offline');
    expect(calls[0]!.signal.aborted).toBe(true);

    env.win.dispatchEvent(new Event('online'));
    // retryMs 60 с: переподключение не ждёт backoff.
    await vi.waitFor(() => expect(seqs(r.events)).toEqual([1, 2, 3, 4]));
    expect(calls).toHaveLength(2);
    expect(calls[1]!.lastEventId).toBe('2');
    expect(m.streamOf(spec)?.dedup.duplicates).toBe(1);
    expect(r.states.slice(-3)).toEqual(['offline', 'reconnecting', 'open']);
  });

  it('online event short-circuits a pending backoff after a network error', async () => {
    const env = fakeEnvironment();
    const calls = mockSseFetch((c, i) => {
      if (i === 0) return sseResponse([frame(1)], 'error');
      return sseResponse([frame(2)], 'hang', c.signal);
    });
    const m = manager({ environment: { window: env.window, document: env.document }, streamOptions: { retryMs: 60_000 } });
    const r = recorder();
    m.subscribe(spec, r.sub);
    await vi.waitFor(() => expect(r.states.at(-1)).toBe('reconnecting'));
    env.win.dispatchEvent(new Event('online'));
    await vi.waitFor(() => expect(seqs(r.events)).toEqual([1, 2]));
    expect(calls[1]!.lastEventId).toBe('1');
  });

  it('pauses streams of a hidden tab after hiddenPauseMs and resumes with Last-Event-ID when visible', async () => {
    const env = fakeEnvironment();
    const calls = mockSseFetch((c, i) => sseResponse(i === 0 ? [frame(1)] : [frame(2)], 'hang', c.signal));
    const m = manager({ environment: { window: env.window, document: env.document }, hiddenPauseMs: 10 });
    const r = recorder();
    m.subscribe(spec, r.sub);
    await vi.waitFor(() => expect(r.events).toHaveLength(1));

    env.doc.visibilityState = 'hidden';
    env.doc.dispatchEvent(new Event('visibilitychange'));
    await vi.waitFor(() => expect(r.states.at(-1)).toBe('paused'));
    expect(calls[0]!.signal.aborted).toBe(true);

    env.doc.visibilityState = 'visible';
    env.doc.dispatchEvent(new Event('visibilitychange'));
    await vi.waitFor(() => expect(seqs(r.events)).toEqual([1, 2]));
    expect(calls[1]!.lastEventId).toBe('1');
  });

  it('a short hide does not pause the stream', async () => {
    const env = fakeEnvironment();
    const calls = mockSseFetch((c) => sseResponse([frame(1)], 'hang', c.signal));
    const m = manager({ environment: { window: env.window, document: env.document }, hiddenPauseMs: 50 });
    const r = recorder();
    m.subscribe(spec, r.sub);
    await vi.waitFor(() => expect(r.events).toHaveLength(1));
    env.doc.visibilityState = 'hidden';
    env.doc.dispatchEvent(new Event('visibilitychange'));
    env.doc.visibilityState = 'visible';
    env.doc.dispatchEvent(new Event('visibilitychange'));
    await new Promise((res) => setTimeout(res, 80));
    expect(r.states).not.toContain('paused');
    expect(calls).toHaveLength(1);
  });

  it('410 resync is delivered to every subscriber; restart after a fatal error replays from the beginning', async () => {
    const calls = mockSseFetch((c, i) => {
      if (i === 0) return problem(410, 'replay_window_exceeded', { last_seq: 7 });
      if (i === 1) return problem(403, 'forbidden');
      return sseResponse([frame(1), frame(2)], 'hang', c.signal);
    });
    const m = manager();
    const a = recorder();
    const b = recorder();
    m.subscribe(spec, a.sub);
    m.subscribe(spec, b.sub);
    await vi.waitFor(() => expect(a.states.at(-1)).toBe('failed'));
    expect(a.resync).toHaveBeenCalledTimes(1);
    expect(b.resync).toHaveBeenCalledTimes(1);
    expect(calls[1]!.url.searchParams.get('after_seq')).toBe('7');
    expect(m.activeCount).toBe(0);

    m.restart(spec);
    expect(a.reset).toHaveBeenCalledTimes(1);
    await vi.waitFor(() => expect(seqs(b.events)).toEqual([1, 2]));
    expect(calls[2]!.lastEventId).toBeNull();
    expect(calls[2]!.url.searchParams.get('after_seq')).toBeNull();
  });

  it('does not deliver events after unsubscribe', async () => {
    let push: ((chunk: string) => void) | undefined;
    mockSseFetch(() => {
      const enc = new TextEncoder();
      const stream = new ReadableStream<Uint8Array>({
        start(c) {
          push = (chunk) => c.enqueue(enc.encode(chunk));
        },
      });
      return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    });
    const m = manager({ lingerMs: 1000 });
    const a = recorder();
    const b = recorder();
    const unsubA = m.subscribe(spec, a.sub);
    m.subscribe(spec, b.sub);
    await vi.waitFor(() => expect(push).toBeDefined());
    push!(frame(1));
    await vi.waitFor(() => expect(b.events).toHaveLength(1));
    unsubA();
    push!(frame(2));
    await vi.waitFor(() => expect(b.events).toHaveLength(2));
    expect(seqs(a.events)).toEqual([1]);
  });
});
