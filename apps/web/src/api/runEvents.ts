/**
 * Клиент SSE-потока run (GET /runs/{run_id}/events; API_DESIGN §13, ADR-0011, EVENT_SCHEMAS §5).
 *
 * Почему fetch + ReadableStream, а не нативный EventSource:
 *  - EventSource не показывает HTTP-статус ошибки, а клиенту нужно различать
 *    410 replay_window_exceeded (перечитать состояние и продолжить с after_seq),
 *    401 (вход), 403/404 (фатально), 429/503 (повтор с Retry-After);
 *  - явный Last-Event-ID при любом переподключении (в т.ч. после watchdog-обрыва)
 *    и управляемая задержка повторов; новых зависимостей не требуется.
 *
 * Гарантии для UI: событие с seq ≤ последнего принятого отбрасывается (EVENT_SCHEMAS §5),
 * повтор event_id тоже; переподключение всегда идёт с Last-Event-ID = последний принятый seq,
 * поэтому сервер досылает пропущенное (durable replay из run_event_log).
 */
import { ApiError, isApiError, openEventStream, type SseEventType } from './client';

export interface SseFrame {
  id: string | undefined;
  event: string;
  data: string;
}

/**
 * Инкрементальный парсер text/event-stream (WHATWG HTML §9.2.6): строки CRLF/LF/CR,
 * комментарии `:`, поля id/event/data/retry; кадр отправляется на пустой строке.
 */
export class SseParser {
  private buffer = '';
  private data: string[] = [];
  private event = '';
  private id: string | undefined;
  private hasData = false;
  /** Последнее значение поля `retry:` (мс), если сервер его прислал. */
  retryMs: number | undefined;

  feed(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (!match) break;
      // Одиночный CR в конце буфера может быть началом CRLF — ждём следующий кусок.
      if (match[0] === '\r' && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const frame = this.line(line);
      if (frame) frames.push(frame);
    }
    return frames;
  }

  private line(line: string): SseFrame | null {
    if (line === '') {
      if (!this.hasData) {
        this.event = '';
        return null;
      }
      const frame: SseFrame = { id: this.id, event: this.event || 'message', data: this.data.join('\n') };
      this.data = [];
      this.hasData = false;
      this.event = '';
      return frame;
    }
    if (line.startsWith(':')) return null;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    switch (field) {
      case 'data':
        this.data.push(value);
        this.hasData = true;
        break;
      case 'event':
        this.event = value;
        break;
      case 'id':
        if (!value.includes('\0')) this.id = value;
        break;
      case 'retry':
        if (/^\d+$/.test(value)) this.retryMs = Number(value);
        break;
      default:
        break;
    }
    return null;
  }
}

/** Конверт SseEnvelope (packages/contracts/schemas/events/1.0/sse-event.schema.json). */
export interface RunEvent {
  event_id: string;
  run_id: string;
  seq: number;
  type: Exclude<SseEventType, 'heartbeat'> | (string & {});
  timestamp: string;
  schema_version: string;
  payload: Record<string, unknown>;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/** Разбор кадра в событие; heartbeat и битые кадры → null (heartbeat обрабатывается отдельно). */
export function parseRunEvent(frame: SseFrame): RunEvent | null {
  if (frame.event === 'heartbeat') return null;
  let body: unknown;
  try {
    body = JSON.parse(frame.data);
  } catch {
    return null;
  }
  if (!isRecord(body) || body.type === 'heartbeat') return null;
  const seq = typeof body.seq === 'number' ? body.seq : frame.id !== undefined ? Number(frame.id) : NaN;
  if (!Number.isSafeInteger(seq) || seq < 1 || typeof body.event_id !== 'string' || typeof body.type !== 'string') {
    return null;
  }
  return {
    event_id: body.event_id,
    run_id: typeof body.run_id === 'string' ? body.run_id : '',
    seq,
    type: body.type,
    timestamp: typeof body.timestamp === 'string' ? body.timestamp : '',
    schema_version: typeof body.schema_version === 'string' ? body.schema_version : '',
    payload: isRecord(body.payload) ? body.payload : {},
  };
}

/**
 * Дедупликация по seq (монотонен в пределах run) и event_id.
 * Фильтры types/task_id дают разрывы seq — это не потеря (API_DESIGN §13 п.5), поэтому
 * «дыры» не детектируются; полноту обеспечивает Last-Event-ID при переподключении.
 */
export class EventDeduper {
  lastSeq: number | null = null;
  private readonly seen = new Set<string>();
  private readonly order: string[] = [];
  duplicates = 0;

  constructor(private readonly maxIds = 2000) {}

  accept(event: RunEvent): boolean {
    if ((this.lastSeq !== null && event.seq <= this.lastSeq) || this.seen.has(event.event_id)) {
      this.duplicates += 1;
      return false;
    }
    this.lastSeq = event.seq;
    this.seen.add(event.event_id);
    this.order.push(event.event_id);
    if (this.order.length > this.maxIds) this.seen.delete(this.order.shift() as string);
    return true;
  }

  /** После 410: продолжить «с текущего максимума» — всё, что ≤ seq, считается полученным. */
  skipTo(seq: number): void {
    if (this.lastSeq === null || seq > this.lastSeq) this.lastSeq = seq;
  }
}

export type StreamState = 'connecting' | 'open' | 'reconnecting' | 'closed' | 'failed';

export interface RunEventStreamOptions {
  runId: string;
  /** Новые (уже дедуплицированные) события; вызывается пачкой на каждый прочитанный кусок потока. */
  onEvents: (events: RunEvent[]) => void;
  onState?: (state: StreamState, error?: ApiError) => void;
  /** Состояние могло разойтись с лентой (410): перечитать run/tasks через REST. */
  onResync?: () => void;
  /** Run в терминальном статусе: при закрытии потока сервером не переподключаться. */
  isTerminal?: () => boolean;
  /** Пауза перед переподключением, если сервер не прислал `retry:` (мс). */
  retryMs?: number;
  maxRetryMs?: number;
  /** Нет ни байта (включая heartbeat каждые 15 с) дольше этого — соединение считается мёртвым. */
  idleTimeoutMs?: number;
}

const TERMINAL_TYPE = 'run.completed';

/** Один логический поток событий run с автоматическим возобновлением. */
export class RunEventStream {
  readonly dedup = new EventDeduper();
  /** Счётчик HTTP-подключений (для диагностики и тестов). */
  connections = 0;
  private controller: AbortController | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private idleTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private terminal = false;
  private failures = 0;
  private serverRetryMs: number | undefined;
  /** after_seq для следующего подключения после 410 (контракт: «после_seq = текущий max»). */
  private afterSeq: number | null = null;

  constructor(private readonly opts: RunEventStreamOptions) {}

  start(): void {
    this.stopped = false;
    this.opts.onState?.('connecting');
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.controller?.abort();
    this.controller = null;
  }

  private clearTimers(): void {
    if (this.timer) clearTimeout(this.timer);
    if (this.idleTimer) clearTimeout(this.idleTimer);
    this.timer = null;
    this.idleTimer = null;
  }

  private armIdle(controller: AbortController): void {
    if (this.idleTimer) clearTimeout(this.idleTimer);
    const ms = this.opts.idleTimeoutMs ?? 45_000;
    this.idleTimer = setTimeout(() => controller.abort(new DOMException('idle', 'TimeoutError')), ms);
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    const controller = new AbortController();
    this.controller = controller;
    this.connections += 1;
    const last = this.dedup.lastSeq;
    const useAfterSeq = this.afterSeq !== null;
    try {
      const res = await openEventStream({
        path: `/runs/${encodeURIComponent(this.opts.runId)}/events`,
        query: useAfterSeq ? { after_seq: this.afterSeq } : undefined,
        lastEventId: !useAfterSeq && last !== null ? String(last) : undefined,
        signal: controller.signal,
      });
      this.afterSeq = null;
      if (!res.body) throw new ApiError({ status: 0, code: 'network_error' });
      // Успешное подключение: следующий штатный реконнект — через `retry:` сервера, без backoff.
      this.failures = 0;
      this.opts.onState?.('open');
      await this.read(res.body, controller);
      // Сервер закрыл поток: run завершён, истёк max lifetime (30 мин) или отозван доступ.
      if (this.stopped) return;
      if (this.terminal || this.opts.isTerminal?.()) {
        this.finish('closed');
        return;
      }
      this.scheduleReconnect();
    } catch (error) {
      if (this.stopped) return;
      this.handleError(error);
    } finally {
      if (this.idleTimer) clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  private async read(body: ReadableStream<Uint8Array>, controller: AbortController): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    const onAbort = () => void reader.cancel().catch(() => undefined);
    controller.signal.addEventListener('abort', onAbort);
    this.armIdle(controller);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done || this.stopped) break;
        this.armIdle(controller);
        const frames = parser.feed(decoder.decode(value, { stream: true }));
        if (parser.retryMs !== undefined) this.serverRetryMs = parser.retryMs;
        const fresh: RunEvent[] = [];
        for (const frame of frames) {
          const event = parseRunEvent(frame);
          if (event && this.dedup.accept(event)) {
            fresh.push(event);
            if (event.type === TERMINAL_TYPE) this.terminal = true;
          }
        }
        if (fresh.length > 0) this.opts.onEvents(fresh);
      }
    } finally {
      controller.signal.removeEventListener('abort', onAbort);
      reader.releaseLock();
    }
  }

  private handleError(error: unknown): void {
    if (!isApiError(error)) {
      // Обрыв чтения (сеть, watchdog) — переподключение с Last-Event-ID.
      this.scheduleReconnect();
      return;
    }
    if (error.status === 410 || error.code === 'replay_window_exceeded') {
      const lastSeq = error.problem ? (error.problem as Record<string, unknown>).last_seq : undefined;
      if (typeof lastSeq === 'number' && Number.isSafeInteger(lastSeq) && lastSeq >= 0 && this.afterSeq === null) {
        // История вне окна хранения: состояние берём из REST, ленту продолжаем «с текущего max».
        this.dedup.skipTo(lastSeq);
        this.afterSeq = lastSeq;
        this.opts.onResync?.();
        this.scheduleReconnect(0);
        return;
      }
      this.finish('failed', error);
      return;
    }
    const retryable = error.status === 0 || error.status === 429 || error.status >= 500;
    if (!retryable) {
      this.finish('failed', error); // 401 (обработан клиентом), 403, 404, 422
      return;
    }
    const after = error.retryAfterSeconds !== undefined ? error.retryAfterSeconds * 1000 : undefined;
    this.scheduleReconnect(after);
  }

  private scheduleReconnect(delayMs?: number): void {
    if (this.stopped) return;
    this.controller = null;
    this.failures += 1;
    const base = this.serverRetryMs ?? this.opts.retryMs ?? 3000;
    const max = this.opts.maxRetryMs ?? 30_000;
    const delay = delayMs ?? Math.min(base * 2 ** Math.max(this.failures - 1, 0), max);
    this.opts.onState?.('reconnecting');
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.connect();
    }, delay);
  }

  private finish(state: 'closed' | 'failed', error?: ApiError): void {
    this.stopped = true;
    this.clearTimers();
    this.controller = null;
    this.opts.onState?.(state, error);
  }
}
