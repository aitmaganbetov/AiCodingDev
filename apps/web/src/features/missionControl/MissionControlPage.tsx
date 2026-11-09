import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link, Navigate } from 'react-router';
import { isApiError } from '../../api/client';
import { runKeys, runsApi, type RunStatus, type RunSummary } from '../../api/runs';
import { PageHeading } from '../../components/PageHeading';
import { StatusBadge } from '../../components/StatusBadge';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { AgentBadge } from '../../components/ui/AgentBadge';
import { Callout } from '../../components/ui/Badge';
import { ButtonLink } from '../../components/ui/Button';
import { ResizableGroup, ResizableHandle, ResizablePanel, useResizableGroup } from '../../components/ui/Resizable';
import { Skeleton } from '../../components/ui/Skeleton';
import { RelativeTime } from '../../components/ui/Time';
import { cx } from '../../components/ui/cx';
import { ContextPanel } from '../../layout/ContextPanel';
import { useMediaQuery } from '../../lib/layoutPrefs';
import { useCurrentWorkspace } from '../../workspace';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';

/** Активные статусы run (не терминальные). */
export const ACTIVE_RUN_STATUSES: RunStatus[] = ['CREATED', 'DISCOVERY', 'PLANNED', 'AWAITING_APPROVAL', 'RUNNING', 'VERIFYING', 'PAUSED', 'BLOCKED'];
export const MISSION_CONTROL_RUNS_LIMIT = 20;

/** `?run=<id>`: переход к run по одному id (уведомления/поиск не знают project_id). */
function RunResolver({ workspaceId, runId }: { workspaceId: string; runId: string }) {
  const { t } = useTranslation();
  const run = useQuery({ queryKey: runKeys.detail(runId), queryFn: ({ signal }) => runsApi.get(runId, signal), retry: false });
  if (run.isPending) return <LoadingState label={t('missionControl.resolving')} />;
  if (run.isError) {
    return (
      <>
        <PageHeading title={t('nav.missionControl')} />
        <ErrorState
          error={run.error}
          title={isApiError(run.error) && run.error.status === 404 ? t('missionControl.runNotFound') : t('missionControl.resolveError')}
          onRetry={() => void run.refetch()}
        />
      </>
    );
  }
  return <Navigate replace to={`/w/${workspaceId}/projects/${run.data.project_id}/runs/${run.data.id}`} />;
}

function RunLinks({ workspaceId, run }: { workspaceId: string; run: RunSummary }) {
  const { t } = useTranslation();
  const base = `/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}`;
  return (
    <div className="flex flex-wrap gap-2">
      <ButtonLink size="sm" variant="primary" to={base}>
        {t('missionControl.openRun')}
        <span className="sr-only">: {run.objective.slice(0, 80)}</span>
      </ButtonLink>
      <ButtonLink size="sm" to={`${base}/graph`}>
        {t('missionControl.openGraph')}
      </ButtonLink>
      <ButtonLink size="sm" to={`${base}/chat`}>
        {t('missionControl.openChat')}
      </ButtonLink>
    </div>
  );
}

function RunsPanel({
  runs,
  selected,
  onSelect,
}: {
  runs: RunSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="mc-runs" className="flex h-full min-h-0 flex-col">
      <h2 id="mc-runs" className="mb-2 text-base font-semibold text-fg">
        {t('missionControl.activeRuns', { count: runs.length })}
      </h2>
      <ul className="flex min-h-0 flex-col gap-2 overflow-y-auto pr-1" aria-label={t('missionControl.activeRunsList')}>
        {runs.map((r) => {
          const isSelected = r.id === selected;
          return (
            <li key={r.id} data-run-id={r.id}>
              <button
                type="button"
                aria-pressed={isSelected}
                onClick={() => onSelect(r.id)}
                className={cx(
                  'flex w-full flex-col items-start gap-1 rounded-lg border p-3 text-left transition-colors duration-instant',
                  isSelected ? 'border-border-active bg-accent-soft/40' : 'border-border bg-surface hover:bg-surface-muted',
                )}
              >
                <span className="flex flex-wrap items-center gap-2">
                  <StatusBadge kind="run" status={r.status} />
                  <span className="text-xs text-fg-muted">{t(`enums.run_mode.${r.mode}`, { defaultValue: r.mode })}</span>
                </span>
                <span className="line-clamp-2 break-words text-sm font-medium text-fg">{r.objective}</span>
                <span className="text-xs text-fg-muted">
                  {t('missionControl.created')} <RelativeTime value={r.created_at} />
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function TasksPanel({ workspaceId, run }: { workspaceId: string; run: RunSummary | null }) {
  const { t } = useTranslation();
  const tasks = useQuery({
    queryKey: runKeys.tasks(run?.id ?? ''),
    queryFn: ({ signal }) => runsApi.tasks(run?.id ?? '', {}, signal),
    enabled: Boolean(run),
  });
  if (!run) {
    return (
      <section aria-labelledby="mc-tasks" className="flex h-full flex-col">
        <h2 id="mc-tasks" className="mb-2 text-base font-semibold text-fg">
          {t('missionControl.tasks')}
        </h2>
        <p className="text-sm text-fg-muted">{t('missionControl.selectRun')}</p>
      </section>
    );
  }
  return (
    <section aria-labelledby="mc-tasks" className="flex h-full min-h-0 flex-col" aria-busy={tasks.isFetching}>
      <h2 id="mc-tasks" className="mb-2 text-base font-semibold text-fg">
        {t('missionControl.tasksOf', { objective: run.objective.slice(0, 60) })}
      </h2>
      {tasks.isPending ? (
        <Skeleton lines={4} />
      ) : tasks.isError ? (
        <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} title={t('missionControl.tasksError')} />
      ) : tasks.data.tasks.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('missionControl.noTasks')}</p>
      ) : (
        <ul className="flex min-h-0 flex-col divide-y divide-border overflow-y-auto rounded-lg border border-border bg-surface">
          {tasks.data.tasks.map((task) => (
            <li key={task.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
              <AgentBadge agentRole={task.agent_role} showName={false} />
              <Link to={`/w/${workspaceId}/projects/${run.project_id}/runs/${run.id}/tasks/${task.id}`} className="min-w-0 flex-1 break-words text-sm font-medium">
                {task.title}
              </Link>
              <StatusBadge kind="task" status={task.status} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ResetPanels() {
  const { t } = useTranslation();
  const { reset } = useResizableGroup();
  return (
    <button type="button" className="btn-ghost btn-sm" onClick={reset}>
      {t('missionControl.resetPanels')}
    </button>
  );
}

function RunContext({ workspaceId, run }: { workspaceId: string; run: RunSummary }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-3 text-sm" data-testid="mc-context">
      <StatusBadge kind="run" status={run.status} />
      <p className="break-words font-medium text-fg">{run.objective}</p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-fg-muted">
        <dt>{t('missionControl.mode')}</dt>
        <dd className="text-fg">{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</dd>
        <dt>{t('missionControl.created')}</dt>
        <dd className="text-fg">
          <RelativeTime value={run.created_at} />
        </dd>
        {run.started_at ? (
          <>
            <dt>{t('missionControl.started')}</dt>
            <dd className="text-fg">
              <RelativeTime value={run.started_at} />
            </dd>
          </>
        ) : null}
        <dt>{t('missionControl.requestedBy')}</dt>
        <dd className="break-words text-fg">{run.requested_by.display_name}</dd>
      </dl>
      <RunLinks workspaceId={workspaceId} run={run} />
    </div>
  );
}

/**
 * AI Mission Control (F2): выбор активного run. Split view (≥ 1024 px, размеры сохраняются,
 * клавиатура/кнопка сброса) — список активных runs и задачи выбранного; сводка — в контекстной
 * панели оболочки. Объединённый экран (DAG, лента, инспектор) — фаза F4 (плашка на экране).
 */
export default function MissionControlPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const [params, setParams] = useInPageSearchParams();
  const resolveRun = params.get('run');
  const selectedId = params.get('selected');
  const split = useMediaQuery('(min-width: 1024px)', true);

  const runs = useQuery({
    queryKey: [...runKeys.list(workspaceId ?? ''), 'mission-control'] as const,
    queryFn: ({ signal }) => runsApi.list(workspaceId ?? '', { status: ACTIVE_RUN_STATUSES, limit: MISSION_CONTROL_RUNS_LIMIT }, signal),
    enabled: Boolean(workspaceId && !resolveRun),
  });

  if (!workspaceId) return null;
  if (resolveRun) return <RunResolver workspaceId={workspaceId} runId={resolveRun} />;

  const items = runs.data?.items ?? [];
  const selected = items.find((r) => r.id === selectedId) ?? null;
  const select = (id: string) => {
    const next = new URLSearchParams(params);
    next.set('selected', id);
    setParams(next);
  };

  let body;
  if (runs.isPending) body = <LoadingState label={t('missionControl.loading')} />;
  else if (runs.isError) body = <ErrorState error={runs.error} onRetry={() => void runs.refetch()} title={t('missionControl.error')} />;
  else if (items.length === 0)
    body = (
      <EmptyState
        icon="missionControl"
        title={t('missionControl.emptyTitle')}
        description={t('missionControl.emptyDescription')}
        action={
          <ButtonLink to={`/w/${workspaceId}/runs`} icon="workflow">
            {t('missionControl.toRuns')}
          </ButtonLink>
        }
      />
    );
  else if (split)
    body = (
      <div className="h-[min(70vh,44rem)] rounded-lg border border-border bg-canvas-secondary p-3" data-testid="mc-split">
        <ResizableGroup id="mission-control.chooser" defaultLayout={{ 'mc-runs': 40, 'mc-tasks': 60 }}>
          <ResizablePanel id="mc-runs" defaultSize="40" minSize="25">
            <div className="h-full pr-2">
              <RunsPanel runs={items} selected={selected?.id ?? null} onSelect={select} />
            </div>
          </ResizablePanel>
          <ResizableHandle before={t('missionControl.panelRuns')} after={t('missionControl.panelTasks')} />
          <ResizablePanel id="mc-tasks" defaultSize="60" minSize="30">
            <div className="flex h-full flex-col gap-2 pl-2">
              <div className="flex justify-end">
                <ResetPanels />
              </div>
              <div className="min-h-0 flex-1">
                <TasksPanel workspaceId={workspaceId} run={selected} />
              </div>
            </div>
          </ResizablePanel>
        </ResizableGroup>
      </div>
    );
  else
    body = (
      <div className="flex flex-col gap-6">
        <RunsPanel runs={items} selected={selected?.id ?? null} onSelect={select} />
        <TasksPanel workspaceId={workspaceId} run={selected} />
      </div>
    );

  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4">
      <PageHeading title={t('nav.missionControl')}>
        <ButtonLink to={`/w/${workspaceId}/runs`} icon="workflow">
          {t('missionControl.allRuns')}
        </ButtonLink>
      </PageHeading>
      <Callout tone="info" title={t('missionControl.phaseTitle')}>
        {t('missionControl.phaseNote')}
      </Callout>
      {runs.data?.next_cursor ? <p className="text-sm text-fg-muted">{t('missionControl.sample', { count: MISSION_CONTROL_RUNS_LIMIT })}</p> : null}
      {body}
      <ContextPanel id="mission-control" title={t('missionControl.context.title')}>
        {selected ? <RunContext workspaceId={workspaceId} run={selected} /> : null}
      </ContextPanel>
    </section>
  );
}
