import { useTranslation } from 'react-i18next';
import type { StreamState } from '../../api/runEvents';
import type { ApiError } from '../../api/client';
import { problemMessage } from '../../lib/problem';

/** Состояние live-потока событий run (общие строки run.stream.*) + переподключение после failed. */
export function StreamStatus({ state, error, onReconnect }: { state: StreamState; error: ApiError | null; onReconnect: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-3">
      <p role="status" className="text-sm text-fg-muted" data-testid="stream-state" data-state={state}>
        {t(`run.stream.${state}`)}
      </p>
      {state === 'failed' ? (
        <>
          <button type="button" className="btn-secondary btn-sm" onClick={onReconnect}>
            {t('run.reconnect')}
          </button>
          {error ? <p className="text-sm font-medium text-danger-text">{problemMessage(t, error)}</p> : null}
        </>
      ) : null}
    </div>
  );
}
