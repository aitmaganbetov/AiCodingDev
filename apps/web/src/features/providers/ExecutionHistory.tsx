import { useInfiniteQuery } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { providerKeys, providersApi, type ExecutionStatus, type Provider, type ProviderExecution, type ProviderExecutionsQuery } from '../../api/providers';
import { AgentBadge, Badge, LoadMore, RelativeTime, TileError, VirtualList, type Tone } from '../../components/ui';
import { statusIcon } from '../../components/StatusBadge';

export const EXECUTIONS_PAGE = 50;

export const EXECUTION_STATUSES = [
  'QUEUED',
  'STARTING',
  'RUNNING',
  'EXITED',
  'COLLECTED',
  'TIMED_OUT',
  'CANCELLED',
  'CRASHED',
  'AUTH_REQUIRED',
  'RATE_LIMITED',
] as const satisfies readonly ExecutionStatus[];

/** Сбои исполнения (как `errors` + `rate_limited` + `auth_required` analytics.providers). */
export const FAILURE_STATUSES: readonly ExecutionStatus[] = ['TIMED_OUT', 'CRASHED', 'AUTH_REQUIRED', 'RATE_LIMITED'];

const EXECUTION_TONE: Record<ExecutionStatus, Tone> = {
  QUEUED: 'neutral',
  STARTING: 'info',
  RUNNING: 'info',
  EXITED: 'info',
  COLLECTED: 'success',
  TIMED_OUT: 'danger',
  CANCELLED: 'neutral',
  CRASHED: 'danger',
  AUTH_REQUIRED: 'warning',
  RATE_LIMITED: 'warning',
};

/** Значение фильтра (URL-независимое состояние карточки) → параметр `status` операции. */
export function executionStatusFilter(value: string): ExecutionStatus[] | undefined {
  if (value === 'failures') return [...FAILURE_STATUSES];
  return (EXECUTION_STATUSES as readonly string[]).includes(value) ? [value as ExecutionStatus] : undefined;
}

export function ExecutionDuration({ ms }: { ms: number | null }) {
  const { t } = useTranslation();
  if (ms === null) return null;
  if (ms < 1000) return <>{t('providers.history.ms', { value: ms })}</>;
  const sec = Math.round(ms / 1000);
  if (sec < 60) return <>{t('providers.history.sec', { value: sec })}</>;
  return <>{t('providers.history.min', { m: Math.floor(sec / 60), s: sec % 60 })}</>;
}

function ExecutionRow({ workspaceId, e }: { workspaceId: string; e: ProviderExecution }) {
  const { t } = useTranslation();
  const mission = `/w/${workspaceId}/mission-control?run=${encodeURIComponent(e.run_id)}&task=${encodeURIComponent(e.task_id)}`;
  return (
    <div className="flex min-w-0 flex-col gap-1 border-b border-border px-3 py-2 text-sm last:border-b-0" data-execution-id={e.id} data-status={e.status}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge tone={EXECUTION_TONE[e.status] ?? 'neutral'} icon={statusIcon(e.status)}>
          {t(`providers.enums.execution_status.${e.status}`, { defaultValue: e.status })}
        </Badge>
        <AgentBadge agentRole={e.agent_role} />
        <span className="ml-auto text-xs text-fg-muted">
          <RelativeTime value={e.created_at} />
        </span>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-fg-muted">
        <span>{t('providers.history.attempt', { n: e.attempt_no })}</span>
        <span className="font-mono">{e.exit_code === null ? t('providers.history.noExitCode') : t('providers.history.exitCode', { code: e.exit_code })}</span>
        {e.duration_ms !== null ? (
          <span className="tabular-nums">
            <ExecutionDuration ms={e.duration_ms} />
          </span>
        ) : null}
        <span>{t('providers.history.timeout', { value: e.timeout_seconds })}</span>
        {e.failure_category ? (
          <span className="font-medium text-danger-text">{t(`providers.enums.cli_error.${e.failure_category}`, { defaultValue: e.failure_category })}</span>
        ) : null}
        <Link to={mission} className="link ml-auto inline-flex min-h-target items-center" aria-label={t('providers.history.openMissionLabel')}>
          {t('providers.history.openMission')}
        </Link>
      </div>
    </div>
  );
}

/** История исполнений CLI провайдера (B1 `listProviderExecutions`): фильтр статуса, виртуализация, cursor-пагинация. */
export function ExecutionHistory({ workspaceId, provider }: { workspaceId: string; provider: Provider }) {
  const { t } = useTranslation();
  const uid = useId();
  const [filter, setFilter] = useState('');
  const status = executionStatusFilter(filter);
  const query: ProviderExecutionsQuery = { limit: EXECUTIONS_PAGE, ...(status ? { status } : {}) };
  const executions = useInfiniteQuery({
    queryKey: providerKeys.executions(workspaceId, provider.provider_id, query),
    queryFn: ({ pageParam, signal }) =>
      providersApi.executions(workspaceId, provider.provider_id, { ...query, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = executions.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <div className="flex min-w-0 flex-col gap-2" data-testid={`executions-${provider.code}`}>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor={`${uid}-status`} className="text-sm font-medium text-fg">
            {t('providers.history.filter')}
          </label>
          <select id={`${uid}-status`} className="select" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">{t('providers.history.anyStatus')}</option>
            <option value="failures">{t('providers.history.failuresOnly')}</option>
            {EXECUTION_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`providers.enums.execution_status.${s}`)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {executions.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-2">
          <span className="sr-only">{t('providers.loading')}</span>
          <span aria-hidden="true" className="skeleton block h-12 w-full" />
          <span aria-hidden="true" className="skeleton block h-12 w-full" />
        </div>
      ) : executions.isError && items.length === 0 ? (
        <TileError error={executions.error} onRetry={() => void executions.refetch()} />
      ) : items.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border p-3 text-sm text-fg-muted" data-testid="executions-empty">
          {status ? t('providers.history.emptyFiltered') : t('providers.history.empty')}
        </p>
      ) : (
        <>
          <VirtualList
            items={items}
            getKey={(e) => e.id}
            renderItem={(e) => <ExecutionRow workspaceId={workspaceId} e={e} />}
            label={t('providers.history.label', { name: provider.display_name })}
            estimateSize={68}
            className="h-80 rounded-lg border border-border"
          />
          <LoadMore
            shown={items.length}
            hasNextPage={executions.hasNextPage}
            isFetchingNextPage={executions.isFetchingNextPage}
            nextPageError={executions.isFetchNextPageError ? executions.error : null}
            onLoadMore={() => void executions.fetchNextPage()}
          />
        </>
      )}
      <p className="text-xs text-fg-subtle">{t('providers.history.source')}</p>
    </div>
  );
}
