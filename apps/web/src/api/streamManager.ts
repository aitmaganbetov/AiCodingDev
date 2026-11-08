/**
 * Менеджер SSE-потоков (P7-02, ADR-0014 п.3): один HTTP-поток на (путь + фильтры),
 * сколько бы экранов/панелей на него ни подписалось.
 *
 *  - Ref-counting: подписчики одного потока делят соединение; после ухода последнего
 *    поток закрывается с задержкой `lingerMs` (навигация туда-обратно и StrictMode
 *    не порождают переподключений).
 *  - Поздний подписчик сразу получает буфер уже принятых событий (до `bufferSize`) и текущее состояние.
 *  - Лимит: не более `maxStreams` одновременных потоков (API_DESIGN §13: ≤ 5 на пользователя,
 *    6-й ⇒ 429). Лишние ждут в очереди (state `queued`) и подключаются, когда слот освободится.
 *  - Возобновление/дедуп/backoff/heartbeat-watchdog/410 — в EventStream (src/api/sse.ts).
 *  - Сеть: событие `offline` разрывает потоки (state `offline`), `online` — немедленно
 *    переподключает с Last-Event-ID, без ожидания backoff.
 *  - Видимость (необязательно): вкладка скрыта дольше `hiddenPauseMs` — потоки
 *    приостанавливаются (освобождают серверный слот), при возврате — возобновляются.
 */
import type { ApiError } from './client';
import { EventStream, type EventStreamOptions, type StreamEvent, type StreamQuery, type StreamState } from './sse';

export interface StreamSpec {
  /** Путь относительно /api/v1, например `/runs/{id}/events`. */
  path: string;
  query?: StreamQuery;
  terminalEventTypes?: readonly string[];
}

export interface StreamSubscriber {
  /** Новые события (после дедупликации); при подписке — буфер уже принятых. */
  onEvents?: (events: StreamEvent[]) => void;
  onState?: (state: StreamState, error: ApiError | null) => void;
  /** 410: часть истории недоступна — перечитать состояние через REST. */
  onResync?: () => void;
  /** Поток перезапущен с нуля (restart после фатальной ошибки) — очистить накопленное. */
  onReset?: () => void;
  /** Объект потока завершён (например, run в терминальном статусе) — не переподключаться. */
  isTerminal?: () => boolean;
}

export interface StreamManagerOptions {
  maxStreams?: number;
  lingerMs?: number;
  bufferSize?: number;
  /** null — не приостанавливать потоки скрытой вкладки. */
  hiddenPauseMs?: number | null;
  /** Параметры каждого EventStream (тесты уменьшают задержки). */
  streamOptions?: Pick<EventStreamOptions, 'retryMs' | 'maxRetryMs' | 'idleTimeoutMs'>;
  /** Источник событий online/offline/visibilitychange (по умолчанию window/document). */
  environment?: { window?: Window | undefined; document?: Document | undefined } | null;
}

interface Entry {
  key: string;
  spec: StreamSpec;
  stream: EventStream | null;
  subscribers: Set<StreamSubscriber>;
  buffer: StreamEvent[];
  state: StreamState;
  error: ApiError | null;
  linger: ReturnType<typeof setTimeout> | null;
}

/** Идентичность потока: путь + отсортированные фильтры. */
export function streamKey(spec: StreamSpec): string {
  const q = spec.query ?? {};
  const parts = Object.keys(q)
    .sort()
    .flatMap((k) => {
      const v = q[k];
      if (v === undefined || v === null || v === '') return [];
      return [`${k}=${Array.isArray(v) ? [...(v as readonly string[])].sort().join(',') : String(v)}`];
    });
  return parts.length > 0 ? `${spec.path}?${parts.join('&')}` : spec.path;
}

const ACTIVE: ReadonlySet<StreamState> = new Set(['connecting', 'open', 'reconnecting', 'paused', 'offline']);

export class StreamManager {
  readonly maxStreams: number;
  private readonly lingerMs: number;
  private readonly bufferSize: number;
  private readonly hiddenPauseMs: number | null;
  private readonly entries = new Map<string, Entry>();
  private readonly queue: Entry[] = [];
  private hiddenTimer: ReturnType<typeof setTimeout> | null = null;
  private offline = false;
  private hiddenPaused = false;
  private readonly detach: () => void;

  constructor(private readonly opts: StreamManagerOptions = {}) {
    this.maxStreams = opts.maxStreams ?? 5;
    this.lingerMs = opts.lingerMs ?? 1500;
    this.bufferSize = opts.bufferSize ?? 1000;
    this.hiddenPauseMs = opts.hiddenPauseMs === undefined ? 60_000 : opts.hiddenPauseMs;
    this.detach = this.attachEnvironment();
  }

  /** Число потоков, занимающих слот (подключены, подключаются, ждут реконнекта или приостановлены). */
  get activeCount(): number {
    let n = 0;
    for (const e of this.entries.values()) if (e.stream && !e.stream.finished) n += 1;
    return n;
  }

  /** Диагностика: состояние потока по ключу. */
  stateOf(spec: StreamSpec): StreamState | undefined {
    return this.entries.get(streamKey(spec))?.state;
  }

  /** Подключение текущего потока (для тестов и отладки). */
  streamOf(spec: StreamSpec): EventStream | null {
    return this.entries.get(streamKey(spec))?.stream ?? null;
  }

  subscribe(spec: StreamSpec, subscriber: StreamSubscriber): () => void {
    const key = streamKey(spec);
    let entry = this.entries.get(key);
    if (!entry) {
      entry = { key, spec, stream: null, subscribers: new Set(), buffer: [], state: 'queued', error: null, linger: null };
      this.entries.set(key, entry);
      entry.subscribers.add(subscriber);
      this.admit(entry);
    } else {
      if (entry.linger) {
        clearTimeout(entry.linger);
        entry.linger = null;
      }
      entry.subscribers.add(subscriber);
      // Поздний подписчик: всё, что уже принято, и текущее состояние.
      if (entry.buffer.length > 0) subscriber.onEvents?.(entry.buffer.slice());
      subscriber.onState?.(entry.state, entry.error);
    }

    let active = true;
    return () => {
      if (!active) return;
      active = false;
      const current = this.entries.get(key);
      if (!current) return;
      current.subscribers.delete(subscriber);
      if (current.subscribers.size === 0) {
        current.linger = setTimeout(() => this.close(current), this.lingerMs);
      }
    };
  }

  /** Начать поток заново с полной истории (после фатальной ошибки, кнопка «Переподключиться»). */
  restart(spec: StreamSpec): void {
    const entry = this.entries.get(streamKey(spec));
    if (!entry) return;
    entry.stream?.stop();
    entry.stream = null;
    entry.buffer = [];
    entry.error = null;
    for (const s of entry.subscribers) s.onReset?.();
    const queued = this.queue.indexOf(entry);
    if (queued >= 0) this.queue.splice(queued, 1);
    this.admit(entry);
  }

  /** Закрыть все потоки и отписаться от событий окружения. */
  dispose(): void {
    this.detach();
    if (this.hiddenTimer) clearTimeout(this.hiddenTimer);
    for (const e of this.entries.values()) {
      if (e.linger) clearTimeout(e.linger);
      e.stream?.stop();
    }
    this.entries.clear();
    this.queue.length = 0;
  }

  private admit(entry: Entry): void {
    if (this.activeCount >= this.maxStreams) {
      this.queue.push(entry);
      this.setState(entry, 'queued', null);
      return;
    }
    this.open(entry);
  }

  private open(entry: Entry): void {
    const stream = new EventStream({
      ...this.opts.streamOptions,
      path: entry.spec.path,
      query: entry.spec.query,
      terminalEventTypes: entry.spec.terminalEventTypes,
      onEvents: (events) => {
        if (entry.stream !== stream) return;
        entry.buffer.push(...events);
        if (entry.buffer.length > this.bufferSize) entry.buffer.splice(0, entry.buffer.length - this.bufferSize);
        for (const s of entry.subscribers) s.onEvents?.(events);
      },
      onState: (state, error) => {
        if (entry.stream !== stream) return;
        this.setState(entry, state, error ?? null);
        if (state === 'closed' || state === 'failed') this.pump();
      },
      onResync: () => {
        for (const s of entry.subscribers) s.onResync?.();
      },
      isTerminal: () => [...entry.subscribers].some((s) => s.isTerminal?.() ?? false),
    });
    entry.stream = stream;
    stream.start();
    if (this.offline) stream.suspend('offline');
    else if (this.hiddenPaused) stream.suspend('paused');
  }

  private setState(entry: Entry, state: StreamState, error: ApiError | null): void {
    entry.state = state;
    entry.error = error;
    for (const s of entry.subscribers) s.onState?.(state, error);
  }

  private close(entry: Entry): void {
    entry.linger = null;
    if (entry.subscribers.size > 0) return;
    entry.stream?.stop();
    entry.stream = null;
    this.entries.delete(entry.key);
    const queued = this.queue.indexOf(entry);
    if (queued >= 0) this.queue.splice(queued, 1);
    this.pump();
  }

  /** Освободился слот — подключить следующий поток из очереди. */
  private pump(): void {
    while (this.queue.length > 0 && this.activeCount < this.maxStreams) {
      const next = this.queue.shift() as Entry;
      if (this.entries.get(next.key) === next) this.open(next);
    }
  }

  private forEachStream(fn: (stream: EventStream) => void): void {
    for (const e of this.entries.values()) if (e.stream && !e.stream.finished && ACTIVE.has(e.state)) fn(e.stream);
  }

  private attachEnvironment(): () => void {
    const env = this.opts.environment === null ? null : this.opts.environment ?? {};
    const win = env ? env.window ?? (typeof window === 'undefined' ? undefined : window) : undefined;
    const doc = env ? env.document ?? (typeof document === 'undefined' ? undefined : document) : undefined;
    if (!win && !doc) return () => undefined;

    const onOffline = () => {
      this.offline = true;
      this.forEachStream((s) => s.suspend('offline'));
    };
    const onOnline = () => {
      this.offline = false;
      if (this.hiddenPaused) {
        this.forEachStream((s) => s.suspend('paused'));
        return;
      }
      this.forEachStream((s) => {
        s.resume();
        s.retryNow();
      });
    };
    const onVisibility = () => {
      if (!doc || this.hiddenPauseMs === null) return;
      if (doc.visibilityState === 'hidden') {
        if (this.hiddenTimer) clearTimeout(this.hiddenTimer);
        this.hiddenTimer = setTimeout(() => {
          this.hiddenTimer = null;
          this.hiddenPaused = true;
          if (!this.offline) this.forEachStream((s) => s.suspend('paused'));
        }, this.hiddenPauseMs);
      } else {
        if (this.hiddenTimer) clearTimeout(this.hiddenTimer);
        this.hiddenTimer = null;
        if (this.hiddenPaused) {
          this.hiddenPaused = false;
          if (!this.offline) this.forEachStream((s) => s.resume());
        }
      }
    };
    win?.addEventListener('offline', onOffline);
    win?.addEventListener('online', onOnline);
    doc?.addEventListener('visibilitychange', onVisibility);
    if (win && win.navigator && win.navigator.onLine === false) this.offline = true;
    return () => {
      win?.removeEventListener('offline', onOffline);
      win?.removeEventListener('online', onOnline);
      doc?.removeEventListener('visibilitychange', onVisibility);
    };
  }
}
