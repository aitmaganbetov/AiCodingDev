import { useMemo } from 'react';
import { runEventsSpec, type RunEvent, type RunStreamFilters } from './runEvents';
import { useEventStream } from './useEventStream';

/** Сколько последних событий держит лента (полная история — GET /runs/{id}/activity). */
export const FEED_LIMIT = 500;

interface Options {
  /** Новые события (после дедупликации) — для инвалидации REST-запросов. */
  onEvents?: (events: RunEvent[]) => void;
  onResync?: () => void;
  isTerminal?: () => boolean;
  filters?: RunStreamFilters;
  limit?: number;
}

/**
 * Лента событий run через общий StreamManager: Run view, граф, чат и т.д. на одном run
 * делят одно SSE-соединение (лимит 5 потоков на пользователя, API_DESIGN §13).
 */
export function useRunEvents(runId: string, { filters, limit = FEED_LIMIT, ...options }: Options = {}) {
  const types = filters?.types?.join(',');
  const taskId = filters?.task_id;
  const spec = useMemo(
    () => (runId ? runEventsSpec(runId, { ...(types ? { types: types.split(',') } : {}), ...(taskId ? { task_id: taskId } : {}) }) : null),
    [runId, types, taskId],
  );
  return useEventStream(spec, { ...options, limit });
}
