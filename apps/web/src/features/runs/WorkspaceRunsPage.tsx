import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router';
import { projectsApi } from '../../api/projects';
import { runKeys, runsApi, type RunStatus, type RunSummary } from '../../api/runs';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Timestamp } from '../../components/ui/Time';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';

export const WORKSPACE_RUNS_PAGE_SIZE = 20;
/** Проекты для фильтра: одна страница (максимум контракта — 200). */
const PROJECTS_FILTER_LIMIT = 200;

export const RUN_STATUSES = [
  'CREATED',
  'DISCOVERY',
  'PLANNED',
  'AWAITING_APPROVAL',
  'RUNNING',
  'VERIFYING',
  'PAUSED',
  'BLOCKED',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
] as const satisfies readonly RunStatus[];

const FINISHED: readonly RunStatus[] = ['COMPLETED', 'FAILED', 'CANCELLED'];
/** Наборы статусов фильтра: `status` повторяемый (listRuns), в URL — имя набора или один статус. */
const PRESETS: Record<string, readonly RunStatus[]> = {
  active: RUN_STATUSES.filter((s) => !FINISHED.includes(s)),
  finished: FINISHED,
};

export function statusesForFilter(value: string | null): RunStatus[] | undefined {
  if (!value) return undefined;
  const preset = PRESETS[value];
  if (preset) return [...preset];
  return (RUN_STATUSES as readonly string[]).includes(value) ? [value as RunStatus] : undefined;
}

function RunItem({ run, workspaceId, projectName, canQuality }: { run: RunSummary; workspaceId: string; projectName?: string; canQuality: boolean }) {
  const { t } = useTranslation();
  const base = `/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}`;
  const label = run.objective.slice(0, 80);
  const links = [
    { to: base, text: t('runs.open') },
    { to: `${base}/graph`, text: t('runs.nav.graph') },
    { to: `${base}/chat`, text: t('runs.nav.chat') },
    ...(canQuality ? [{ to: `/w/${workspaceId}/quality/runs/${run.id}`, text: t('runs.nav.quality') }] : []),
  ];
  return (
    <li className="card flex flex-col gap-2" data-run-id={run.id}>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge kind="run" status={run.status} />
        <span className="text-fg-muted">{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</span>
        {projectName ? (
          <Link to={`/w/${workspaceId}/projects/${run.project_id}/runs`} className="text-link underline">
            {projectName}
          </Link>
        ) : null}
        <span className="text-fg-muted">
          {t('runs.createdAt')}: <Timestamp value={run.created_at} />
        </span>
      </div>
      <p className="line-clamp-3 whitespace-pre-line break-words">{run.objective}</p>
      <ul className="flex flex-wrap gap-4" aria-label={t('runs.all.linksLabel', { objective: label })}>
        {links.map((l) => (
          <li key={l.to}>
            <Link to={l.to} className="font-medium text-link underline">
              {l.text}
              <span className="sr-only">: {label}</span>
            </Link>
          </li>
        ))}
      </ul>
    </li>
  );
}

/** Запуски workspace (GET /runs): фильтры статуса и проекта в URL, cursor-пагинация. */
export function WorkspaceRunsPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const statusParam = params.get('status');
  const statuses = statusesForFilter(statusParam);
  const statusValue = statuses ? (statusParam ?? '') : '';
  const projectId = params.get('project_id') ?? '';
  const enabled = Boolean(workspaceId && membership);

  const projects = useQuery({
    queryKey: ['projects', workspaceId ?? '', 'filter'],
    queryFn: ({ signal }) => projectsApi.list(workspaceId ?? '', { limit: PROJECTS_FILTER_LIMIT }, signal),
    enabled,
  });
  const projectNames = useMemo(() => new Map((projects.data?.items ?? []).map((p) => [p.id, p.name])), [projects.data]);

  const runs = useInfiniteQuery({
    queryKey: [...runKeys.list(workspaceId ?? '', projectId || undefined), { status: statusValue }],
    queryFn: ({ pageParam, signal }) =>
      runsApi.list(
        workspaceId ?? '',
        {
          limit: WORKSPACE_RUNS_PAGE_SIZE,
          ...(projectId ? { project_id: projectId } : {}),
          ...(statuses ? { status: statuses } : {}),
          ...(pageParam ? { cursor: pageParam } : {}),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
  });

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const canQuality = can(membership.role, 'quality:read');
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(statuses || projectId);
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('runs.allTitle')} />
      <p className="mb-4 text-fg-muted">{t('runs.all.description')}</p>
      <div role="group" aria-label={t('runs.all.filters')} className="card mb-6 flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="runs-filter-status" className="text-sm font-medium text-fg">
            {t('runs.status')}
          </label>
          <select id="runs-filter-status" className="input" value={statusValue} onChange={(e) => update('status', e.target.value)}>
            <option value="">{t('runs.all.anyStatus')}</option>
            <option value="active">{t('runs.all.active')}</option>
            <option value="finished">{t('runs.all.finished')}</option>
            {RUN_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`enums.run_status.${s}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="runs-filter-project" className="text-sm font-medium text-fg">
            {t('runs.all.project')}
          </label>
          <select id="runs-filter-project" className="input" value={projectId} onChange={(e) => update('project_id', e.target.value)}>
            <option value="">{t('runs.all.anyProject')}</option>
            {projectId && !projectNames.has(projectId) ? <option value={projectId}>{projectId}</option> : null}
            {(projects.data?.items ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
        {filtered ? (
          <button type="button" className="btn-secondary" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
            {t('runs.all.reset')}
          </button>
        ) : null}
      </div>
      {runs.isPending ? (
        <LoadingState label={t('runs.loading')} />
      ) : runs.isError && items.length === 0 ? (
        <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title={filtered ? t('runs.all.emptyFilteredTitle') : t('runs.emptyTitle')}
          description={filtered ? t('runs.all.emptyFilteredDescription') : t('runs.all.emptyDescription')}
        />
      ) : (
        <>
          <ul className="flex flex-col gap-3" aria-label={t('runs.listLabel')}>
            {items.map((r) => (
              <RunItem key={r.id} run={r} workspaceId={workspaceId} projectName={projectNames.get(r.project_id)} canQuality={canQuality} />
            ))}
          </ul>
          <LoadMore
            shown={items.length}
            hasNextPage={runs.hasNextPage}
            isFetchingNextPage={runs.isFetchingNextPage}
            nextPageError={runs.isFetchNextPageError ? runs.error : null}
            onLoadMore={() => void runs.fetchNextPage()}
          />
        </>
      )}
    </section>
  );
}
