/**
 * P7-08 Task Detail: задача и критерии приёмки, текущий исполнитель, попытки, события/логи
 * (история + live), ACP-сообщения, diff viewer (Monaco, лениво) и evidence / gate executions.
 */
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { runKeys } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { taskKeys, tasksApi, type Task } from '../../api/tasks';
import { useRunEvents } from '../../api/useRunEvents';
import { Card, ErrorState, LoadingState, PageHeading, StatusBadge, Tabs, TabsContent, TabsList, TabsTrigger, Timestamp } from '../../components/ui';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { enumLabel, executorLabel, providerLabel, roleLabel, TASK_ACTIVE } from '../runs/labels';
import { RunNav } from '../runs/RunNav';
import { StreamStatus } from '../runs/StreamStatus';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';
import { useRunDetail } from '../runs/useRunStatus';
import { DiffViewer } from './DiffViewer';
import { TaskAttempts, TaskEvents, TaskEvidenceList, TaskGates, TaskMessages } from './TaskSections';

const TABS = ['overview', 'attempts', 'events', 'messages', 'diff', 'evidence'] as const;
type Tab = (typeof TABS)[number];

function TaskSummaryCard({ task }: { task: Task }) {
  const { t } = useTranslation();
  const active = TASK_ACTIVE.includes(task.status);
  return (
    <Card className="mb-6">
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 text-sm sm:grid-cols-[auto,1fr]">
        <dt className="text-fg-muted">{t('tasks.status')}</dt>
        <dd className="flex flex-wrap items-center gap-2">
          <StatusBadge kind="task" status={task.status} />
          {task.blocked_reason ? <span className="text-fg-muted">{enumLabel(t, 'blocked_reason', task.blocked_reason)}</span> : null}
        </dd>
        <dt className="text-fg-muted">{t('tasks.role')}</dt>
        <dd>{roleLabel(t, task.agent_role)}</dd>
        <dt className="text-fg-muted">{t('tasks.executor')}</dt>
        <dd data-testid="task-executor">
          {active
            ? executorLabel(t, task.agent_role, task.current_provider)
            : task.current_provider
              ? t('tasks.lastProvider', { provider: providerLabel(t, task.current_provider) })
              : t('runGraph.noExecutor')}
        </dd>
        <dt className="text-fg-muted">{t('run.col.attempts')}</dt>
        <dd>{t('run.attempts', { count: task.attempt_count, max: task.max_attempts })}</dd>
        {task.review_loop_count !== undefined ? (
          <>
            <dt className="text-fg-muted">{t('tasks.reviewLoops')}</dt>
            <dd>{task.review_loop_count}</dd>
          </>
        ) : null}
        <dt className="text-fg-muted">{t('tasks.complexity')}</dt>
        <dd>
          {task.complexity} · {t(`tasks.enums.risk.${task.risk_level}`, { defaultValue: task.risk_level })}
        </dd>
        {task.next_attempt_at ? (
          <>
            <dt className="text-fg-muted">{t('tasks.nextAttempt')}</dt>
            <dd>
              <Timestamp value={task.next_attempt_at} />
            </dd>
          </>
        ) : null}
        <dt className="text-fg-muted">{t('tasks.updatedAt')}</dt>
        <dd>
          <Timestamp value={task.updated_at} />
        </dd>
      </dl>
    </Card>
  );
}

function Overview({ task }: { task: Task }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4 text-sm">
      <section aria-labelledby="task-instructions">
        <h3 id="task-instructions" className="mb-1 font-semibold">
          {t('tasks.instructions')}
        </h3>
        {task.instructions ? <p className="whitespace-pre-wrap break-words">{task.instructions}</p> : <p className="text-fg-muted">—</p>}
      </section>
      <section aria-labelledby="task-criteria">
        <h3 id="task-criteria" className="mb-1 font-semibold">
          {t('tasks.criteria')}
        </h3>
        {(task.acceptance_criteria ?? []).length > 0 ? (
          <ul className="list-disc pl-5">
            {(task.acceptance_criteria ?? []).map((c, i) => (
              <li key={i} className="break-words">
                {c}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-fg-muted">{t('tasks.noCriteria')}</p>
        )}
      </section>
      {task.depends_on.length > 0 ? (
        <p className="text-fg-muted">{t('tasks.dependsCount', { count: task.depends_on.length })}</p>
      ) : null}
    </div>
  );
}

export function TaskDetailPage() {
  const { t } = useTranslation();
  const { projectId = '', runId = '', taskId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const queryClient = useQueryClient();
  const [params, setParams] = useInPageSearchParams();
  const tabParam = params.get('tab');
  const tab: Tab = (TABS as readonly string[]).includes(tabParam ?? '') ? (tabParam as Tab) : 'overview';
  const enabled = Boolean(membership && taskId);
  const role = membership?.role;
  const canEvidence = can(role, 'evidence:read');
  const canQuality = can(role, 'quality:read');
  const canDownload = can(role, 'artifact:download');

  const task = useQuery({ queryKey: taskKeys.detail(taskId), queryFn: ({ signal }) => tasksApi.get(taskId, signal), enabled });
  const { isTerminal } = useRunDetail(runId, enabled);
  const evidence = useInfiniteQuery({
    queryKey: taskKeys.evidence(taskId),
    queryFn: ({ pageParam, signal }) => tasksApi.evidence(taskId, { limit: 100, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: enabled && canEvidence && (tab === 'diff' || tab === 'evidence'),
  });
  const quality = useQuery({
    queryKey: taskKeys.quality(runId),
    queryFn: ({ signal }) => tasksApi.quality(runId, signal),
    enabled: enabled && canQuality && tab === 'evidence',
  });
  const evidenceItems = useMemo(() => evidence.data?.pages.flatMap((p) => p.items) ?? [], [evidence.data]);
  const gates = useMemo(() => (quality.data?.gate_executions ?? []).filter((g) => g.task_id === taskId), [quality.data, taskId]);

  const onEvents = useCallback(
    (events: RunEvent[]) => {
      const mine = events.filter((e) => e.payload.task_id === taskId);
      if (mine.some((e) => e.type === 'task.state_changed')) {
        void queryClient.invalidateQueries({ queryKey: taskKeys.detail(taskId), exact: true });
        void queryClient.invalidateQueries({ queryKey: taskKeys.attempts(taskId) });
      }
      if (mine.some((e) => e.type === 'agent.message')) void queryClient.invalidateQueries({ queryKey: taskKeys.messages(taskId) });
      if (mine.some((e) => e.type === 'gate.completed')) {
        void queryClient.invalidateQueries({ queryKey: taskKeys.evidence(taskId) });
        void queryClient.invalidateQueries({ queryKey: taskKeys.quality(runId) });
      }
      if (events.some((e) => e.type === 'run.state_changed' || e.type === 'run.completed'))
        void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId), exact: true });
    },
    [queryClient, taskId, runId],
  );
  // Общее соединение run (фильтр task_id на клиенте): не тратит отдельный поток из лимита 5.
  const stream = useRunEvents(runId, { onEvents, isTerminal });
  const liveForTask = useMemo(() => stream.events.filter((e) => e.payload.task_id === taskId), [stream.events, taskId]);

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const setTab = (next: string) => {
    const p = new URLSearchParams(params);
    if (next === 'overview') p.delete('tab');
    else p.set('tab', next);
    setParams(p);
  };

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={task.data ? task.data.title : t('tasks.title')}>
        <div className="flex flex-wrap gap-2">
          <Link to={`/w/${workspaceId}/mission-control?${new URLSearchParams({ run: runId, task: taskId }).toString()}`} className="btn-secondary">
            {t('tasks.backToGraph')}
          </Link>
          {can(role, 'chat:read') ? (
            <Link
              to={`/w/${workspaceId}/mission-control?${new URLSearchParams({ run: runId, task: taskId, stream: 'task', ctask: taskId }).toString()}`}
              className="btn-secondary"
            >
              {t('tasks.openChat')}
            </Link>
          ) : null}
        </div>
      </PageHeading>
      <RunNav workspaceId={workspaceId} projectId={projectId} runId={runId} taskId={taskId} />
      {task.isPending ? (
        <LoadingState label={t('tasks.loading')} />
      ) : task.isError ? (
        <ErrorState error={task.error} onRetry={() => void task.refetch()} />
      ) : (
        <>
          <TaskSummaryCard task={task.data} />
          <div className="mb-3">
            <StreamStatus state={stream.state} error={stream.error} onReconnect={stream.reconnect} />
          </div>
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList label={t('tasks.tabsLabel')}>
              {TABS.map((x) => (
                <TabsTrigger key={x} value={x}>
                  {t(`tasks.tab.${x}`)}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="overview">
              <Overview task={task.data} />
            </TabsContent>
            <TabsContent value="attempts">
              <TaskAttempts taskId={taskId} canDownload={canDownload} />
            </TabsContent>
            <TabsContent value="events">
              <TaskEvents runId={runId} taskId={taskId} live={liveForTask} />
            </TabsContent>
            <TabsContent value="messages">
              <TaskMessages taskId={taskId} />
            </TabsContent>
            <TabsContent value="diff">
              {!canEvidence ? (
                <p className="text-fg-muted">{t('state.forbiddenDescription')}</p>
              ) : evidence.isPending ? (
                <LoadingState label={t('tasks.evidence.loading')} />
              ) : evidence.isError ? (
                <ErrorState error={evidence.error} onRetry={() => void evidence.refetch()} />
              ) : (
                <DiffViewer evidence={evidenceItems} canDownload={canDownload} />
              )}
            </TabsContent>
            <TabsContent value="evidence">
              <div className="flex flex-col gap-6">
                {canQuality ? (
                  quality.isPending ? (
                    <LoadingState label={t('tasks.gates.loading')} />
                  ) : quality.isError ? (
                    <ErrorState error={quality.error} onRetry={() => void quality.refetch()} />
                  ) : (
                    <TaskGates gates={gates} evidenceIds={new Set(evidenceItems.map((e) => e.id))} />
                  )
                ) : null}
                <section aria-labelledby="task-evidence-title">
                  <h3 id="task-evidence-title" className="mb-2 font-semibold">
                    {t('tasks.evidence.title')}
                  </h3>
                  {!canEvidence ? (
                    <p className="text-fg-muted">{t('state.forbiddenDescription')}</p>
                  ) : evidence.isPending ? (
                    <LoadingState label={t('tasks.evidence.loading')} />
                  ) : evidence.isError ? (
                    <ErrorState error={evidence.error} onRetry={() => void evidence.refetch()} />
                  ) : (
                    <TaskEvidenceList items={evidenceItems} canDownload={canDownload} canDownloadRaw={can(role, 'artifact:download_raw')} />
                  )}
                </section>
              </div>
            </TabsContent>
          </Tabs>
        </>
      )}
    </section>
  );
}

export default TaskDetailPage;
