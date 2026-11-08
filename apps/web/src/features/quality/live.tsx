import { useQueryClient, type QueryKey } from '@tanstack/react-query';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import type { RunEvent } from '../../api/runEvents';
import type { StreamState } from '../../api/sse';
import { useRunEvents } from '../../api/useRunEvents';

/**
 * Живые обновления экрана run через SSE (общий StreamManager, Last-Event-ID и дедуп — в нём):
 * события `types` инвалидируют перечисленные REST-запросы; 410 (окно истории) — перечитать всё.
 * runId = null — не подписываться (например, подтверждение вне run).
 */
export function useRunInvalidation(
  runId: string | null | undefined,
  types: readonly string[],
  keys: readonly QueryKey[],
  isTerminal?: () => boolean,
): { state: StreamState; reconnect: () => void } {
  const queryClient = useQueryClient();
  const keysJson = JSON.stringify(keys);
  const invalidate = useCallback(() => {
    for (const key of JSON.parse(keysJson) as QueryKey[]) void queryClient.invalidateQueries({ queryKey: key });
  }, [queryClient, keysJson]);
  const onEvents = useCallback(
    (events: RunEvent[]) => {
      if (events.some((e) => types.includes(e.type))) invalidate();
    },
    [invalidate, types],
  );
  const { state, reconnect } = useRunEvents(runId ?? '', {
    filters: { types },
    limit: 0,
    onEvents,
    onResync: invalidate,
    ...(isTerminal ? { isTerminal } : {}),
  });
  return { state: runId ? state : 'closed', reconnect };
}

/** Строка состояния живых обновлений (role=status) + повтор подключения после отказа. */
export function LiveStatus({ state, onReconnect }: { state: StreamState; onReconnect: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-3">
      <p role="status" className="text-sm text-fg-muted" data-testid="live-state" data-state={state}>
        {t(`quality.live.${state}`)}
      </p>
      {state === 'failed' ? (
        <button type="button" className="btn-secondary btn-sm" onClick={onReconnect}>
          {t('quality.live.reconnect')}
        </button>
      ) : null}
    </div>
  );
}
