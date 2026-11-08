import { describe, expect, it, vi } from 'vitest';
import { problem, type RecordedRequest } from '../test/utils';
import { EventDeduper, parseRunEvent, RunEventStream, SseParser, type RunEvent, type StreamState } from './runEvents';

const RUN_ID = '01900000-0000-7000-8000-00000000a001';
const PATH = `/api/v1/runs/${RUN_ID}/events`;

function envelope(seq: number, type = 'task.state_changed', payload: Record<string, unknown> = {}): RunEvent {
  return {
    event_id: `01900000-0000-7000-8000-${String(seq).padStart(12, '0')}`,
    run_id: RUN_ID,
    seq,
    type,
    timestamp: '2026-11-08T09:00:00Z',
    schema_version: '1.0',
    payload,
  };
}

function frame(seq: number, type = 'task.state_changed'): string {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(envelope(seq, type))}\n\n`;
}

const heartbeat = `event: heartbeat\ndata: ${JSON.stringify({
  event_id: '01900000-0000-7000-8000-0000000000ff',
  run_id: RUN_ID,
  seq: null,
  type: 'heartbeat',
  timestamp: '2026-11-08T09:00:00Z',
  schema_version: '1.0',
  payload: { server_time: '2026-11-08T09:00:00Z', last_seq: 3 },
})}\n\n`;

type Body = { chunks: string[]; end: 'close' | 'error' | 'hang' };

/** Ответ text/event-stream из кусков; end: закрыть, оборвать ошибкой сети или «зависнуть». */
function sse({ chunks, end }: Body): Response {
  const enc = new TextEncoder();
  let i = 0;
  // pull: каждый кусок читается до обрыва (controller.error() сбросил бы ещё не прочитанную очередь).
  const stream = new ReadableStream<Uint8Array>({
    pull(c) {
      const ch = chunks[i++];
      if (ch !== undefined) c.enqueue(enc.encode(ch));
      else if (end === 'close') c.close();
      else if (end === 'error') c.error(new TypeError('network connection lost'));
      else return new Promise<void>(() => undefined);
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

function startStream(extra: Partial<ConstructorParameters<typeof RunEventStream>[0]> = {}) {
  const received: RunEvent[] = [];
  const states: StreamState[] = [];
  const resync = vi.fn();
  const stream = new RunEventStream({
    runId: RUN_ID,
    onEvents: (events) => received.push(...events),
    onState: (s) => states.push(s),
    onResync: resync,
    retryMs: 1,
    ...extra,
  });
  stream.start();
  return { stream, received, states, resync };
}

describe('SseParser', () => {
  it('parses frames split across chunks, CRLF/CR line endings, comments and multi-line data', () => {
    const p = new SseParser();
    expect(p.feed('retry: 3000\n\n: comment\nid: 1\nevent: a\nda')).toEqual([]);
    expect(p.retryMs).toBe(3000);
    expect(p.feed('ta: x\r\ndata:y\r')).toEqual([]);
    expect(p.feed('\n\r\nid: 2\rdata: z\r\r\n')).toEqual([
      { id: '1', event: 'a', data: 'x\ny' },
      { id: '2', event: 'message', data: 'z' },
    ]);
  });

  it('keeps the last id for frames without id (heartbeat does not move Last-Event-ID)', () => {
    const p = new SseParser();
    const frames = p.feed(`${frame(7)}${heartbeat}`);
    expect(frames.map((f) => [f.id, f.event])).toEqual([
      ['7', 'task.state_changed'],
      ['7', 'heartbeat'],
    ]);
    expect(parseRunEvent(frames[1]!)).toBeNull();
    expect(parseRunEvent(frames[0]!)?.seq).toBe(7);
  });

  it('ignores malformed data', () => {
    expect(parseRunEvent({ id: '1', event: 'x', data: '{oops' })).toBeNull();
    expect(parseRunEvent({ id: '1', event: 'x', data: '{"type":"x"}' })).toBeNull();
  });
});

describe('EventDeduper', () => {
  it('drops events with seq <= last seen and repeated event_id', () => {
    const d = new EventDeduper();
    expect(d.accept(envelope(1))).toBe(true);
    expect(d.accept(envelope(2))).toBe(true);
    expect(d.accept(envelope(2))).toBe(false);
    expect(d.accept(envelope(1))).toBe(false);
    expect(d.accept({ ...envelope(5), event_id: envelope(2).event_id })).toBe(false);
    expect(d.accept(envelope(4))).toBe(true); // разрыв seq (фильтры) — не потеря
    expect(d.lastSeq).toBe(4);
    expect(d.duplicates).toBe(3);
    d.skipTo(10);
    expect(d.accept(envelope(9))).toBe(false);
    expect(d.accept(envelope(11))).toBe(true);
  });
});

describe('RunEventStream', () => {
  it('reconnects after a dropped connection with Last-Event-ID and shows every event exactly once', async () => {
    const calls = mockStream([
      () => sse({ chunks: ['retry: 1\n\n', frame(1), frame(2) + frame(3).slice(0, 20)], end: 'error' }),
      // Сервер повторяет seq 2 (на случай гонки) — клиент должен отбросить дубль.
      () => sse({ chunks: [frame(2), heartbeat, frame(3), frame(4)], end: 'close' }),
      () => sse({ chunks: [frame(5), frame(6, 'run.completed')], end: 'close' }),
    ]);
    const { stream, received, states } = startStream();
    await vi.waitFor(() => expect(states.at(-1)).toBe('closed'));

    expect(received.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(calls).toHaveLength(3);
    expect(calls[0]!.headers.get('Last-Event-ID')).toBeNull();
    expect(calls[0]!.headers.get('Accept')).toBe('text/event-stream');
    expect(calls[1]!.headers.get('Last-Event-ID')).toBe('2'); // кадр 3 был оборван посередине
    expect(calls[2]!.headers.get('Last-Event-ID')).toBe('4');
    expect(stream.dedup.duplicates).toBe(1);
    expect(states).toEqual(['connecting', 'open', 'reconnecting', 'open', 'reconnecting', 'open', 'closed']);
  });

  it('does not reconnect after the server closes the stream of a terminal run', async () => {
    const calls = mockStream([() => sse({ chunks: [frame(1)], end: 'close' })]);
    const { states } = startStream({ isTerminal: () => true });
    await vi.waitFor(() => expect(states.at(-1)).toBe('closed'));
    expect(calls).toHaveLength(1);
  });

  it('on 410 replay_window_exceeded resyncs state and continues from last_seq via after_seq', async () => {
    const calls = mockStream([
      () => problem(410, 'replay_window_exceeded', { min_available_seq: 30, last_seq: 42 }),
      () => sse({ chunks: [frame(41), frame(43), frame(44, 'run.completed')], end: 'close' }),
    ]);
    const { received, resync, states } = startStream();
    await vi.waitFor(() => expect(states.at(-1)).toBe('closed'));
    expect(resync).toHaveBeenCalledTimes(1);
    expect(calls[1]!.url.searchParams.get('after_seq')).toBe('42');
    expect(calls[1]!.headers.get('Last-Event-ID')).toBeNull();
    expect(received.map((e) => e.seq)).toEqual([43, 44]);
  });

  it('fails without retry on 404 and after a repeated 410', async () => {
    const calls = mockStream([() => problem(404, 'not_found')]);
    const { states } = startStream();
    await vi.waitFor(() => expect(states.at(-1)).toBe('failed'));
    expect(calls).toHaveLength(1);

    const calls410 = mockStream([() => problem(410, 'replay_window_exceeded', { last_seq: 5 })]);
    const second = startStream();
    await vi.waitFor(() => expect(second.states.at(-1)).toBe('failed'));
    expect(calls410).toHaveLength(2);
  });

  it('retries 503 and network errors', async () => {
    let n = 0;
    const calls = mockStream([
      () => problem(503, 'service_unavailable'),
      () => {
        n += 1;
        throw new TypeError('offline');
      },
      () => sse({ chunks: [frame(1, 'run.completed')], end: 'close' }),
    ]);
    const { states, received } = startStream();
    await vi.waitFor(() => expect(states.at(-1)).toBe('closed'));
    expect(n).toBe(1);
    expect(calls).toHaveLength(3);
    expect(received).toHaveLength(1);
  });

  it('reconnects when the connection goes silent (no heartbeat) and stops cleanly', async () => {
    const calls = mockStream([
      () => sse({ chunks: [frame(1)], end: 'hang' }),
      () => sse({ chunks: [frame(2)], end: 'hang' }),
    ]);
    const { stream, received } = startStream({ idleTimeoutMs: 20 });
    await vi.waitFor(() => expect(calls.length).toBeGreaterThanOrEqual(2));
    expect(calls[1]!.headers.get('Last-Event-ID')).toBe('1');
    await vi.waitFor(() => expect(received.map((e) => e.seq)).toEqual([1, 2]));
    stream.stop();
    const count = calls.length;
    await new Promise((r) => setTimeout(r, 60));
    expect(calls.length).toBe(count);
  });
});
