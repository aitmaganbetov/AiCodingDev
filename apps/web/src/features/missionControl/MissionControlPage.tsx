/**
 * AI Mission Control (F4, план §3.3, §5.8, §6.2): центральный экран исполнения.
 *
 *  ≥ 1366 px — три панели: Workflow Navigator | Live Agent Chat (+ User Command Center снизу) — изменяемые
 *  разделители (react-resizable-panels, размеры `aicd.layout.mission-control.*`), Agent Inspector —
 *  правая контекстная панель оболочки (ширина и открытость сохраняются, F2);
 *  768–1365 px — две панели: вкладки «Навигатор | Инспектор» и лента с Command Center;
 *  < 768 px — одна панель с сегментными вкладками «Навигатор | Лента | Инспектор», Command Center ниже.
 *
 * Состояние (run, задача, вид ленты, фильтры, вкладка) — в URL (`mcParams.ts`). Данные — только API:
 * getRun/getRunTasks/getRunChat/listDirectives/listRunEvidence/getRunQuality/getTask/listTaskAttempts/
 * getAttemptContextPack + один поток SSE run на экран (StreamManager).
 */
import { useCallback, useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { isApiError } from '../../api/client';
import type { ChatFilters } from '../../api/chat';
import type { Run } from '../../api/runs';
import { PageHeading } from '../../components/PageHeading';
import { StatusBadge } from '../../components/StatusBadge';
import { ButtonLink, EmptyState, ErrorState, IconButton, LoadingState, Tabs, TabsContent, TabsList, TabsTrigger } from '../../components/ui';
import { ResizableGroup, ResizableHandle, ResizablePanel, useResizableGroup } from '../../components/ui/Resizable';
import { ContextPanel } from '../../layout/ContextPanel';
import { MQ_DESKTOP, useContextOpenPref, useMediaQuery } from '../../lib/layoutPrefs';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';
import { CommandCenter } from './CommandCenter';
import { FeedPanel } from './FeedPanel';
import { RunInspector, TaskInspector, useTaskSummary } from './Inspector';
import { readMcState, writeMcState, type FeedFilters, type McPane, type McState, type McView, type NavMode } from './mcParams';
import { CurrentExecutors, MISSION_CONTROL_RUNS_LIMIT, RunList, RunStages, RunSwitcher, TaskDag, useActiveRuns } from './Navigator';
import { useMissionControlRun } from './useRunData';

/** Три панели (Navigator | лента | Inspector в контекстной панели) — с этой ширины окна (план §5.8). */
export const MQ_MC_WIDE = '(min-width: 1366px)';
export const MC_SCREEN_ID = 'mission-control';

/** Выбор активного run (без `?run=`). */
function RunChooser({ workspaceId, onSelect }: { workspaceId: string; onSelect: (id: string) => void }) {
  const { t } = useTranslation();
  const runs = useActiveRuns(workspaceId);
  const items = runs.data?.items ?? [];
  return (
    <section aria-labelledby="mc-choose-title" className="flex max-w-3xl flex-col gap-3">
      <h2 id="mc-choose-title" className="text-lg font-semibold">
        {runs.isPending ? t('missionControl.activeRunsList') : t('missionControl.activeRuns', { count: items.length })}
      </h2>
      <p className="text-sm text-fg-muted">{t('missionControl.chooseHint')}</p>
      {runs.isPending ? (
        <LoadingState label={t('missionControl.loading')} />
      ) : runs.isError ? (
        <ErrorState error={runs.error} onRetry={() => void runs.refetch()} title={t('missionControl.error')} />
      ) : items.length === 0 ? (
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
      ) : (
        <RunList runs={items} selected={null} onSelect={onSelect} />
      )}
      {runs.data?.next_cursor ? <p className="text-sm text-fg-muted">{t('missionControl.sample', { count: MISSION_CONTROL_RUNS_LIMIT })}</p> : null}
    </section>
  );
}

/** Сброс размеров обеих групп панелей (WCAG 2.5.7: альтернатива перетаскиванию — кнопка). */
function RegisterGroupReset({ register }: { register: (fn: () => void) => void }) {
  const { reset } = useResizableGroup();
  useEffect(() => register(reset), [register, reset]);
  return null;
}
function ResetLayoutButton({ resets }: { resets: { current: (() => void)[] } }) {
  const { t } = useTranslation();
  return <IconButton icon="refresh" label={t('missionControl.resetPanels')} onClick={() => resets.current.forEach((fn) => fn())} />;
}

function RunBar({ run, planVersion, taskTitle, onClearTask, extra }: { run: Run; planVersion: number; taskTitle: string | null; onClearTask: () => void; extra?: ReactNode }) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-surface px-3 py-2 text-sm" data-testid="mc-run-bar">
      <span className="flex items-center gap-2">
        <span className="text-fg-muted">{t('run.status')}:</span>
        <span data-testid="run-status">
          <StatusBadge kind="run" status={run.status} />
        </span>
      </span>
      <span className="text-fg-muted">{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</span>
      {planVersion > 0 ? <span className="text-fg-muted">{t('runGraph.planVersion', { version: planVersion })}</span> : null}
      <span className="min-w-0 flex-1 truncate font-medium text-fg" title={run.objective}>
        {run.objective}
      </span>
      {taskTitle ? (
        <span className="flex min-w-0 items-center gap-1 text-fg-muted">
          <span className="truncate">{t('missionControl.selectedTask', { task: taskTitle })}</span>
          <IconButton icon="close" label={t('missionControl.clearTask')} onClick={onClearTask} />
        </span>
      ) : null}
      {extra}
    </div>
  );
}

function RunWorkspace({
  workspaceId,
  role,
  state,
  update,
}: {
  workspaceId: string;
  role: string;
  state: McState;
  update: (patch: Partial<McState>) => void;
}) {
  const { t } = useTranslation();
  const runId = state.runId ?? '';
  const data = useMissionControlRun(runId, role, state.taskId, t);
  const wide = useMediaQuery(MQ_MC_WIDE, true);
  const desktop = useMediaQuery(MQ_DESKTOP, true);
  const [, setContextOpen] = useContextOpenPref(wide ? MC_SCREEN_ID : null);
  // Сброс обеих групп панелей (горизонтальной и вертикальной) одной кнопкой в строке run.
  const resets = useRef<(() => void)[]>([]);
  const registerOuterReset = useCallback((fn: () => void) => {
    resets.current[0] = fn;
  }, []);
  const registerInnerReset = useCallback((fn: () => void) => {
    resets.current[1] = fn;
  }, []);
  const run = data.run.data;
  const selected = useTaskSummary(data.tasks, state.taskId);

  const taskHref = useCallback(
    (taskId: string, tab?: string) =>
      `/w/${workspaceId}/projects/${run?.project_id ?? ''}/runs/${runId}/tasks/${taskId}${tab && tab !== 'overview' ? `?tab=${tab}` : ''}`,
    [workspaceId, run?.project_id, runId],
  );
  const selectTask = useCallback(
    (taskId: string) => {
      update({ taskId, ...(!desktop ? { pane: 'inspector' as McPane } : {}) });
      if (wide) setContextOpen(true);
    },
    [update, desktop, wide, setContextOpen],
  );
  const clearTask = useCallback(() => update({ taskId: null }), [update]);
  const setView = useCallback((view: McView) => update({ view }), [update]);
  const setNav = useCallback((nav: NavMode) => update({ nav }), [update]);
  const setChat = useCallback((chat: ChatFilters) => update({ chat }), [update]);
  const setFeed = useCallback((feed: FeedFilters) => update({ feed }), [update]);

  if (data.run.isError) {
    const notFound = isApiError(data.run.error) && data.run.error.status === 404;
    return (
      <ErrorState
        error={data.run.error}
        title={notFound ? t('missionControl.runNotFound') : t('missionControl.resolveError')}
        onRetry={() => void data.run.refetch()}
      />
    );
  }
  if (!run) return <LoadingState label={t('run.loading')} />;

  const navigator = (
    <section aria-labelledby="mc-nav-title" className="flex flex-col gap-4" data-testid="mc-navigator">
      <h2 id="mc-nav-title" className="text-base font-semibold text-fg">
        {t('missionControl.navigator.title')}
      </h2>
      <RunSwitcher workspaceId={workspaceId} selected={run.id} onSelect={(id) => update({ runId: id, taskId: null })} />
      <RunStages run={run} />
      <CurrentExecutors tasks={data.tasks} onSelect={selectTask} />
      <TaskDag
        tasks={data.tasks}
        edges={data.edges}
        loading={data.tasksQuery.isPending}
        error={data.tasksQuery.isError ? data.tasksQuery.error : null}
        onRetry={() => void data.tasksQuery.refetch()}
        mode={state.nav}
        onMode={setNav}
        taskHref={taskHref}
        selectedId={state.taskId}
        onSelect={selectTask}
        indicators={data.indicators}
        graphClassName={wide ? 'h-[max(22rem,calc(100dvh-30rem))]' : 'h-[26rem]'}
      />
      {data.tasksQuery.isFetchingNextPage ? <LoadingState inline label={t('runGraph.loadingMore')} /> : null}
    </section>
  );
  const feed = (
    <FeedPanel
      workspaceId={workspaceId}
      role={role}
      run={run}
      data={data}
      view={state.view}
      onView={setView}
      chatFilters={state.chat}
      onChatFilters={setChat}
      feedFilters={state.feed}
      onFeedFilters={setFeed}
      taskHref={taskHref}
      onSelectTask={selectTask}
    />
  );
  const command = <CommandCenter run={run} role={role} workspaceId={workspaceId} tasks={data.tasks} selectedTask={selected ?? null} taskHref={taskHref} />;
  const inspector = state.taskId ? (
    <TaskInspector
      key={state.taskId}
      role={role}
      summary={selected}
      gates={data.quality.data?.gate_executions ?? null}
      events={data.stream.events}
      taskHref={taskHref}
      onClose={clearTask}
    />
  ) : (
    <RunInspector workspaceId={workspaceId} role={role} run={run} />
  );
  const inspectorTitle = state.taskId ? t('missionControl.inspector.title') : t('missionControl.inspector.runTitle');

  let layout: ReactNode;
  if (wide) {
    layout = (
      <div className="h-[max(36rem,calc(100dvh-12.5rem))] min-h-0" data-testid="mc-layout" data-layout="three">
        <ResizableGroup id="mission-control.main" defaultLayout={{ 'mc-nav': 36, 'mc-center': 64 }}>
          <RegisterGroupReset register={registerOuterReset} />
          <ResizablePanel id="mc-nav" defaultSize="36" minSize="22">
            <div className="h-full overflow-y-auto pr-2">{navigator}</div>
          </ResizablePanel>
          <ResizableHandle before={t('missionControl.navigator.title')} after={t('missionControl.feed.title')} />
          <ResizablePanel id="mc-center" defaultSize="64" minSize="35">
            <ResizableGroup id="mission-control.center" orientation="vertical" defaultLayout={{ 'mc-feed': 68, 'mc-command': 32 }}>
              <RegisterGroupReset register={registerInnerReset} />
              <ResizablePanel id="mc-feed" defaultSize="68" minSize="30">
                <div className="h-full overflow-y-auto pb-2 pl-2">{feed}</div>
              </ResizablePanel>
              <ResizableHandle orientation="vertical" before={t('missionControl.feed.title')} after={t('missionControl.command.title')} />
              <ResizablePanel id="mc-command" defaultSize="32" minSize="14">
                <div className="h-full overflow-y-auto pl-2 pt-2">{command}</div>
              </ResizablePanel>
            </ResizableGroup>
          </ResizablePanel>
        </ResizableGroup>
      </div>
    );
  } else if (desktop) {
    const side: McPane = state.pane === 'inspector' ? 'inspector' : 'navigator';
    layout = (
      <div className="h-[max(36rem,calc(100dvh-12.5rem))] min-h-0" data-testid="mc-layout" data-layout="two">
        <ResizableGroup id="mission-control.medium" defaultLayout={{ 'mc-side': 42, 'mc-main': 58 }}>
          <ResizablePanel id="mc-side" defaultSize="42" minSize="28">
            <div className="h-full overflow-y-auto pr-2">
              <Tabs value={side} onValueChange={(v) => update({ pane: v as McPane })}>
                <TabsList label={t('missionControl.panes.label')} variant="pills">
                  <TabsTrigger value="navigator">{t('missionControl.panes.navigator')}</TabsTrigger>
                  <TabsTrigger value="inspector">{inspectorTitle}</TabsTrigger>
                </TabsList>
                <TabsContent value="navigator">{navigator}</TabsContent>
                <TabsContent value="inspector">
                  <section aria-label={inspectorTitle}>{inspector}</section>
                </TabsContent>
              </Tabs>
            </div>
          </ResizablePanel>
          <ResizableHandle before={t('missionControl.panes.navigator')} after={t('missionControl.feed.title')} />
          <ResizablePanel id="mc-main" defaultSize="58" minSize="35">
            <div className="h-full space-y-3 overflow-y-auto pl-2">
              {feed}
              {command}
            </div>
          </ResizablePanel>
        </ResizableGroup>
      </div>
    );
  } else {
    const pane: McPane = state.pane ?? 'feed';
    layout = (
      <div className="flex flex-col gap-3" data-testid="mc-layout" data-layout="one">
        <Tabs value={pane} onValueChange={(v) => update({ pane: v as McPane })}>
          <TabsList label={t('missionControl.panes.label')} variant="pills" className="w-full">
            <TabsTrigger value="navigator">{t('missionControl.panes.navigator')}</TabsTrigger>
            <TabsTrigger value="feed">{t('missionControl.panes.feed')}</TabsTrigger>
            <TabsTrigger value="inspector">{t('missionControl.panes.inspector')}</TabsTrigger>
          </TabsList>
          <TabsContent value="navigator">{navigator}</TabsContent>
          <TabsContent value="feed">{feed}</TabsContent>
          <TabsContent value="inspector">
            <section aria-label={inspectorTitle}>{inspector}</section>
          </TabsContent>
        </Tabs>
        {command}
      </div>
    );
  }

  return (
    <>
      <RunBar
        run={run}
        planVersion={data.planVersion}
        taskTitle={selected?.title ?? null}
        onClearTask={clearTask}
        extra={wide ? <ResetLayoutButton resets={resets} /> : null}
      />
      <p className="sr-only" role="status" aria-live="polite" data-testid="graph-announcement">
        {data.announcement}
      </p>
      {layout}
      {wide ? (
        <ContextPanel id={MC_SCREEN_ID} title={inspectorTitle}>
          {inspector}
        </ContextPanel>
      ) : null}
    </>
  );
}

export default function MissionControlPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [params, setParams] = useInPageSearchParams();
  const state = useMemo(() => readMcState(params), [params]);
  const update = useCallback((patch: Partial<McState>) => setParams(writeMcState({ ...state, ...patch })), [state, setParams]);
  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-3">
      <PageHeading title={t('nav.missionControl')}>
        <ButtonLink to={`/w/${workspaceId}/runs`} icon="workflow">
          {t('missionControl.allRuns')}
        </ButtonLink>
      </PageHeading>
      {state.runId ? (
        <RunWorkspace key={state.runId} workspaceId={workspaceId} role={membership.role} state={state} update={update} />
      ) : (
        <>
          <RunChooser workspaceId={workspaceId} onSelect={(id) => update({ runId: id })} />
          {/* Контекстная панель экрана: без выбранного run — «Нет выбранного контекста». */}
          <ContextPanel id={MC_SCREEN_ID} title={t('missionControl.inspector.runTitle')} />
        </>
      )}
    </section>
  );
}
