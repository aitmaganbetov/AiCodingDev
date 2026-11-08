/**
 * Клиент SSE (API_DESIGN §13, ADR-0011, EVENT_SCHEMAS §5): разбор text/event-stream,
 * конверт SseEnvelope, дедупликация и одно логическое соединение с возобновлением.
 * Разделение потоков между экранами, лимит одновременных потоков и реакция на
 * видимость/сеть — в src/api/streamManager.ts.
 *
 * Почему fetch + ReadableStream, а не нативный EventSource:
 *  - EventSource не показывает HTTP-статус ошибки, а клиенту нужно различать
 *    410 replay_window_exceeded (перечитать состояние и продолжить с after_seq),
 *    401 (вход), 403/404 (фатально), 429/503 (повтор с Retry-After);
 *  - явный Last-Event-ID при любом переподключении (в т.ч. после watchdog-обрыва)
 *    и управляемая задержка повторов; новых зависимостей не требуется.
 *
 * Гарантии для UI: событие с seq ≤ последнего принятого отбрасывается, повтор event_id тоже;
 * переподключение всегда идёт с Last-Event-ID = последний принятый seq, поэтому сервер
 * досылает пропущенное (durable replay) — ни дублей, ни пропусков.
 */
import { ApiError, isApiError, openEventStream, type RequestOptions, type Schemas } from './client';

export type SseEventType = Schemas['SseEventType'];

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
export interface StreamEvent {
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

/** Разбор кадра в событие; heartbeat и битые кадры → null (heartbeat только продлевает watchdog). */
export function parseStreamEvent(frame: SseFrame): StreamEvent | null {
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
 * Дедупликация по seq (монотонен в пределах потока) и event_id.
 * Фильтры types/task_id дают разрывы seq — это не потеря (API_DESIGN §13 п.5), поэтому
 * «дыры» не детектируются; полноту обеспечивает Last-Event-ID при переподключении.
 */
export class EventDeduper {
  lastSeq: number | null = null;
  private readonly seen = new Set<string>();
  private readonly order: string[] = [];
  duplicates = 0;

  constructor(private readonly maxIds = 2000) {}

  /**
   * `bySeq = false` — поток с курсором (`id:` не равен seq, например поток workspace: события разных run):
   * дубли отсекаются только по event_id (сервер повторяет окно перекрытия при возобновлении).
   */
  accept(event: StreamEvent, bySeq = true): boolean {
    if ((bySeq && this.lastSeq !== null && event.seq <= this.lastSeq) || this.seen.has(event.event_id)) {
      this.duplicates += 1;
      return false;
    }
    if (bySeq) this.lastSeq = event.seq;
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

/**
 * Состояние логического потока:
 *  - connecting / open / reconnecting — штатная работа;
 *  - queued — ждёт свободного слота (лимит одновременных потоков, StreamManager);
 *  - paused — приостановлен (вкладка долго скрыта), offline — нет сети; возобновляется сам;
 *  - closed — сервер закрыл поток терминального объекта; failed — фатальная ошибка.
 */
export type StreamState = 'connecting' | 'open' | 'reconnecting' | 'queued' | 'paused' | 'offline' | 'closed' | 'failed';

export type StreamQuery = RequestOptions['query'];

export interface EventStreamOptions {
  /** Путь относительно /api/v1, например `/runs/{id}/events`. */
  path: string;
  /** Фильтры потока (types, task_id …) — часть идентичности потока. */
  query?: StreamQuery;
  /** Новые (уже дедуплицированные) события; вызывается пачкой на каждый прочитанный кусок потока. */
  onEvents: (events: StreamEvent[]) => void;
  onState?: (state: StreamState, error?: ApiError) => void;
  /** Состояние могло разойтись с лентой (410): перечитать данные через REST. */
  onResync?: () => void;
  /** Объект потока в терминальном статусе: при закрытии потока сервером не переподключаться. */
  isTerminal?: () => boolean;
  /** Типы событий, после которых сервер закрывает поток навсегда. */
  terminalEventTypes?: readonly string[];
  /** Пауза перед переподключением, если сервер не прислал `retry:` (мс). */
  retryMs?: number;
  maxRetryMs?: number;
  /** Нет ни байта (включая heartbeat каждые 15 с) дольше этого — соединение считается мёртвым. */
  idleTimeoutMs?: number;
  /**
   * Возобновление: 'seq' (по умолчанию) — Last-Event-ID = последний seq потока run, after_seq после 410;
   * 'cursor' — Last-Event-ID = `id:` последнего кадра как есть (поток workspace), после 410 — без курсора.
   */
  resume?: 'seq' | 'cursor';
}

export const DEFAULT_TERMINAL_EVENTS: readonly string[] = ['run.completed'];

/** Один логический поток событий с автоматическим возобновлением (Last-Event-ID). */
export class EventStream {
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
  /** after_seq для следующего подключения после 410 (контракт: «after_seq = текущий max»). */
  private afterSeq: number | null = null;
  /** Курсор потока (`id:` последнего кадра) в режиме resume = 'cursor'. */
  private lastCursor: string | undefined;
  private suspended: 'paused' | 'offline' | null = null;
  /** Поколение подключения: результаты «старых» подключений после pause/stop игнорируются. */
  private generation = 0;

  constructor(private readonly opts: EventStreamOptions) {}

  get finished(): boolean {
    return this.stopped;
  }

  start(): void {
    this.stopped = false;
    this.opts.onState?.('connecting');
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.generation += 1;
    this.clearTimers();
    this.controller?.abort();
    this.controller = null;
  }

  /** Разорвать соединение без потери позиции (вкладка скрыта, нет сети). */
  suspend(reason: 'paused' | 'offline'): void {
    if (this.stopped || this.suspended === reason) return;
    this.suspended = reason;
    this.generation += 1;
    this.clearTimers();
    this.controller?.abort();
    this.controller = null;
    this.opts.onState?.(reason);
  }

  /** Возобновить после suspend — сразу, с Last-Event-ID и без накопленного backoff. */
  resume(): void {
    if (this.stopped || this.suspended === null) return;
    this.suspended = null;
    this.failures = 0;
    this.opts.onState?.('reconnecting');
    void this.connect();
  }

  /** Сеть вернулась: не ждать окончания backoff-паузы. */
  retryNow(): void {
    if (this.stopped || this.suspended !== null || this.timer === null) return;
    clearTimeout(this.timer);
    this.timer = null;
    this.failures = 0;
    void this.connect();
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
    if (this.stopped || this.suspended) return;
    const gen = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    this.connections += 1;
    const last = this.dedup.lastSeq;
    const cursorMode = this.opts.resume === 'cursor';
    const useAfterSeq = !cursorMode && this.afterSeq !== null;
    try {
      const res = await openEventStream({
        path: this.opts.path,
        query: useAfterSeq ? { ...this.opts.query, after_seq: this.afterSeq } : this.opts.query,
        lastEventId: cursorMode ? this.lastCursor : !useAfterSeq && last !== null ? String(last) : undefined,
        signal: controller.signal,
      });
      if (gen !== this.generation) return;
      this.afterSeq = null;
      if (!res.body) throw new ApiError({ status: 0, code: 'network_error' });
      // Успешное подключение: следующий штатный реконнект — через `retry:` сервера, без backoff.
      this.failures = 0;
      this.opts.onState?.('open');
      await this.read(res.body, controller, gen);
      if (gen !== this.generation) return;
      // Сервер закрыл поток: объект завершён, истёк max lifetime (30 мин) или отозван доступ.
      if (this.terminal || this.opts.isTerminal?.()) {
        this.finish('closed');
        return;
      }
      this.scheduleReconnect();
    } catch (error) {
      if (gen !== this.generation) return;
      this.handleError(error);
    } finally {
      if (gen === this.generation && this.idleTimer) {
        clearTimeout(this.idleTimer);
        this.idleTimer = null;
      }
    }
  }

  private async read(body: ReadableStream<Uint8Array>, controller: AbortController, gen: number): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    const parser = new SseParser();
    const terminalTypes = this.opts.terminalEventTypes ?? DEFAULT_TERMINAL_EVENTS;
    const onAbort = () => void reader.cancel().catch(() => undefined);
    controller.signal.addEventListener('abort', onAbort);
    this.armIdle(controller);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done || gen !== this.generation) break;
        this.armIdle(controller);
        const frames = parser.feed(decoder.decode(value, { stream: true }));
        if (parser.retryMs !== undefined) this.serverRetryMs = parser.retryMs;
        const fresh: StreamEvent[] = [];
        const cursorMode = this.opts.resume === 'cursor';
        for (const frame of frames) {
          const event = parseStreamEvent(frame);
          if (cursorMode && event && frame.id !== undefined) this.lastCursor = frame.id;
          if (event && this.dedup.accept(event, !cursorMode)) {
            fresh.push(event);
            if (terminalTypes.includes(event.type)) this.terminal = true;
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
    if ((error.status === 410 || error.code === 'replay_window_exceeded') && this.opts.resume === 'cursor') {
      // Курсор вне окна повтора: состояние — из REST, поток — «с текущего момента».
      if (this.lastCursor === undefined) {
        this.finish('failed', error);
        return;
      }
      this.lastCursor = undefined;
      this.opts.onResync?.();
      this.scheduleReconnect(0);
      return;
    }
    if (error.status === 410 || error.code === 'replay_window_exceeded') {
      const lastSeq = error.problem?.last_seq;
      if (typeof lastSeq === 'number' && Number.isSafeInteger(lastSeq) && lastSeq >= 0 && this.afterSeq === null) {
        // История вне окна хранения (OQ-35: 410, а не синтетический resync): состояние берём
        // из REST, ленту продолжаем «с текущего max».
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
    if (this.stopped || this.suspended) return;
    this.controller = null;
    this.failures += 1;
    const base = this.serverRetryMs ?? this.opts.retryMs ?? 3000;
    const max = this.opts.maxRetryMs ?? 30_000;
    // Экспоненциальный backoff с джиттером ±20% только после ошибок подряд (первый повтор — `retry:` сервера).
    const exp = Math.min(base * 2 ** Math.max(this.failures - 1, 0), max);
    const delay = delayMs ?? (this.failures > 1 ? Math.round(exp * (0.8 + Math.random() * 0.4)) : exp);
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
