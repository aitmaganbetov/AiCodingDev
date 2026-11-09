/**
 * Workflow Navigator (F4, план §3.3, §6.2): активные запуски workspace (listRuns + поток workspace),
 * этапы выбранного run, текущие исполнители и DAG задач — граф (React Flow, лениво) или доступный
 * список (таблица того же графа). Выбор задачи — `task` в URL (Agent Inspector, подсветка узла).
 */
import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { runKeys, runsApi, type Run, type RunStatus, type RunSummary, type TaskSummary } from '../../api/runs';
import { StatusBadge } from '../../components/StatusBadge';
import { AgentBadge, EmptyState, ErrorState, Icon, LoadingState, RelativeTime, Skeleton, Table, cx, type Column } from '../../components/ui';
import { useWorkspaceInvalidation } from '../quality/live';
import { enumLabel, executorLabel, providerLabel, roleLabel, TASK_ACTIVE } from '../runs/labels';
import type { TaskIndicators } from './feedModel';
import type { NavMode } from './mcParams';

const TaskGraphCanvas = lazy(() => import('../runs/TaskGraphCanvas'));

/** Активные статусы run (не терминальные). */
export const ACTIVE_RUN_STATUSES: RunStatus[] = ['CREATED', 'DISCOVERY', 'PLANNED', 'AWAITING_APPROVAL', 'RUNNING', 'VERIFYING', 'PAUSED', 'BLOCKED'];
export const MISSION_CONTROL_RUNS_LIMIT = 20;

export function useActiveRuns(workspaceId: string) {
  const runs = useQuery({
    queryKey: [...runKeys.list(workspaceId), 'mission-control'] as const,
    queryFn: ({ signal }) => runsApi.list(workspaceId, { status: ACTIVE_RUN_STATUSES, limit: MISSION_CONTROL_RUNS_LIMIT }, signal),
    enabled: Boolean(workspaceId),
  });
  // Поток уведомлений workspace (общее соединение с оболочкой): run.* → перечитать список.
  useWorkspaceInvalidation(workspaceId, ['run.state_changed', 'run.completed'], [runKeys.list(workspaceId)]);
  return runs;
}

/** Список активных запусков (кнопки с aria-pressed: выбор run для всех панелей). */
export function RunList({ runs, selected, onSelect }: { runs: readonly RunSummary[]; selected: string | null; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  return (
    <ul className="flex flex-col gap-2" aria-label={t('missionControl.activeRunsList')} data-testid="mc-run-list">
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
  );
}

/** Сворачиваемый список активных запусков над этапами выбранного run. */
function RunSwitcher({ workspaceId, selected, onSelect }: { workspaceId: string; selected: string; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const runs = useActiveRuns(workspaceId);
  const items = runs.data?.items ?? [];
  return (
    <section aria-labelledby="mc-runs-title" className="flex flex-col gap-2">
      <h3 id="mc-runs-title" className="sr-only">
        {t('missionControl.activeRunsList')}
      </h3>
      <button
        type="button"
        className="btn-ghost btn-sm justify-between"
        aria-expanded={open}
        aria-controls="mc-runs-panel"
        onClick={() => setOpen((v) => !v)}
      >
        <span>{runs.isPending ? t('missionControl.loading') : t('missionControl.activeRuns', { count: items.length })}</span>
        <Icon name={open ? 'chevronUp' : 'chevronDown'} className="h-4 w-4" />
      </button>
      <div id="mc-runs-panel" hidden={!open}>
        {runs.isError ? (
          <ErrorState error={runs.error} onRetry={() => void runs.refetch()} title={t('missionControl.error')} />
        ) : items.length === 0 && !runs.isPending ? (
          <p className="text-sm text-fg-muted">{t('missionControl.emptyDescription')}</p>
        ) : (
          <RunList runs={items} selected={selected} onSelect={onSelect} />
        )}
        {runs.data?.next_cursor ? <p className="mt-1 text-xs text-fg-muted">{t('missionControl.sample', { count: MISSION_CONTROL_RUNS_LIMIT })}</p> : null}
      </div>
    </section>
  );
}

const STAGES: readonly RunStatus[] = ['CREATED', 'DISCOVERY', 'PLANNED', 'RUNNING', 'VERIFYING', 'COMPLETED'];

/** Этапы жизненного цикла run (FSM): пройденные, текущий, следующие; особые состояния — бейджем. */
export function RunStages({ run }: { run: Run }) {
  const { t } = useTranslation();
  const index = STAGES.indexOf(run.status);
  // PAUSED/BLOCKED/AWAITING_APPROVAL/FAILED/CANCELLED: этап по началу/завершению run.
  const reached = index >= 0 ? index : run.finished_at ? STAGES.length - 1 : run.started_at ? STAGES.indexOf('RUNNING') : 0;
  const counts = Object.entries(run.task_counts ?? {}).filter(([, n]) => n > 0);
  return (
    <section aria-labelledby="mc-stages-title" className="flex flex-col gap-2" data-testid="mc-stages">
      <h3 id="mc-stages-title" className="text-sm font-semibold text-fg">
        {t('missionControl.stages.title')}
      </h3>
      <ol className="flex flex-wrap gap-1 text-xs">
        {STAGES.map((stage, i) => {
          const state = index < 0 && i === reached ? 'special' : i < reached ? 'done' : i === reached ? 'current' : 'next';
          return (
            <li
              key={stage}
              data-stage={stage}
              data-state={state}
              aria-current={state === 'current' || state === 'special' ? 'step' : undefined}
              className={cx(
                'inline-flex items-center gap-1 rounded-full border px-2 py-0.5',
                state === 'done' && 'border-success-border bg-success-soft text-success-fg',
                (state === 'current' || state === 'special') && 'border-accent bg-accent-soft font-semibold text-accent-soft-fg',
                state === 'next' && 'border-dashed border-border text-fg-muted',
              )}
            >
              {state === 'done' ? <Icon name="check" className="h-3 w-3" /> : null}
              {enumLabel(t, 'run_status', stage)}
              <span className="sr-only">
                {' — '}
                {t(`missionControl.stages.${state === 'special' ? 'current' : state}`)}
              </span>
            </li>
          );
        })}
      </ol>
      {index < 0 ? (
        <p className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          {t('missionControl.stages.special')} <StatusBadge kind="run" status={run.status} />
        </p>
      ) : null}
      {counts.length > 0 ? (
        <p className="text-xs text-fg-muted" data-testid="mc-task-counts">
          {counts.map(([s, n]) => `${enumLabel(t, 'task_status', s)}: ${n}`).join(' · ')}
        </p>
      ) : null}
    </section>
  );
}

/** Кто сейчас работает: активные задачи с ролью агента и CLI-провайдером (выбор — в инспектор). */
export function CurrentExecutors({ tasks, onSelect }: { tasks: readonly TaskSummary[]; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const active = tasks.filter((x) => TASK_ACTIVE.includes(x.status));
  return (
    <section aria-labelledby="executors-title" className="flex flex-col gap-1">
      <h3 id="executors-title" className="text-sm font-semibold text-fg">
        {t('runGraph.executors')}
      </h3>
      {active.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('runGraph.executorsEmpty')}</p>
      ) : (
        <ul className="flex flex-col gap-1 text-sm" data-testid="current-executors">
          {active.map((x) => (
            <li key={x.id} className="flex flex-wrap items-center gap-2">
              <AgentBadge agentRole={x.agent_role} showName={false} provider={providerLabel(t, x.current_provider) || undefined} />
              <span className="font-medium">{executorLabel(t, x.agent_role, x.current_provider)}</span>
              <span aria-hidden="true">—</span>
              <button type="button" className="link text-left" onClick={() => onSelect(x.id)}>
                {x.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Доступная альтернатива графа: таблица задач, зависимостей и исполнителей + выбор задачи. */
export function TaskTable({
  tasks,
  taskHref,
  selectedId,
  onSelect,
  indicators,
}: {
  tasks: readonly TaskSummary[];
  taskHref: (id: string) => string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  indicators: ReadonlyMap<string, TaskIndicators>;
}) {
  const { t } = useTranslation();
  const titles = useMemo(() => new Map(tasks.map((x) => [x.id, x.title])), [tasks]);
  const columns: Column<TaskSummary>[] = [
    {
      id: 'title',
      header: t('run.col.title'),
      rowHeader: true,
      sortValue: (x) => x.title,
      cell: (x) => (
        <span className="flex flex-col items-start gap-1">
          <button type="button" className="link text-left font-medium" aria-pressed={x.id === selectedId} onClick={() => onSelect(x.id)}>
            {x.title}
          </button>
          <Link to={taskHref(x.id)} className="link text-xs">
            {t('missionControl.node.openTask')}
            <span className="sr-only">: {x.title}</span>
          </Link>
        </span>
      ),
    },
    { id: 'role', header: t('run.col.role'), sortValue: (x) => roleLabel(t, x.agent_role), cell: (x) => <AgentBadge agentRole={x.agent_role} /> },
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
      id: 'verified',
      header: t('missionControl.node.verifiedHeader'),
      cell: (x) => {
        const ind = indicators.get(x.id);
        return ind && ind.gates > 0 ? t('missionControl.node.verified', { verified: ind.verified, total: ind.gates }) : t('missionControl.node.noGates');
      },
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
        x.depends_on.length > 0 ? x.depends_on.map((id) => titles.get(id) ?? t('run.unknownTask', { id: id.slice(0, 8) })).join(', ') : t('run.noDependencies'),
    },
  ];
  return (
    <Table
      caption={t('runGraph.tableCaption')}
      columns={columns}
      rows={tasks}
      rowKey={(x) => x.id}
      density="compact"
      rowProps={(x) => ({ 'data-task-id': x.id, 'data-status': x.status, 'data-selected': x.id === selectedId ? 'true' : undefined })}
      minWidthClass="min-w-[52rem]"
    />
  );
}

/** DAG выбранного run: переключатель «Граф/Список» (aria-pressed) и представление. */
export function TaskDag({
  tasks,
  edges,
  loading,
  error,
  onRetry,
  mode,
  onMode,
  taskHref,
  selectedId,
  onSelect,
  indicators,
  graphClassName,
}: {
  tasks: readonly TaskSummary[];
  edges: Parameters<typeof TaskGraphCanvas>[0]['edges'];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  mode: NavMode;
  onMode: (m: NavMode) => void;
  taskHref: (id: string) => string;
  selectedId: string | null;
  onSelect: (id: string) => void;
  indicators: ReadonlyMap<string, TaskIndicators>;
  graphClassName?: string;
}) {
  const { t } = useTranslation();
  if (loading) return <Skeleton lines={4} />;
  if (error && tasks.length === 0) return <ErrorState error={error} onRetry={onRetry} />;
  if (tasks.length === 0) return <EmptyState headingLevel={3} icon="layers" title={t('runGraph.emptyTitle')} description={t('run.tasksEmpty')} />;
  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="group" aria-label={t('runGraph.viewLabel')} className="inline-flex rounded-md border border-border">
          {(['graph', 'list'] as const).map((v) => (
            <button
              key={v}
              type="button"
              aria-pressed={mode === v}
              onClick={() => onMode(v)}
              className={cx(
                'min-h-touch px-4 text-sm font-medium first:rounded-l-md last:rounded-r-md',
                mode === v ? 'bg-accent text-accent-fg' : 'bg-surface text-fg hover:bg-surface-muted',
              )}
            >
              {t(`runGraph.view.${v}`)}
            </button>
          ))}
        </div>
        <p className="text-xs text-fg-muted" id="graph-hint">
          {t('missionControl.graphHint', { tasks: tasks.length, edges: edges.length })}
        </p>
      </div>
      {mode === 'graph' ? (
        <Suspense fallback={<LoadingState label={t('runGraph.loadingCanvas')} />}>
          <TaskGraphCanvas
            tasks={tasks}
            edges={edges}
            taskHref={taskHref}
            label={t('runGraph.canvasLabel')}
            selectedId={selectedId}
            onSelect={onSelect}
            indicators={indicators}
            {...(graphClassName ? { className: graphClassName } : {})}
          />
        </Suspense>
      ) : (
        <TaskTable tasks={tasks} taskHref={taskHref} selectedId={selectedId} onSelect={onSelect} indicators={indicators} />
      )}
    </div>
  );
}

export { RunSwitcher };
