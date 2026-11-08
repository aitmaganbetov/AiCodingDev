import { vi } from 'vitest';
import type { StreamEvent } from '../api/sse';

/** Тестовые помощники SSE: конверты, кадры, ответы text/event-stream и мок fetch. */
export const RUN_ID = '01900000-0000-7000-8000-00000000a001';

export function envelope(seq: number, type = 'task.state_changed', runId = RUN_ID): StreamEvent {
  return {
    event_id: `01900000-0000-7000-8000-${String(seq).padStart(12, '0')}`,
    run_id: runId,
    seq,
    type,
    timestamp: '2026-11-08T09:00:00Z',
    schema_version: '1.0',
    payload: {},
  };
}

export function frame(seq: number, type = 'task.state_changed', runId = RUN_ID): string {
  return `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify(envelope(seq, type, runId))}\n\n`;
}

/** Ответ text/event-stream из кусков; end: закрыть, оборвать ошибкой сети или «зависнуть» (до abort). */
export function sseResponse(chunks: string[], end: 'close' | 'error' | 'hang' = 'hang', signal?: AbortSignal): Response {
  const enc = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(c) {
      const ch = chunks[i++];
      if (ch !== undefined) c.enqueue(enc.encode(ch));
      else if (end === 'close') c.close();
      else if (end === 'error') c.error(new TypeError('network connection lost'));
      else
        return new Promise<void>((resolve) => {
          signal?.addEventListener('abort', () => {
            try {
              c.error(new DOMException('aborted', 'AbortError'));
            } catch {
              /* already closed */
            }
            resolve();
          });
        });
    },
  });
  return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

export interface SseCall {
  url: URL;
  lastEventId: string | null;
  signal: AbortSignal;
}

/** Подменяет fetch: `respond(call, index)` формирует ответ на каждое подключение. */
export function mockSseFetch(respond: (call: SseCall, index: number) => Response | Promise<Response>) {
  const calls: SseCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const call: SseCall = {
        url: new URL(String(input), 'http://localhost'),
        lastEventId: new Headers(init?.headers).get('Last-Event-ID'),
        signal: init?.signal ?? new AbortController().signal,
      };
      calls.push(call);
      if (call.signal.aborted) throw call.signal.reason;
      return respond(call, calls.length - 1);
    }),
  );
  return calls;
}
