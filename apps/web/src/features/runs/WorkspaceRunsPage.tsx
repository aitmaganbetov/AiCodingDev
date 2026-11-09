import { useInfiniteQuery, useQueries, useQuery, type UseQueryResult } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useSearchParams } from 'react-router';
import { projectsApi } from '../../api/projects';
import { runKeys, runsApi, type Run, type RunMode, type RunStatus, type RunSummary } from '../../api/runs';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Badge, Icon, Meter, cx } from '../../components/ui';
import { Timestamp } from '../../components/ui/Time';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { runProgress } from '../dashboard/model';
import { useNow } from '../missionControl/useNow';
import { WORKFLOW_STAGES, durationParts, isRunFinished, workflowDurationSeconds, workflowStage } from './workflowModel';

export const WORKSPACE_RUNS_PAGE_SIZE = 20;
/** Проекты для фильтра: одна страница (максимум контракта — 200). */
const PROJECTS_FILTER_LIMIT = 200;
/** getRun (task_counts, pending_approvals) — для первых N загруженных процессов (N+1 ограничен). */
export const WORKFLOW_DETAILS_LIMIT = 20;

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

export const RUN_MODES = [
  'CREATE',
  'DEVELOP',
  'DEBUG',
  'AUDIT',
  'MODERNIZE',
  'SECURITY',
  'OPTIMIZE',
  'TEST',
  'MAINTAIN',
  'DOCUMENT',
  'REVIEW',
] as const satisfies readonly RunMode[];

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

export function modeForFilter(value: string | null): RunMode | undefined {
  return value && (RUN_MODES as readonly string[]).includes(value) ? (value as RunMode) : undefined;
}

function Duration({ run, now }: { run: RunSummary; now: number }) {
  const { t } = useTranslation();
  const seconds = workflowDurationSeconds(run, now);
  if (seconds === null) return <>{t('runs.workflows.notStarted')}</>;
  const { h, m, s } = durationParts(seconds);
  const text = s === null ? t('runs.workflows.durationHm', { h, m }) : m > 0 ? t('runs.workflows.durationMs', { m, s }) : t('runs.workflows.durationS', { s });
  return <span className="tabular-nums">{run.finished_at ? text : t('runs.workflows.durationRunning', { duration: text })}</span>;
}

function StageTrack({ run }: { run: RunSummary }) {
  const { t } = useTranslation();
  const stage = workflowStage(run);
  const label = t(`enums.run_status.${stage.stage}`);
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid="workflow-stage" data-stage={stage.stage} data-special={stage.special || undefined}>
      <p className="text-xs text-fg-muted">
        {stage.special
          ? t('runs.workflows.stageSpecial', { stage: label, n: stage.index + 1, total: WORKFLOW_STAGES.length })
          : t('runs.workflows.stage', { stage: label, n: stage.index + 1, total: WORKFLOW_STAGES.length })}
      </p>
      <ol aria-hidden="true" className="flex gap-1">
        {WORKFLOW_STAGES.map((s, i) => (
          <li
            key={s}
            className={cx(
              'h-1.5 flex-1 rounded-full',
              i < stage.index || (i === stage.index && run.status === 'COMPLETED')
                ? 'bg-success'
                : i === stage.index
                  ? stage.special
                    ? 'bg-warning'
                    : 'bg-accent'
                  : 'bg-surface-muted ring-1 ring-inset ring-border',
            )}
          />
        ))}
      </ol>
    </div>
  );
}

function Progress({ workspaceId, run, detail }: { workspaceId: string; run: RunSummary; detail: UseQueryResult<Run> | undefined }) {
  const { t } = useTranslation();
  if (!detail) return <p className="text-xs text-fg-muted">{t('runs.workflows.progressInMissionControl')}</p>;
  if (detail.isPending) return <div aria-hidden="true" className="skeleton h-6 w-full" />;
  if (detail.isError) return <p className="text-xs text-danger-text">{t('runs.workflows.detailsError')}</p>;
  const p = runProgress(detail.data);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      {p ? (
        <>
          <Meter
            testId="workflow-progress"
            label={t('runs.workflows.progress')}
            ratio={p.ratio}
            valueText={t('runs.workflows.progressValue', { verified: p.verified, total: p.total })}
            tone="success"
          />
          <p className="text-xs text-fg-muted">{t('runs.workflows.progressHint', { inProgress: p.inProgress, failed: p.failed, blocked: p.blocked })}</p>
        </>
      ) : (
        <p className="text-xs text-fg-muted">{t('runs.workflows.noPlan')}</p>
      )}
      {detail.data.pending_approvals ? (
        <Link
          to={`/w/${workspaceId}/approvals?run_id=${encodeURIComponent(run.id)}`}
          className="inline-flex w-fit items-center gap-1 rounded-full border border-warning-border bg-warning-soft px-2 py-0.5 text-xs font-medium text-warning-fg"
        >
          <Icon name="approval" className="h-3.5 w-3.5" />
          {t('runs.workflows.pendingApprovals', { count: detail.data.pending_approvals })}
        </Link>
      ) : null}
    </div>
  );
}

function RunItem({
  run,
  workspaceId,
  projectName,
  canQuality,
  detail,
  now,
}: {
  run: RunSummary;
  workspaceId: string;
  projectName?: string;
  canQuality: boolean;
  detail: UseQueryResult<Run> | undefined;
  now: number;
}) {
  const { t } = useTranslation();
  const base = `/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}`;
  const label = run.objective.slice(0, 80);
  const links = [
    { to: `/w/${workspaceId}/mission-control?run=${encodeURIComponent(run.id)}`, text: t('runs.nav.missionControl') },
    { to: base, text: t('runs.open') },
    { to: `${base}/graph`, text: t('runs.nav.graph') },
    { to: `${base}/chat`, text: t('runs.nav.chat') },
    ...(canQuality ? [{ to: `/w/${workspaceId}/quality/runs/${run.id}`, text: t('runs.nav.quality') }] : []),
  ];
  return (
    <li
      className="flex min-w-0 flex-col gap-3 rounded-xl border border-border bg-surface p-4 shadow-e1 transition-colors duration-instant hover:border-border-strong focus-within:border-border-active"
      data-run-id={run.id}
      data-status={run.status}
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusBadge kind="run" status={run.status} />
        <Badge tone="neutral" icon="workflow">
          {t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}
        </Badge>
        {projectName ? (
          <Link to={`/w/${workspaceId}/projects/${run.project_id}/runs`} className="text-link underline">
            {projectName}
          </Link>
        ) : null}
        <span className="text-fg-muted">
          {t('runs.createdAt')}: <Timestamp value={run.created_at} />
        </span>
      </div>
      <p className="line-clamp-3 whitespace-pre-line break-words font-medium text-fg">{run.objective}</p>
      {run.blocked_reason ? (
        <p className="flex items-start gap-2 rounded-md border border-warning-border bg-warning-soft px-3 py-2 text-sm text-warning-fg" data-testid="workflow-blocked">
          <Icon name="alert" className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {t('runs.workflows.blockedReason')}: {t(`enums.blocked_reason.${run.blocked_reason}`, { defaultValue: run.blocked_reason })}
          </span>
        </p>
      ) : null}
      <div className="grid min-w-0 gap-3 md:grid-cols-[minmax(0,2fr),minmax(0,3fr),auto] md:items-start">
        <StageTrack run={run} />
        <Progress workspaceId={workspaceId} run={run} detail={detail} />
        <p className="text-xs text-fg-muted md:text-right" data-testid="workflow-duration">
          {t('runs.workflows.duration')}: <Duration run={run} now={now} />
        </p>
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label={t('runs.all.linksLabel', { objective: label })}>
        {links.map((l, i) => (
          <li key={l.to}>
            <Link to={l.to} className={cx('inline-flex min-h-target items-center gap-1 font-medium text-link underline', i === 0 && 'font-semibold')}>
              {i === 0 ? <Icon name="missionControl" className="h-4 w-4" /> : null}
              {l.text}
              <span className="sr-only">: {label}</span>
            </Link>
          </li>
        ))}
      </ul>
    </li>
  );
}

/** Справка: конвейер ролей по умолчанию для режима (AGENT_ROLES §4, документация; фактический план — в Mission Control). */
function PipelinesReference({ mode }: { mode: RunMode | undefined }) {
  const { t } = useTranslation();
  const modes = mode ? [mode] : RUN_MODES;
  return (
    <details className="mb-6 rounded-lg border border-border bg-surface" data-testid="pipelines-reference">
      <summary className="min-h-touch cursor-pointer px-4 py-3 text-sm font-medium text-fg">
        {mode ? t('runs.workflows.pipelinesFor', { mode: t(`enums.run_mode.${mode}`) }) : t('runs.workflows.pipelines')}
      </summary>
      <div className="flex flex-col gap-2 px-4 pb-4">
        <p className="text-xs text-fg-subtle">{t('runs.workflows.pipelinesSource')}</p>
        <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[minmax(8rem,auto),1fr]">
          {modes.map((m) => (
            <div key={m} className="contents">
              <dt className="font-medium text-fg">{t(`enums.run_mode.${m}`)}</dt>
              <dd className="min-w-0 break-words text-fg-muted">{t(`runs.workflows.pipeline.${m}`)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </details>
  );
}

/**
 * Процессы (Workflows, F5): запуски workspace (GET /runs) — режим, статус, этап FSM, прогресс только по
 * подтверждённым задачам (getRun.task_counts, SUCCEEDED), длительность, причина блокировки, ожидающие
 * одобрения, переходы в Mission Control. Фильтры статуса/режима/проекта в URL, cursor-пагинация.
 */
export function WorkspaceRunsPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const statusParam = params.get('status');
  const statuses = statusesForFilter(statusParam);
  const statusValue = statuses ? (statusParam ?? '') : '';
  const mode = modeForFilter(params.get('mode'));
  const projectId = params.get('project_id') ?? '';
  const enabled = Boolean(workspaceId && membership);

  const projects = useQuery({
    queryKey: ['projects', workspaceId ?? '', 'filter'],
    queryFn: ({ signal }) => projectsApi.list(workspaceId ?? '', { limit: PROJECTS_FILTER_LIMIT }, signal),
    enabled,
  });
  const projectNames = useMemo(() => new Map((projects.data?.items ?? []).map((p) => [p.id, p.name])), [projects.data]);

  const runs = useInfiniteQuery({
    queryKey: [...runKeys.list(workspaceId ?? '', projectId || undefined), { status: statusValue, mode: mode ?? '' }],
    queryFn: ({ pageParam, signal }) =>
      runsApi.list(
        workspaceId ?? '',
        {
          limit: WORKSPACE_RUNS_PAGE_SIZE,
          ...(projectId ? { project_id: projectId } : {}),
          ...(statuses ? { status: statuses } : {}),
          ...(mode ? { mode } : {}),
          ...(pageParam ? { cursor: pageParam } : {}),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  const detailIds = items.slice(0, WORKFLOW_DETAILS_LIMIT).map((r) => r.id);
  const details = useQueries({
    queries: detailIds.map((id) => ({
      queryKey: runKeys.detail(id),
      queryFn: ({ signal }: { signal: AbortSignal }) => runsApi.get(id, signal),
      staleTime: 15_000,
    })),
  });
  const now = useNow(items.some((r) => !isRunFinished(r.status) && r.started_at));

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const canQuality = can(membership.role, 'quality:read');
  const filtered = Boolean(statuses || projectId || mode);
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
      <div role="group" aria-label={t('runs.all.filters')} className="card mb-4 flex flex-wrap items-end gap-4">
        <div className="flex min-w-0 flex-col gap-1">
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
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="runs-filter-mode" className="text-sm font-medium text-fg">
            {t('runs.mode')}
          </label>
          <select id="runs-filter-mode" className="input" value={mode ?? ''} onChange={(e) => update('mode', e.target.value)}>
            <option value="">{t('runs.workflows.anyMode')}</option>
            {RUN_MODES.map((m) => (
              <option key={m} value={m}>
                {t(`enums.run_mode.${m}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex min-w-0 max-w-full flex-col gap-1">
          <label htmlFor="runs-filter-project" className="text-sm font-medium text-fg">
            {t('runs.all.project')}
          </label>
          <select id="runs-filter-project" className="input max-w-full" value={projectId} onChange={(e) => update('project_id', e.target.value)}>
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
      <PipelinesReference mode={mode} />
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
            {items.map((r, i) => (
              <RunItem
                key={r.id}
                run={r}
                workspaceId={workspaceId}
                projectName={projectNames.get(r.project_id)}
                canQuality={canQuality}
                detail={i < WORKFLOW_DETAILS_LIMIT ? details[i] : undefined}
                now={now}
              />
            ))}
          </ul>
          <LoadMore
            shown={items.length}
            hasNextPage={runs.hasNextPage}
            isFetchingNextPage={runs.isFetchingNextPage}
            nextPageError={runs.isFetchNextPageError ? runs.error : null}
            onLoadMore={() => void runs.fetchNextPage()}
          />
          <p className="mt-2 text-xs text-fg-subtle">{t('runs.workflows.source', { limit: WORKFLOW_DETAILS_LIMIT })}</p>
        </>
      )}
    </section>
  );
}
