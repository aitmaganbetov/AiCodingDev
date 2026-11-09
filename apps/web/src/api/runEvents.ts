/**
 * Поток событий run: GET /runs/{run_id}/events (API_DESIGN §13). Транспорт, дедупликация и
 * возобновление — src/api/sse.ts; разделение потока между экранами — src/api/streamManager.ts.
 */
import { seg } from './client';
import {
  EventDeduper,
  EventStream,
  parseStreamEvent,
  SseParser,
  type EventStreamOptions,
  type SseFrame,
  type StreamEvent,
  type StreamState,
} from './sse';
import type { StreamSpec } from './streamManager';

export { EventDeduper, SseParser };
export type { SseFrame, StreamState };

/** Событие run — конверт SseEnvelope. */
export type RunEvent = StreamEvent;
export const parseRunEvent = parseStreamEvent;

/** После этого события сервер закрывает поток run навсегда. */
export const RUN_TERMINAL_EVENT = 'run.completed';

export interface RunStreamFilters {
  /** Только эти типы событий (разрывы seq допустимы, API_DESIGN §13 п.5). */
  types?: readonly string[];
  task_id?: string;
}

/** Спецификация потока run для StreamManager/useEventStream (фильтры — часть идентичности). */
export function runEventsSpec(runId: string, filters: RunStreamFilters = {}): StreamSpec {
  const query: StreamSpec['query'] = {};
  if (filters.types && filters.types.length > 0) query.types = filters.types;
  if (filters.task_id) query.task_id = filters.task_id;
  return {
    path: `/runs/${seg(runId)}/events`,
    ...(Object.keys(query).length > 0 ? { query } : {}),
    terminalEventTypes: [RUN_TERMINAL_EVENT],
  };
}

export interface RunEventStreamOptions extends Omit<EventStreamOptions, 'path' | 'query' | 'terminalEventTypes'> {
  runId: string;
  filters?: RunStreamFilters;
}

/** Одиночный поток run без менеджера (низкоуровневый; экраны используют useRunEvents). */
export class RunEventStream extends EventStream {
  constructor({ runId, filters, ...opts }: RunEventStreamOptions) {
    const spec = runEventsSpec(runId, filters);
    super({ ...opts, path: spec.path, query: spec.query, terminalEventTypes: spec.terminalEventTypes });
  }
}
