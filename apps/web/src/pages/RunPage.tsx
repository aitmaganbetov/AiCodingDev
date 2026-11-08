import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { TFunction } from 'i18next';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { IdempotentIntent } from '../api/client';
import { runKeys, runsApi, type Run, type RunCommand, type TaskSummary } from '../api/runs';
import type { RunEvent } from '../api/runEvents';
import { FEED_LIMIT, useRunEvents } from '../api/useRunEvents';
import { LoadMore } from '../components/LoadMore';
import { PageHeading } from '../components/PageHeading';
import { ErrorState, LoadingState } from '../components/states';
import { RUN_PAUSABLE, RUN_TERMINAL, StatusBadge } from '../components/StatusBadge';
import { formatDate } from '../lib/format';
import { can } from '../lib/permissions';
import { problemMessage } from '../lib/problem';
import { useCurrentWorkspace } from '../workspace';
import { WorkspaceNotFound } from './NotFoundPage';

export const TASKS_PAGE_SIZE = 200;

/** События, после которых состояние run/задач в REST могло измениться. */
const RUN_STATE_EVENTS = new Set(['run.state_changed', 'run.completed', 'approval.requested', 'approval.decided']);
const TASK_STATE_EVENTS = new Set(['task.state_changed', 'gate.completed']);

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : typeof v === 'number' ? String(v) : '';
}

function enumLabel(t: TFunction, group: string, value: unknown): string {
  const s = str(value);
  return s ? t(`enums.${group}.${s}`, { defaultValue: s }) : '';
}

/** Однострочное описание события ленты; неизвестный тип — только его название. */
export function describeEvent(t: TFunction, e: RunEvent, taskTitle: (id: string) => string | undefined): string {
  const p = e.payload;
  switch (e.type) {
    case 'run.state_changed': {
      const text = t('event.transition', {
        from: enumLabel(t, 'run_status', p.from_status),
        to: enumLabel(t, 'run_status', p.to_status),
      });
      return p.blocked_reason ? `${text} (${enumLabel(t, 'blocked_reason', p.blocked_reason)})` : text;
    }
    case 'task.state_changed': {
      const text = t('event.taskTransition', {
        task: str(p.title) || taskTitle(str(p.task_id)) || enumLabel(t, 'agent_role', p.agent_role),
        from: enumLabel(t, 'task_status', p.from_status),
        to: enumLabel(t, 'task_status', p.to_status),
      });
      return p.blocked_reason ? `${text} (${enumLabel(t, 'blocked_reason', p.blocked_reason)})` : text;
    }
    case 'agent.message':
      // from/to — ACP-участники {role, provider?} (sse-event.schema.json).
      return t('event.agentMessage', {
        from: enumLabel(t, 'agent_role', isRecord(p.from) ? p.from.role : p.from),
        to: enumLabel(t, 'agent_role', isRecord(p.to) ? p.to.role : p.to),
        summary: str(p.summary),
      });
    case 'cli.output':
      return [str(p.provider), enumLabel(t, 'agent_role', p.agent_role), str(p.text) || str(p.cli_event_type)]
        .filter(Boolean)
        .join(' · ');
    case 'approval.requested':
      return str(p.summary);
    case 'approval.decided':
      return t('event.approvalDecided', { action: str(p.action_type), status: str(p.status) });
    case 'gate.completed':
      return t('event.gate', { gate: str(p.gate_type), status: str(p.status) });
    case 'run.completed':
      return t('event.runCompleted', {
        status: enumLabel(t, 'run_status', p.final_status),
        passed: str(p.required_gates_passed),
        total: str(p.required_gates_total),
      });
    case 'directive.state_changed':
      return t('event.transition', { from: str(p.from_status), to: str(p.to_status) });
    case 'directive.impact_ready':
      return str(p.summary);
    case 'chat.message':
      return str(p.body);
    default:
      return '';
  }
}

function RunHeader({ run }: { run: Run }) {
  const { t, i18n } = useTranslation();
  const counts = Object.entries(run.task_counts ?? {}).filter(([, n]) => n > 0);
  return (
    <div className="card mb-6 flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm text-fg-muted">{t('run.status')}:</span>
        <StatusBadge kind="run" status={run.status} />
      </div>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto,1fr]">
        <dt className="text-fg-muted">{t('run.objective')}</dt>
        <dd className="whitespace-pre-line break-words">{run.objective}</dd>
        <dt className="text-fg-muted">{t('run.mode')}</dt>
        <dd>{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</dd>
        {run.blocked_reason ? (
          <>
            <dt className="text-fg-muted">{t('run.blockedReason')}</dt>
            <dd data-testid="run-blocked-reason">{enumLabel(t, 'blocked_reason', run.blocked_reason)}</dd>
          </>
        ) : null}
        <dt className="text-fg-muted">{t('run.pendingApprovals')}</dt>
        <dd data-testid="run-pending-approvals">{run.pending_approvals ?? 0}</dd>
        {counts.length > 0 ? (
          <>
            <dt className="text-fg-muted">{t('run.taskCounts')}</dt>
            <dd>{counts.map(([s, n]) => `${enumLabel(t, 'task_status', s)}: ${n}`).join(', ')}</dd>
          </>
        ) : null}
        <dt className="text-fg-muted">{t('run.requestedBy')}</dt>
        <dd>{run.requested_by.display_name}</dd>
        <dt className="text-fg-muted">{t('run.createdAt')}</dt>
        <dd>{formatDate(run.created_at, i18n.language)}</dd>
      </dl>
    </div>
  );
}

function RunControls({ run }: { run: Run }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [accepted, setAccepted] = useState<RunCommand | null>(null);
  const command = useMutation({
    mutationFn: (cmd: RunCommand) =>
      runsApi.command(run.id, cmd, intent.current.keyFor({ run: run.id, cmd, version: run.version }), run.version),
    onSuccess: (_res, cmd) => {
      intent.current.complete();
      setAccepted(cmd);
      setConfirmCancel(false);
    },
    // 409/412: состояние ушло вперёд — перечитать.
    onSettled: () => void queryClient.invalidateQueries({ queryKey: runKeys.detail(run.id) }),
  });

  const terminal = RUN_TERMINAL.includes(run.status);
  const busy = command.isPending;
  return (
    <div role="group" aria-label={t('run.controls')} className="mb-6 flex flex-col gap-3">
      <div className="flex flex-wrap gap-3">
        {run.status === 'PAUSED' ? (
          <button type="button" className="btn-primary" disabled={busy} onClick={() => command.mutate('resume')}>
            {t('run.resume')}
          </button>
        ) : (
          <button
            type="button"
            className="btn-secondary"
            disabled={busy || !RUN_PAUSABLE.includes(run.status)}
            onClick={() => command.mutate('pause')}
          >
            {t('run.pause')}
          </button>
        )}
        {!confirmCancel ? (
          <button type="button" className="btn-secondary" disabled={busy || terminal} onClick={() => setConfirmCancel(true)}>
            {t('run.cancel')}
          </button>
        ) : null}
      </div>
      {confirmCancel && !terminal ? (
        <div className="flex flex-wrap items-center gap-3 rounded-md border border-warning-border bg-warning-soft p-3" role="alert">
          <p className="text-sm text-warning-fg">{t('run.cancelConfirm')}</p>
          <button type="button" className="btn-primary" disabled={busy} onClick={() => command.mutate('cancel')}>
            {t('run.cancelYes')}
          </button>
          <button type="button" className="btn-secondary" onClick={() => setConfirmCancel(false)}>
            {t('run.cancelNo')}
          </button>
        </div>
      ) : null}
      <p role="status" className="text-sm text-fg-muted">
        {busy ? t('run.commandSending') : accepted && !command.isError ? t(`run.commandAccepted.${accepted}`) : ''}
      </p>
      {command.isError ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {problemMessage(t, command.error)}
        </p>
      ) : null}
    </div>
  );
}

function TasksTable({ runId, tasks, planVersion }: { runId: string; tasks: TaskSummary[]; planVersion: number }) {
  const { t } = useTranslation();
  const titles = useMemo(() => new Map(tasks.map((x) => [x.id, x.title])), [tasks]);
  if (tasks.length === 0) return <p className="text-fg-muted">{t('run.tasksEmpty')}</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-surface">
      <table className="w-full min-w-[40rem] text-left text-sm" data-run-id={runId}>
        <caption className="sr-only">{t('run.tasksCaption', { version: planVersion })}</caption>
        <thead className="bg-surface-muted text-fg-muted">
          <tr>
            <th scope="col" className="px-3 py-2 font-semibold">{t('run.col.title')}</th>
            <th scope="col" className="px-3 py-2 font-semibold">{t('run.col.role')}</th>
            <th scope="col" className="px-3 py-2 font-semibold">{t('run.col.status')}</th>
            <th scope="col" className="px-3 py-2 font-semibold">{t('run.col.attempts')}</th>
            <th scope="col" className="px-3 py-2 font-semibold">{t('run.col.dependsOn')}</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id} className="border-t border-border align-top" data-task-id={task.id}>
              <th scope="row" className="px-3 py-2 font-medium">{task.title}</th>
              <td className="px-3 py-2">{enumLabel(t, 'agent_role', task.agent_role)}</td>
              <td className="px-3 py-2">
                <StatusBadge kind="task" status={task.status} />
                {task.blocked_reason ? (
                  <span className="mt-1 block text-xs text-fg-muted">{enumLabel(t, 'blocked_reason', task.blocked_reason)}</span>
                ) : null}
              </td>
              <td className="px-3 py-2">{t('run.attempts', { count: task.attempt_count, max: task.max_attempts })}</td>
              <td className="px-3 py-2">
                {task.depends_on.length > 0
                  ? task.depends_on.map((id) => titles.get(id) ?? t('run.unknownTask', { id: id.slice(0, 8) })).join(', ')
                  : t('run.noDependencies')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunTasks({ runId }: { runId: string }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: runKeys.tasks(runId),
    queryFn: ({ pageParam, signal }) =>
      runsApi.tasks(runId, { limit: TASKS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const tasks = query.data?.pages.flatMap((p) => p.tasks) ?? [];
  const planVersion = query.data?.pages[0]?.plan_version ?? 0;
  return (
    <section aria-labelledby="run-tasks-title" className="mb-6">
      <h2 id="run-tasks-title" className="mb-3 text-lg font-semibold">
        {t('run.tasks')}
      </h2>
      {query.isPending ? (
        <LoadingState label={t('run.tasksLoading')} />
      ) : query.isError && tasks.length === 0 ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <TasksTable runId={runId} tasks={tasks} planVersion={planVersion} />
          {query.hasNextPage || query.isFetchNextPageError ? (
            <LoadMore
              shown={tasks.length}
              hasNextPage={query.hasNextPage}
              isFetchingNextPage={query.isFetchingNextPage}
              nextPageError={query.isFetchNextPageError ? query.error : null}
              onLoadMore={() => void query.fetchNextPage()}
            />
          ) : null}
        </>
      )}
    </section>
  );
}

function EventFeed({
  runId,
  isTerminal,
  taskTitle,
}: {
  runId: string;
  isTerminal: () => boolean;
  taskTitle: (id: string) => string | undefined;
}) {
  const { t, i18n } = useTranslation();
  const queryClient = useQueryClient();
  const [resynced, setResynced] = useState(false);
  const onEvents = useCallback(
    (events: RunEvent[]) => {
      // Одна инвалидация на пачку: replay сотен событий не порождает сотни запросов.
      const runChanged = events.some((e) => RUN_STATE_EVENTS.has(e.type));
      const tasksChanged = events.some((e) => TASK_STATE_EVENTS.has(e.type));
      if (runChanged || tasksChanged) void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId), exact: true });
      if (tasksChanged) void queryClient.invalidateQueries({ queryKey: runKeys.tasks(runId) });
    },
    [queryClient, runId],
  );
  const onResync = useCallback(() => {
    setResynced(true);
    void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId) });
  }, [queryClient, runId]);
  const { events, state, error, reconnect } = useRunEvents(runId, { onEvents, onResync, isTerminal });
  const time = useMemo(() => new Intl.DateTimeFormat(i18n.language, { timeStyle: 'medium' }), [i18n.language]);

  return (
    <section aria-labelledby="run-feed-title">
      <h2 id="run-feed-title" className="mb-3 text-lg font-semibold">
        {t('run.feed')}
      </h2>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <p role="status" className="text-sm text-fg-muted" data-testid="stream-state" data-state={state}>
          {t(`run.stream.${state}`)}
        </p>
        {state === 'failed' ? (
          <button type="button" className="btn-secondary" onClick={reconnect}>
            {t('run.reconnect')}
          </button>
        ) : null}
      </div>
      {state === 'failed' && error ? (
        <p role="alert" className="mb-3 text-sm font-medium text-danger-text">
          {problemMessage(t, error)}
        </p>
      ) : null}
      {resynced ? (
        <p className="mb-3 rounded-md border border-warning-border bg-warning-soft p-3 text-sm text-warning-fg">{t('run.resynced')}</p>
      ) : null}
      {events.length === 0 ? (
        <p className="text-fg-muted">{t('run.feedEmpty')}</p>
      ) : (
        <>
          {events.length >= FEED_LIMIT ? (
            <p className="mb-2 text-sm text-fg-muted">{t('run.feedTrimmed', { count: FEED_LIMIT })}</p>
          ) : null}
          <ol aria-label={t('run.feedLabel')} className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface">
            {events.map((e) => {
              const text = describeEvent(t, e, taskTitle);
              return (
                <li key={e.event_id} className="flex flex-col gap-0.5 px-3 py-2 text-sm" data-seq={e.seq} data-event-id={e.event_id}>
                  <div className="flex flex-wrap items-baseline gap-x-2 text-fg-muted">
                    <span className="font-mono text-xs">{t('event.seq', { seq: e.seq })}</span>
                    {e.timestamp ? <time dateTime={e.timestamp}>{time.format(new Date(e.timestamp))}</time> : null}
                    <span className="font-semibold text-fg">{t(`event.type.${e.type}`, { defaultValue: e.type })}</span>
                  </div>
                  {text ? <p className="whitespace-pre-line break-words">{text}</p> : null}
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}

export function RunPage() {
  const { t } = useTranslation();
  const { projectId = '', runId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const queryClient = useQueryClient();
  const run = useQuery({
    queryKey: runKeys.detail(runId),
    queryFn: ({ signal }) => runsApi.get(runId, signal),
    enabled: Boolean(runId && membership),
  });

  const statusRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    statusRef.current = run.data?.status;
  }, [run.data?.status]);
  const isTerminal = useCallback(() => RUN_TERMINAL.includes(statusRef.current ?? ''), []);
  const taskTitle = useCallback(
    (id: string) =>
      queryClient
        .getQueryData<{ pages: { tasks: TaskSummary[] }[] }>(runKeys.tasks(runId))
        ?.pages.flatMap((p) => p.tasks)
        .find((x) => x.id === id)?.title,
    [queryClient, runId],
  );

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const title = run.data
    ? t('run.title', { mode: t(`enums.run_mode.${run.data.mode}`, { defaultValue: run.data.mode }) })
    : t('run.loading');

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={title}>
        <Link to={`/w/${workspaceId}/projects/${projectId}/runs`} className="btn-secondary">
          {t('runs.backToRuns')}
        </Link>
      </PageHeading>
      {run.isPending ? (
        <LoadingState label={t('run.loading')} />
      ) : run.isError ? (
        <ErrorState error={run.error} onRetry={() => void run.refetch()} />
      ) : (
        <>
          <RunHeader run={run.data} />
          {can(membership.role, 'run:control') ? <RunControls run={run.data} /> : null}
          <RunTasks runId={runId} />
        </>
      )}
      {runId && !run.isError ? <EventFeed key={runId} runId={runId} isTerminal={isTerminal} taskTitle={taskTitle} /> : null}
    </section>
  );
}
