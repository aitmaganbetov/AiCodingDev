import { describe, expect, it, vi } from 'vitest';
import { problem, type RecordedRequest } from '../test/utils';
import { EventStream, type StreamEvent, type StreamState } from './sse';
import { workspaceEventsSpec } from './workspaceEvents';

const WS = '01900000-0000-7000-8000-000000000001';
const PATH = `/api/v1/workspaces/${WS}/events`;

function frame(cursor: number, n: number, runSuffix: string, seq: number, type = 'approval.requested'): string {
  const env = {
    event_id: `01900000-0000-7000-8000-${String(n).padStart(12, '0')}`,
    run_id: `01900000-0000-7000-8000-00000000${runSuffix}`,
    seq,
    type,
    timestamp: '2026-11-08T09:00:00Z',
    schema_version: '1.0',
    payload: {},
  };
  return `id: ${cursor}\nevent: ${type}\ndata: ${JSON.stringify(env)}\n\n`;
}

function sse(chunks: string[], end: 'close' | 'error'): Response {
  const enc = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(c) {
      const ch = chunks[i++];
      if (ch !== undefined) c.enqueue(enc.encode(ch));
      else if (end === 'close') c.close();
      else c.error(new TypeError('network connection lost'));
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function mockStream(responses: ((req: RecordedRequest) => Response)[]) {
  const calls: RecordedRequest[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost');
      const req: RecordedRequest = { method: init?.method ?? 'GET', url, headers: new Headers(init?.headers), body: undefined };
      calls.push(req);
      const next = responses[Math.min(calls.length - 1, responses.length - 1)];
      if (!next || url.pathname !== PATH) return problem(404, 'not_found');
      return next(req);
    }),
  );
  return calls;
}

function start() {
  const received: StreamEvent[] = [];
  const states: StreamState[] = [];
  const resync = vi.fn();
  const spec = workspaceEventsSpec(WS, ['approval.requested', 'approval.decided']);
  const stream = new EventStream({
    path: spec.path,
    query: spec.query,
    ...(spec.resume ? { resume: spec.resume } : {}),
    onEvents: (events) => received.push(...events),
    onState: (s) => states.push(s),
    onResync: resync,
    retryMs: 1,
  });
  stream.start();
  return { stream, received, states, resync };
}

describe('workspace notification stream (OQ-45 (c))', () => {
  it('resumes with the opaque cursor as Last-Event-ID and dedups by event_id across runs (seq is per run)', async () => {
    const calls = mockStream([
      () => sse([frame(1000, 1, 'a001', 7), frame(1001, 2, 'a002', 1)], 'error'),
      // Перекрытие: сервер повторяет событие 2; событие 3 другого run имеет меньший seq — не дубль.
      () => sse([frame(1001, 2, 'a002', 1), frame(1002, 3, 'a001', 2, 'approval.decided')], 'error'),
      () => sse([], 'close'),
    ]);
    const { stream, received } = start();
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(3));
    stream.stop();
    expect(received.map((e) => e.event_id.slice(-1))).toEqual(['1', '2', '3']);
    expect(calls[0]!.headers.get('Last-Event-ID')).toBeNull();
    expect(calls[0]!.url.searchParams.getAll('types')).toEqual(['approval.requested', 'approval.decided']);
    expect(calls[1]!.headers.get('Last-Event-ID')).toBe('1001');
    expect(calls[2]!.headers.get('Last-Event-ID')).toBe('1002');
    expect(calls.every((c) => !c.url.searchParams.has('after_seq'))).toBe(true);
    expect(stream.dedup.duplicates).toBe(1);
  });

  it('on 410 drops the cursor, asks the screen to resync from REST and reconnects from now', async () => {
    const calls = mockStream([
      () => sse([frame(5, 1, 'a001', 1)], 'error'),
      () => problem(410, 'replay_window_exceeded'),
      () => sse([frame(9, 2, 'a001', 2)], 'error'),
      () => sse([], 'close'),
    ]);
    const { stream, received, resync } = start();
    await vi.waitFor(() => expect(received).toHaveLength(2));
    stream.stop();
    expect(resync).toHaveBeenCalledTimes(1);
    expect(calls[1]!.headers.get('Last-Event-ID')).toBe('5');
    expect(calls[2]!.headers.get('Last-Event-ID')).toBeNull();
  });
});
