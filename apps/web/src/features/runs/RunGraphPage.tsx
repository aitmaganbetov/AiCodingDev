/**
 * P7-05 Run Graph: DAG задач run (@xyflow/react, лениво), живые статусы по SSE, текущий
 * исполнитель (роль агента · CLI-провайдер) и доступная альтернатива — таблица того же графа.
 */
import { useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { runKeys, type TaskSummary } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { useRunEvents } from '../../api/useRunEvents';
import { StatusBadge } from '../../components/StatusBadge';
import { EmptyState, ErrorState, LoadingState, PageHeading, Table, type Column } from '../../components/ui';
import { cx } from '../../components/ui';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { enumLabel, executorLabel, providerLabel, roleLabel, TASK_ACTIVE } from './labels';
import { RunNav } from './RunNav';
import { StreamStatus } from './StreamStatus';
import { applyTaskEvents, useRunTasks } from './useRunTasks';
import { useInPageSearchParams } from './useInPageSearchParams';
import { useRunDetail } from './useRunStatus';

const TaskGraphCanvas = lazy(() => import('./TaskGraphCanvas'));

type View = 'graph' | 'list';
const RUN_EVENTS = new Set(['run.state_changed', 'run.completed']);
const TASK_EVENTS = new Set(['task.state_changed', 'gate.completed']);

function TaskTable({ tasks, taskHref }: { tasks: readonly TaskSummary[]; taskHref: (id: string) => string }) {
  const { t } = useTranslation();
  const titles = useMemo(() => new Map(tasks.map((x) => [x.id, x.title])), [tasks]);
  const columns: Column<TaskSummary>[] = [
    {
      id: 'title',
      header: t('run.col.title'),
      rowHeader: true,
      sortValue: (x) => x.title,
      cell: (x) => (
        <Link to={taskHref(x.id)} className="link">
          {x.title}
        </Link>
      ),
    },
    { id: 'role', header: t('run.col.role'), sortValue: (x) => roleLabel(t, x.agent_role), cell: (x) => roleLabel(t, x.agent_role) },
    {
      id: 'status',
      header: t('run.col.status'),
      sortValue: (x) => x.status,
      cell: (x) => (
        <>
          <StatusBadge kind="task" status={x.status} />
          {x.blocked_reason ? <span className="mt-1 block text-xs text-fg-muted">{enumLabel(t, 'blocked_reason', x.blocked_reason)}</span> : null}
        </>
      ),
    },
    {
      id: 'executor',
      header: t('runGraph.col.executor'),
      sortValue: (x) => providerLabel(t, x.current_provider),
      cell: (x) =>
        TASK_ACTIVE.includes(x.status) ? (
          <span className="font-medium">{executorLabel(t, x.agent_role, x.current_provider)}</span>
        ) : x.current_provider ? (
          providerLabel(t, x.current_provider)
        ) : (
          t('runGraph.noExecutor')
        ),
    },
    {
      id: 'attempts',
      header: t('run.col.attempts'),
      sortValue: (x) => x.attempt_count,
      cell: (x) => t('run.attempts', { count: x.attempt_count, max: x.max_attempts }),
    },
    {
      id: 'deps',
      header: t('run.col.dependsOn'),
      cell: (x) =>
        x.depends_on.length > 0
          ? x.depends_on.map((id) => titles.get(id) ?? t('run.unknownTask', { id: id.slice(0, 8) })).join(', ')
          : t('run.noDependencies'),
    },
  ];
  return (
    <Table
      caption={t('runGraph.tableCaption')}
      columns={columns}
      rows={tasks}
      rowKey={(x) => x.id}
      rowProps={(x) => ({ 'data-task-id': x.id, 'data-status': x.status })}
      minWidthClass="min-w-[48rem]"
    />
  );
}

/** Кто сейчас работает: активные задачи с ролью агента и CLI-провайдером. */
function CurrentExecutors({ tasks, taskHref }: { tasks: readonly TaskSummary[]; taskHref: (id: string) => string }) {
  const { t } = useTranslation();
  const active = tasks.filter((x) => TASK_ACTIVE.includes(x.status));
  return (
    <section aria-labelledby="executors-title" className="card mb-4">
      <h2 id="executors-title" className="mb-2 text-base font-semibold">
        {t('runGraph.executors')}
      </h2>
      {active.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('runGraph.executorsEmpty')}</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm" data-testid="current-executors">
          {active.map((x) => (
            <li key={x.id} className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{executorLabel(t, x.agent_role, x.current_provider)}</span>
              <span aria-hidden="true">—</span>
              <Link to={taskHref(x.id)} className="link">
                {x.title}
              </Link>
              <StatusBadge kind="task" status={x.status} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function RunGraphPage() {
  const { t } = useTranslation();
  const { projectId = '', runId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const queryClient = useQueryClient();
  const [params, setParams] = useInPageSearchParams();
  const view: View = params.get('view') === 'list' ? 'list' : 'graph';
  const { run, isTerminal } = useRunDetail(runId, Boolean(membership));
  const { query, tasks, edges, planVersion } = useRunTasks(runId, { all: true, enabled: Boolean(membership) });
  const [announcement, setAnnouncement] = useState('');

  const onEvents = useCallback(
    (events: RunEvent[]) => {
      applyTaskEvents(queryClient, runId, events);
      const last = [...events].reverse().find((e) => e.type === 'task.state_changed');
      if (last) {
        const p = last.payload;
        setAnnouncement(
          t('event.taskTransition', {
            task: typeof p.title === 'string' ? p.title : roleLabel(t, p.agent_role),
            from: enumLabel(t, 'task_status', p.from_status),
            to: enumLabel(t, 'task_status', p.to_status),
          }),
        );
      }
      if (events.some((e) => TASK_EVENTS.has(e.type))) void queryClient.invalidateQueries({ queryKey: runKeys.tasks(runId) });
      if (events.some((e) => RUN_EVENTS.has(e.type) || TASK_EVENTS.has(e.type)))
        void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId), exact: true });
    },
    [queryClient, runId, t],
  );
  const onResync = useCallback(() => void queryClient.invalidateQueries({ queryKey: runKeys.tasks(runId) }), [queryClient, runId]);
  const stream = useRunEvents(runId, { onEvents, onResync, isTerminal, limit: 0 });

  const taskHref = useCallback(
    (taskId: string) => `/w/${workspaceId ?? ''}/projects/${projectId}/runs/${runId}/tasks/${taskId}`,
    [workspaceId, projectId, runId],
  );

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const setView = (v: View) => {
    const next = new URLSearchParams(params);
    if (v === 'graph') next.delete('view');
    else next.set('view', v);
    setParams(next);
  };

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('runGraph.title')}>
        <Link to={`/w/${workspaceId}/projects/${projectId}/runs`} className="btn-secondary">
          {t('runs.backToRuns')}
        </Link>
      </PageHeading>
      <RunNav workspaceId={workspaceId} projectId={projectId} runId={runId} />
      {run.isError ? (
        <ErrorState error={run.error} onRetry={() => void run.refetch()} />
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              {run.data ? (
                <>
                  <span className="text-fg-muted">{t('run.status')}:</span>
                  <StatusBadge kind="run" status={run.data.status} />
                </>
              ) : null}
              {planVersion > 0 ? <span className="text-fg-muted">{t('runGraph.planVersion', { version: planVersion })}</span> : null}
            </div>
            <StreamStatus state={stream.state} error={stream.error} onReconnect={stream.reconnect} />
          </div>
          <p className="sr-only" role="status" aria-live="polite" data-testid="graph-announcement">
            {announcement}
          </p>
          {query.isPending ? (
            <LoadingState label={t('run.tasksLoading')} />
          ) : query.isError && tasks.length === 0 ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : tasks.length === 0 ? (
            <EmptyState icon="layers" title={t('runGraph.emptyTitle')} description={t('run.tasksEmpty')} />
          ) : (
            <>
              <CurrentExecutors tasks={tasks} taskHref={taskHref} />
              <div role="group" aria-label={t('runGraph.viewLabel')} className="mb-3 inline-flex rounded-md border border-border">
                {(['graph', 'list'] as const).map((v) => (
                  <button
                    key={v}
                    type="button"
                    aria-pressed={view === v}
                    onClick={() => setView(v)}
                    className={cx(
                      'min-h-touch px-4 text-sm font-medium first:rounded-l-md last:rounded-r-md',
                      view === v ? 'bg-accent text-accent-fg' : 'bg-surface text-fg hover:bg-surface-muted',
                    )}
                  >
                    {t(`runGraph.view.${v}`)}
                  </button>
                ))}
              </div>
              {view === 'graph' ? (
                <>
                  <p className="mb-2 text-sm text-fg-muted" id="graph-hint">
                    {t('runGraph.hint', { tasks: tasks.length, edges: edges.length })}
                  </p>
                  <Suspense fallback={<LoadingState label={t('runGraph.loadingCanvas')} />}>
                    <TaskGraphCanvas tasks={tasks} edges={edges} taskHref={taskHref} label={t('runGraph.canvasLabel')} />
                  </Suspense>
                </>
              ) : (
                <TaskTable tasks={tasks} taskHref={taskHref} />
              )}
              {query.isFetchingNextPage ? <LoadingState inline label={t('runGraph.loadingMore')} /> : null}
              {query.isFetchNextPageError ? (
                <ErrorState error={query.error} onRetry={() => void query.fetchNextPage()} />
              ) : null}
            </>
          )}
        </>
      )}
    </section>
  );
}

export default RunGraphPage;
