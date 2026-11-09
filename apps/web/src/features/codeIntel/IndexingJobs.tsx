/**
 * Indexing Status (B3 `listIndexingJobs`): очередь и история индексации репозитория — статус, вид,
 * commit, попытки, время постановки/старта/завершения, ожидание и длительность, построенный снимок,
 * безопасный текст ошибки (секреты и пути хоста удаляет сервер; показывается только как текст).
 * Пока есть задания в очереди/работе — список перечитывается каждые 15 с.
 */
import { useInfiniteQuery } from '@tanstack/react-query';
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { INDEXING_JOB_STATUSES, codeIntelApi, codeIntelKeys, type IndexingJob, type IndexingJobStatus } from '../../api/codeIntel';
import { EmptyState, ErrorState, LoadMore, Skeleton, Table, Timestamp, type Column } from '../../components/ui';
import { JOB_ACTIVE, formatSeconds } from './model';
import { JobStatusBadge, Sha } from './shared';
import type { TabProps } from './SymbolSearch';

export const JOBS_PAGE_SIZE = 20;
export const JOBS_POLL_MS = 15_000;

/** Последнее задание индексации (для сводки индекса в шапке экрана). */
export function latestJobQuery(projectId: string, repositoryId: string) {
  return {
    queryKey: [...codeIntelKeys.indexingJobs(projectId, repositoryId, { limit: 1 }), 'latest'] as const,
    queryFn: ({ signal }: { signal: AbortSignal }) => codeIntelApi.indexingJobs(projectId, repositoryId, { limit: 1 }, signal),
  };
}

export function IndexingJobs({ projectId, repositoryId, statusFilter, onStatusFilter }: Pick<TabProps, 'projectId' | 'repositoryId'> & {
  statusFilter: IndexingJobStatus | '';
  onStatusFilter: (s: IndexingJobStatus | '') => void;
}) {
  const { t } = useTranslation();
  const uid = useId();
  const units = { h: t('codeIntel.units.h'), m: t('codeIntel.units.m'), s: t('codeIntel.units.s') };
  const query = { limit: JOBS_PAGE_SIZE, ...(statusFilter ? { status: statusFilter } : {}) };
  const jobs = useInfiniteQuery({
    queryKey: codeIntelKeys.indexingJobs(projectId, repositoryId, query),
    queryFn: ({ pageParam, signal }) => codeIntelApi.indexingJobs(projectId, repositoryId, { ...query, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (q) => (q.state.data?.pages.some((p) => p.items.some((j) => JOB_ACTIVE.includes(j.status))) ? JOBS_POLL_MS : false),
  });
  const items = jobs.data?.pages.flatMap((p) => p.items) ?? [];
  const columns: Column<IndexingJob>[] = [
    { id: 'status', header: t('codeIntel.jobs.col.status'), sortValue: (j) => j.status, cell: (j) => <JobStatusBadge status={j.status} /> },
    {
      id: 'commit',
      header: t('codeIntel.jobs.col.commit'),
      rowHeader: true,
      cell: (j) => (
        <span className="flex flex-col">
          <Sha sha={j.commit_sha} />
          <span className="text-xs text-fg-muted">{t(`codeIntel.jobs.kind.${j.kind}`)}</span>
        </span>
      ),
    },
    { id: 'attempts', header: t('codeIntel.jobs.col.attempts'), sortValue: (j) => j.attempt_count, cell: (j) => j.attempt_count },
    {
      id: 'times',
      header: t('codeIntel.jobs.col.times'),
      sortValue: (j) => j.created_at,
      cell: (j) => (
        <span className="flex flex-col text-xs">
          <span>
            {t('codeIntel.jobs.created')} <Timestamp value={j.created_at} />
          </span>
          {j.started_at ? (
            <span>
              {t('codeIntel.jobs.started')} <Timestamp value={j.started_at} />
            </span>
          ) : null}
          {j.finished_at ? (
            <span>
              {t('codeIntel.jobs.finished')} <Timestamp value={j.finished_at} />
            </span>
          ) : null}
        </span>
      ),
    },
    {
      id: 'queue',
      header: t('codeIntel.jobs.col.queue'),
      sortValue: (j) => j.queue_seconds,
      cell: (j) => formatSeconds(j.queue_seconds, units) ?? <span className="text-fg-muted">{t('codeIntel.jobs.notStarted')}</span>,
    },
    {
      id: 'duration',
      header: t('codeIntel.jobs.col.duration'),
      sortValue: (j) => j.duration_seconds,
      cell: (j) => formatSeconds(j.duration_seconds, units) ?? <span className="text-fg-muted">{t('codeIntel.jobs.notFinished')}</span>,
    },
    {
      id: 'snapshot',
      header: t('codeIntel.jobs.col.snapshot'),
      cell: (j) =>
        j.file_count !== null || j.symbol_count !== null ? (
          <span className="text-xs">{t('codeIntel.jobs.counts', { files: j.file_count ?? '—', symbols: j.symbol_count ?? '—' })}</span>
        ) : (
          <span className="text-fg-muted">—</span>
        ),
    },
    {
      id: 'error',
      header: t('codeIntel.jobs.col.error'),
      cell: (j) =>
        j.error ? (
          <p className="max-w-[28rem] whitespace-pre-wrap break-words text-xs text-danger-text" data-testid="job-error">
            {j.error}
          </p>
        ) : (
          <span className="text-fg-muted">—</span>
        ),
    },
  ];
  return (
    <div className="flex flex-col gap-3" data-testid="indexing-jobs">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-status`} className="text-sm font-medium text-fg">
            {t('codeIntel.jobs.status')}
          </label>
          <select id={`${uid}-status`} className="select" value={statusFilter} onChange={(e) => onStatusFilter(e.target.value as IndexingJobStatus | '')}>
            <option value="">{t('codeIntel.jobs.statusAll')}</option>
            {INDEXING_JOB_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`codeIntel.jobStatus.${s}`)}
              </option>
            ))}
          </select>
        </div>
        <p className="text-sm text-fg-muted">{t('codeIntel.jobs.note')}</p>
      </div>
      {jobs.isPending ? (
        <Skeleton lines={4} />
      ) : jobs.isError && items.length === 0 ? (
        <ErrorState error={jobs.error} onRetry={() => void jobs.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title={statusFilter ? t('codeIntel.jobs.emptyFiltered') : t('codeIntel.jobs.emptyTitle')}
          description={statusFilter ? undefined : t('codeIntel.jobs.empty')}
          icon="layers"
        />
      ) : (
        <>
          <Table caption={t('codeIntel.jobs.caption')} columns={columns} rows={items} rowKey={(j) => j.id} density="compact" minWidthClass="min-w-[56rem]" rowProps={(j) => ({ 'data-job-status': j.status })} />
          {jobs.hasNextPage || jobs.isFetchNextPageError ? (
            <LoadMore
              shown={items.length}
              hasNextPage={jobs.hasNextPage}
              isFetchingNextPage={jobs.isFetchingNextPage}
              nextPageError={jobs.isFetchNextPageError ? jobs.error : null}
              onLoadMore={() => void jobs.fetchNextPage()}
            />
          ) : null}
        </>
      )}
    </div>
  );
}
