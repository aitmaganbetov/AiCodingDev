import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { ApiError } from './client';
import type { StreamEvent, StreamState } from './sse';
import { StreamManager, streamKey, type StreamSpec } from './streamManager';

const EMPTY: StreamEvent[] = [];

let defaultManager: StreamManager | null = null;

/** Менеджер потоков вкладки (один на приложение; тесты передают свой через провайдер). */
export function getDefaultStreamManager(): StreamManager {
  defaultManager ??= new StreamManager();
  return defaultManager;
}

const StreamManagerContext = createContext<StreamManager | null>(null);

export function StreamManagerProvider({ manager, children }: { manager: StreamManager; children: ReactNode }) {
  return <StreamManagerContext.Provider value={manager}>{children}</StreamManagerContext.Provider>;
}

export function useStreamManager(): StreamManager {
  return useContext(StreamManagerContext) ?? getDefaultStreamManager();
}

export interface UseEventStreamOptions {
  /** Новые события (после дедупликации) — например, для инвалидации REST-запросов. */
  onEvents?: (events: StreamEvent[]) => void;
  onResync?: () => void;
  isTerminal?: () => boolean;
  /** Сколько последних событий держать в `events` (0 — не накапливать). */
  limit?: number;
}

export interface EventStreamView {
  events: StreamEvent[];
  state: StreamState;
  error: ApiError | null;
  /** Перезапуск с полной истории (после состояния failed). */
  reconnect: () => void;
}

/**
 * Подписка компонента на SSE-поток через общий StreamManager. Несколько компонентов с
 * одинаковым `spec` делят одно соединение; `spec = null` — не подписываться.
 */
export function useEventStream(spec: StreamSpec | null, options: UseEventStreamOptions = {}): EventStreamView {
  const manager = useStreamManager();
  // События хранятся вместе с ключом потока: смена потока сразу даёт пустую ленту.
  const [feed, setFeed] = useState<{ key: string | null; list: StreamEvent[] }>({ key: null, list: [] });
  const [state, setState] = useState<StreamState>('connecting');
  const [error, setError] = useState<ApiError | null>(null);
  // Колбэки и spec читаются через ref: смена их идентичности не должна переоткрывать поток.
  const optionsRef = useRef(options);
  const specRef = useRef(spec);
  useEffect(() => {
    optionsRef.current = options;
    specRef.current = spec;
  });
  const key = spec ? streamKey(spec) : null;
  const limit = options.limit ?? 500;

  useEffect(() => {
    const current = specRef.current;
    if (!key || !current) return undefined;
    return manager.subscribe(current, {
      onEvents: (fresh) => {
        if (limit > 0) {
          setFeed((prev) => {
            const next = (prev.key === key ? prev.list : []).concat(fresh);
            return { key, list: next.length > limit ? next.slice(next.length - limit) : next };
          });
        }
        optionsRef.current.onEvents?.(fresh);
      },
      onState: (s, err) => {
        setState(s);
        setError(err);
      },
      onResync: () => optionsRef.current.onResync?.(),
      onReset: () => setFeed({ key, list: [] }),
      isTerminal: () => optionsRef.current.isTerminal?.() ?? false,
    });
  }, [manager, key, limit]);

  const reconnect = useMemo(
    () => () => {
      if (specRef.current) manager.restart(specRef.current);
    },
    [manager],
  );
  const events = feed.key === key ? feed.list : EMPTY;
  return { events, state, error, reconnect };
}
