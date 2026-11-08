import { useEffect, useRef, useState } from 'react';
import type { ApiError } from './client';
import { RunEventStream, type RunEvent, type StreamState } from './runEvents';

/** Сколько последних событий держит лента (полная история — GET /runs/{id}/activity). */
export const FEED_LIMIT = 500;

interface Options {
  /** Новые события (после дедупликации) — для инвалидации REST-запросов. */
  onEvents?: (events: RunEvent[]) => void;
  onResync?: () => void;
  isTerminal?: () => boolean;
}

export function useRunEvents(runId: string, options: Options = {}) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [state, setState] = useState<StreamState>('connecting');
  const [error, setError] = useState<ApiError | null>(null);
  const [attempt, setAttempt] = useState(0);
  // Колбэки читаются через ref: смена их идентичности не должна переоткрывать поток.
  const optionsRef = useRef(options);
  useEffect(() => {
    optionsRef.current = options;
  });

  useEffect(() => {
    const stream = new RunEventStream({
      runId,
      onEvents: (fresh) => {
        setEvents((prev) => {
          const next = prev.concat(fresh);
          return next.length > FEED_LIMIT ? next.slice(next.length - FEED_LIMIT) : next;
        });
        optionsRef.current.onEvents?.(fresh);
      },
      onState: (s, err) => {
        setState(s);
        setError(err ?? null);
      },
      onResync: () => optionsRef.current.onResync?.(),
      isTerminal: () => optionsRef.current.isTerminal?.() ?? false,
    });
    stream.start();
    return () => stream.stop();
  }, [runId, attempt]);

  // Новый поток после фатальной ошибки начинается с полной истории (replay) — ленту очищаем.
  // Смена runId обрабатывается снаружи через key (компонент монтируется заново).
  const reconnect = () => {
    setEvents([]);
    setError(null);
    setAttempt((n) => n + 1);
  };
  return { events, state, error, reconnect };
}
