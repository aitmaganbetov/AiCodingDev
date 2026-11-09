/**
 * Code Review Workspace (F6, план §3.5, D-4; ADR-0015): `projects/:projectId/review/:runId?task=`.
 *
 * File Explorer изменённых файлов (артефакты git_diff run/задачи → downloadArtifact) · diff (Monaco
 * side-by-side/inline — ленивый чанк; текстовый вид с комментариями под строками) · inline-комментарии B5
 * (тред/ответ/правка своего с историей/resolve; «устарел» относительно commit diff) · находки, тесты и
 * безопасность (D-8) · отправка ревью (Approve — только существующий approval; Request changes — поручение
 * CHANGE_REQUEST с impact preview). Live — `review.comment` и др. из общего потока run. Никаких shell-действий.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { isApiError } from '../../api/client';
import { directiveKeys, directivesApi } from '../../api/directives';
import { findingKeys, findingsApi, type Finding } from '../../api/findings';
import { projectKeys, projectsApi } from '../../api/projects';
import { qualityKeys, qualityApi } from '../../api/quality';
import { reviewKeys, reviewsApi, type ReviewSide, type ReviewThread } from '../../api/reviews';
import type { RunEvent } from '../../api/runEvents';
import { runKeys, runsApi } from '../../api/runs';
import { MAX_INLINE_ARTIFACT_BYTES, artifactDownloadPath, taskKeys, tasksApi } from '../../api/tasks';
import { useRunEvents } from '../../api/useRunEvents';
import { useSession } from '../../auth/session';
import {
  Badge,
  Button,
  ButtonLink,
  Dialog,
  DialogContent,
  DialogTrigger,
  EmptyState,
  ErrorState,
  ForbiddenState,
  Kbd,
  PageHeading,
  SkeletonBlock,
  StatusBadge,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '../../components/ui';
import { ContextPanel } from '../../layout/ContextPanel';
import { MQ_DESKTOP, MQ_REDUCED_MOTION, useMediaQuery } from '../../lib/layoutPrefs';
import { can } from '../../lib/permissions';
import { isTypingTarget, shortcutsEnabled } from '../../lib/shortcuts';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { StreamStatus } from '../runs/StreamStatus';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';
import { useRunDetail } from '../runs/useRunStatus';
import type { MonacoLineSelection } from '../tasks/MonacoDiff';
import { parseUnifiedDiff } from '../tasks/diffModel';
import { CommentBlocked, DiffPane, type DiffMode } from './DiffPane';
import { FileExplorer } from './FileExplorer';
import { NewThreadForm, type ThreadTarget } from './NewThreadForm';
import { ChecksPanel, FindingsPanel, ThreadsSummary } from './ReviewPanels';
import { SubmitReviewPanel } from './SubmitReviewPanel';
import { ThreadCard } from './ThreadCard';
import { anchorKey, cycleIndex, diffEvidence, diffRows, openThreadCount, placeThreads, threadOrder } from './reviewModel';

const PANELS = ['threads', 'findings', 'checks', 'submit'] as const;
type Panel = (typeof PANELS)[number];
const SAMPLE = 200;

function formatBytes(n: number, locale: string): string {
  const units = ['B', 'KB', 'MB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(v)} ${units[i]}`;
}

/** Скелет области с объявлением загрузки для скринридера (скелет сам по себе декоративен). */
function Busy({ label, className = 'h-64' }: { label: string; className?: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      <SkeletonBlock className={`${className} w-full`} />
    </div>
  );
}

interface Draft {
  path: string;
  side: ReviewSide;
  startLine: number;
  endLine: number;
}

export function CodeReviewPage() {
  const { t, i18n } = useTranslation();
  const { projectId = '', runId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const role = membership?.role ?? null;
  const queryClient = useQueryClient();
  const [params, setParams] = useInPageSearchParams();
  const isDesktop = useMediaQuery(MQ_DESKTOP, true);
  const reducedMotion = useMediaQuery(MQ_REDUCED_MOTION);
  const me = useSession().data?.user.id ?? null;
  const enabled = Boolean(membership && runId);
  const canEvidence = can(role, 'evidence:read');
  const canQuality = can(role, 'quality:read');
  const canDownload = can(role, 'artifact:download');
  const canWrite = can(role, 'review:write');
  const canApprovals = can(role, 'approval:read');

  const update = useCallback(
    (patch: Record<string, string | null>) => {
      const next = new URLSearchParams(params);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === '') next.delete(k);
        else next.set(k, v);
      }
      setParams(next);
    },
    [params, setParams],
  );

  // --- данные ---------------------------------------------------------------------------------------
  const { run, isTerminal } = useRunDetail(runId, enabled);
  const project = useQuery({ queryKey: projectKeys.detail(projectId), queryFn: ({ signal }) => projectsApi.get(projectId, signal), enabled });
  const tasks = useQuery({ queryKey: runKeys.tasks(runId), queryFn: ({ signal }) => runsApi.tasks(runId, { limit: 200 }, signal), enabled });
  const evidenceQuery = { limit: SAMPLE };
  const evidence = useQuery({
    queryKey: qualityKeys.runEvidence(runId, evidenceQuery),
    queryFn: ({ signal }) => qualityApi.runEvidence(runId, evidenceQuery, signal),
    enabled: enabled && canEvidence,
  });
  const quality = useQuery({ queryKey: taskKeys.quality(runId), queryFn: ({ signal }) => tasksApi.quality(runId, signal), enabled: enabled && canQuality });
  const taskId = params.get('task') || null;
  const findings = useQuery({
    queryKey: [...findingKeys.all(runId), 'review', taskId ?? ''],
    queryFn: ({ signal }) => findingsApi.list(runId, { limit: SAMPLE, ...(taskId ? { task_id: taskId } : {}) }, signal),
    enabled: enabled && canQuality,
  });
  const worktrees = useQuery({
    queryKey: [...projectKeys.worktrees(projectId, { run_id: runId }), 'review'],
    queryFn: ({ signal }) => projectsApi.worktrees(projectId, { run_id: runId, limit: SAMPLE }, signal),
    enabled,
  });
  const repositories = useQuery({
    queryKey: [...projectKeys.repositories(projectId), 'review'],
    queryFn: ({ signal }) => projectsApi.repositories(projectId, { limit: 50 }, signal),
    enabled,
  });
  const approvals = useQuery({
    queryKey: directiveKeys.approvals(runId),
    queryFn: ({ signal }) => directivesApi.pendingApprovals(workspaceId ?? '', runId, signal),
    enabled: enabled && canApprovals && Boolean(workspaceId),
  });

  const taskList = useMemo(() => tasks.data?.tasks ?? [], [tasks.data]);
  const scopeTask = taskList.find((x) => x.id === taskId) ?? null;
  const diffs = useMemo(() => diffEvidence(evidence.data?.items ?? [], taskId), [evidence.data, taskId]);
  const diff = diffs.find((d) => d.id === params.get('diff')) ?? diffs[0] ?? null;
  const headSha = diff?.commit_sha ?? null;
  const worktree = useMemo(() => {
    const items = worktrees.data?.items ?? [];
    const sameTask = items.filter((w) => (diff?.task_id ? w.task_id === diff.task_id : true));
    return sameTask.find((w) => headSha && w.head_commit_sha === headSha) ?? sameTask[0] ?? null;
  }, [worktrees.data, diff, headSha]);
  const repoItems = repositories.data?.items ?? [];
  const repositoryId = worktree?.repository_id ?? (repoItems.length === 1 ? (repoItems[0]?.id ?? null) : null);
  const baseSha = worktree?.base_commit_sha ?? null;
  const evidenceStatus = useMemo(() => new Map((evidence.data?.items ?? []).map((e) => [e.id, e.verification_status])), [evidence.data]);

  const threadsQuery = { run_id: runId, ...(taskId ? { task_id: taskId } : {}), ...(headSha ? { head_sha: headSha } : {}), limit: SAMPLE };
  const threadsQ = useQuery({
    queryKey: reviewKeys.threads(runId, { ...threadsQuery, repository_id: repositoryId ?? '' }),
    queryFn: ({ signal }) => reviewsApi.listThreads(projectId, repositoryId ?? '', threadsQuery, signal),
    enabled: enabled && Boolean(repositoryId),
  });
  const threads = useMemo(() => threadsQ.data?.items ?? [], [threadsQ.data]);
  const threadById = useMemo(() => new Map(threads.map((th) => [th.id, th])), [threads]);

  const artifact = diff?.artifact ?? null;
  const tooLarge = artifact ? artifact.size_bytes > MAX_INLINE_ARTIFACT_BYTES : false;
  const text = useQuery({
    queryKey: taskKeys.artifact(artifact?.id ?? ''),
    queryFn: ({ signal }) => tasksApi.artifactText(artifact?.id ?? '', signal),
    enabled: Boolean(artifact) && !tooLarge && canDownload,
    staleTime: Infinity,
  });
  const files = useMemo(() => (text.data ? parseUnifiedDiff(text.data) : []), [text.data]);
  const fileParam = params.get('file');
  const fileIndex = Math.max(0, files.findIndex((f) => f.path === fileParam));
  const file = files[fileIndex] ?? null;
  const rows = useMemo(() => (file ? diffRows(file.raw) : []), [file]);
  const fileThreads = useMemo(() => placeThreads(threads, file?.path ?? '', headSha, rows), [threads, file, headSha, rows]);
  const findingsByLine = useMemo(() => {
    const map = new Map<number, Finding[]>();
    for (const f of findings.data?.items ?? []) {
      if (file && f.file_path === file.path && f.line) map.set(f.line, [...(map.get(f.line) ?? []), f]);
    }
    return map;
  }, [findings.data, file]);
  const openCounts = useMemo(() => files.map((f) => openThreadCount(threads.filter((th) => th.anchor.commit_sha === headSha), f.path)), [files, threads, headSha]);
  const navigable = useMemo(() => threadOrder(threads.filter((th) => files.some((f) => f.path === th.anchor.file_path))), [threads, files]);

  // --- вид ------------------------------------------------------------------------------------------
  const modes: DiffMode[] = isDesktop ? ['split', 'inline', 'text'] : ['inline', 'text'];
  const viewParam = params.get('view') as DiffMode | null;
  const mode: DiffMode = viewParam && modes.includes(viewParam) ? viewParam : viewParam === 'split' ? 'inline' : isDesktop ? 'split' : 'text';
  const panelParam = params.get('panel');
  const panel: Panel = (PANELS as readonly string[]).includes(panelParam ?? '') ? (panelParam as Panel) : 'threads';
  const [draft, setDraft] = useState<Draft | null>(null);
  const [selection, setSelection] = useState<{ path: string; sel: MonacoLineSelection } | null>(null);
  const [focus, setFocus] = useState<{ id: string; nonce: number } | null>(null);
  const [reveal, setReveal] = useState<{ side: ReviewSide; line: number; nonce: number } | null>(null);
  const [filesOpen, setFilesOpen] = useState(false);
  const [announcement, setAnnouncement] = useState('');
  const lastFocused = useRef<string | null>(null);

  const selectFile = useCallback(
    (index: number) => {
      const f = files[index];
      if (!f) return;
      setDraft(null);
      update({ file: f.path });
    },
    [files, update],
  );

  const openThread = useCallback(
    (th: ReviewThread) => {
      const other = th.anchor.commit_sha !== headSha ? diffs.find((d) => d.commit_sha === th.anchor.commit_sha) : undefined;
      update({ file: th.anchor.file_path, ...(other ? { diff: other.id } : {}) });
      lastFocused.current = th.id;
      setFocus({ id: th.id, nonce: Date.now() });
      setReveal({ side: th.anchor.side, line: th.anchor.end_line, nonce: Date.now() });
    },
    [diffs, headSha, update],
  );

  const stepThread = useCallback(
    (delta: 1 | -1) => {
      if (navigable.length === 0) return;
      const index = cycleIndex(navigable.length, navigable.findIndex((th) => th.id === lastFocused.current), delta);
      const th = navigable[index];
      if (th) openThread(th);
    },
    [navigable, openThread],
  );

  // Фокус на тред после перехода (файл мог смениться — ждём отрисовки).
  useEffect(() => {
    if (!focus) return undefined;
    const frame = requestAnimationFrame(() => {
      const el = document.getElementById(`thread-${focus.id}`);
      if (!el) return;
      el.focus({ preventScroll: true });
      el.scrollIntoView({ block: 'center', behavior: reducedMotion ? 'auto' : 'smooth' });
    });
    return () => cancelAnimationFrame(frame);
  }, [focus, file, mode, threads, reducedMotion]);

  // Сочетания экрана: ] / [ — файл, n / p — обсуждение (не в полях ввода; отключаются вместе с остальными, WCAG 2.1.4).
  useEffect(() => {
    let lastG = 0;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || !shortcutsEnabled() || isTypingTarget(e.target)) return;
      if (e.key === 'g') {
        lastG = Date.now();
        return;
      }
      // «g, затем буква» — переход оболочки, не наше сочетание.
      if (Date.now() - lastG < 1500) {
        lastG = 0;
        return;
      }
      const action =
        e.key === ']' ? () => selectFile(cycleIndex(files.length, fileIndex, 1)) : e.key === '[' ? () => selectFile(cycleIndex(files.length, fileIndex, -1)) : e.key === 'n' ? () => stepThread(1) : e.key === 'p' ? () => stepThread(-1) : null;
      if (!action) return;
      e.preventDefault();
      action();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [files.length, fileIndex, selectFile, stepThread]);

  // --- live ----------------------------------------------------------------------------------------
  const onEvents = useCallback(
    (events: RunEvent[]) => {
      const has = (p: (type: string) => boolean) => events.some((e) => p(e.type));
      const review = events.filter((e) => e.type === 'review.comment');
      if (review.length > 0) {
        void queryClient.invalidateQueries({ queryKey: reviewKeys.threadsOfRun(runId) });
        void queryClient.invalidateQueries({ queryKey: reviewKeys.submissions(runId) });
        const foreign = review.filter((e) => e.payload.actor_id !== me);
        const last = foreign.at(-1);
        if (last) {
          const p = last.payload as { action?: string; file_path?: string; start_line?: number };
          setAnnouncement(t(`review.live.${p.action ?? 'comment_created'}`, { defaultValue: t('review.live.comment_created'), path: `${p.file_path ?? ''}:${p.start_line ?? ''}` }));
        }
      }
      if (has((x) => x.startsWith('approval.'))) void queryClient.invalidateQueries({ queryKey: directiveKeys.approvals(runId) });
      if (has((x) => x.startsWith('directive.'))) void queryClient.invalidateQueries({ queryKey: reviewKeys.submissions(runId) });
      if (has((x) => x === 'gate.completed')) {
        void queryClient.invalidateQueries({ queryKey: taskKeys.quality(runId) });
        void queryClient.invalidateQueries({ queryKey: qualityKeys.runEvidence(runId, evidenceQuery) });
        void queryClient.invalidateQueries({ queryKey: findingKeys.all(runId) });
      }
      if (has((x) => x === 'run.state_changed' || x === 'run.completed')) void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId), exact: true });
      if (has((x) => x === 'task.state_changed')) void queryClient.invalidateQueries({ queryKey: runKeys.tasks(runId) });
    },
    // evidenceQuery — константа по содержимому.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [queryClient, runId, me, t],
  );
  const stream = useRunEvents(runId, { onEvents, isTerminal });

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const base = `/w/${workspaceId}`;
  const mcHref = `${base}/mission-control?${new URLSearchParams({ run: runId, ...(taskId ? { task: taskId } : {}) }).toString()}`;

  // --- состояние run -------------------------------------------------------------------------------
  const heading = (
    <>
      <nav aria-label={t('review.breadcrumbs')} className="mb-2 text-sm">
        <ol className="flex flex-wrap items-center gap-1 text-fg-muted">
          <li>
            <Link className="text-link underline" to={`${base}/projects`}>
              {t('nav.projects')}
            </Link>
          </li>
          <li aria-hidden="true">/</li>
          <li>
            <Link className="break-all text-link underline" to={`${base}/projects/${projectId}`}>
              {project.data?.name ?? t('projects.detail.titleFallback')}
            </Link>
          </li>
          <li aria-hidden="true">/</li>
          <li aria-current="page">{t('review.title')}</li>
        </ol>
      </nav>
      <PageHeading title={t('review.title')}>
        <div className="flex flex-wrap gap-2">
          <ButtonLink to={mcHref} variant="secondary" icon="missionControl">
            {t('review.openMissionControl')}
          </ButtonLink>
          {taskId ? (
            <ButtonLink to={`${base}/projects/${projectId}/runs/${runId}/tasks/${taskId}`} variant="ghost">
              {t('review.openTask')}
            </ButtonLink>
          ) : null}
        </div>
      </PageHeading>
    </>
  );

  if (run.run.isPending) {
    return (
      <section aria-labelledby="page-title" aria-busy="true">
        {heading}
        <Busy label={t('review.loading')} className="h-24" />
      </section>
    );
  }
  if (run.run.isError) {
    const notFound = isApiError(run.run.error) && run.run.error.status === 404;
    return (
      <section aria-labelledby="page-title">
        {heading}
        {notFound ? (
          <EmptyState title={t('review.notFoundTitle')} description={t('review.notFound')} />
        ) : (
          <ErrorState error={run.run.error} onRetry={() => void run.run.refetch()} />
        )}
      </section>
    );
  }
  const runData = run.run.data;

  // --- diff ----------------------------------------------------------------------------------------
  const commentReason = !canWrite
    ? t('review.diff.readOnly')
    : !headSha
      ? t('review.diff.noCommit')
      : repositories.isPending || worktrees.isPending
        ? null
        : !repositoryId
          ? t('review.diff.noRepository')
          : null;
  const canComment = canWrite && Boolean(headSha) && Boolean(repositoryId);
  const target: ThreadTarget | null =
    file && canComment && repositoryId && headSha
      ? { projectId, repositoryId, runId, taskId: diff?.task_id ?? taskId, path: file.path, headSha, baseSha }
      : null;
  const draftHere = draft && file && draft.path === file.path ? draft : null;
  const form =
    draftHere && target ? (
      <NewThreadForm
        target={target}
        initial={{ side: draftHere.side, startLine: draftHere.startLine, endLine: draftHere.endLine }}
        onCancel={() => setDraft(null)}
        onDone={(th) => {
          setDraft(null);
          setAnnouncement(t('review.form.created', { path: `${th.anchor.file_path}:${th.anchor.start_line}` }));
          lastFocused.current = th.id;
          setFocus({ id: th.id, nonce: Date.now() });
        }}
      />
    ) : null;
  const renderThread = (id: string) => {
    const th = threadById.get(id);
    if (!th) return null;
    return (
      <ThreadCard
        thread={th}
        canWrite={canWrite}
        highlighted={focus?.id === id}
        findingHref={th.finding_id ? `${base}/quality/findings/${th.finding_id}` : undefined}
      />
    );
  };

  const explorer = (
    <FileExplorer
      files={files}
      selected={fileIndex}
      openCounts={openCounts}
      onSelect={(i) => {
        selectFile(i);
        setFilesOpen(false);
      }}
    />
  );

  const navToolbar = (
    <div className="flex flex-wrap items-center gap-1" role="group" aria-label={t('review.nav.label')}>
      <Button size="sm" variant="ghost" icon="chevronUp" aria-keyshortcuts="[" onClick={() => selectFile(cycleIndex(files.length, fileIndex, -1))}>
        {t('review.nav.prevFile')}
      </Button>
      <Button size="sm" variant="ghost" icon="chevronDown" aria-keyshortcuts="]" onClick={() => selectFile(cycleIndex(files.length, fileIndex, 1))}>
        {t('review.nav.nextFile')}
      </Button>
      <Button size="sm" variant="ghost" icon="chevronLeft" aria-keyshortcuts="p" disabled={navigable.length === 0} onClick={() => stepThread(-1)}>
        {t('review.nav.prevThread')}
      </Button>
      <Button size="sm" variant="ghost" icon="chevronRight" aria-keyshortcuts="n" disabled={navigable.length === 0} onClick={() => stepThread(1)}>
        {t('review.nav.nextThread')}
      </Button>
    </div>
  );

  let diffBody;
  if (!canEvidence) diffBody = <ForbiddenState description={t('review.forbiddenEvidence')} />;
  else if (evidence.isPending) diffBody = <Busy label={t('review.loadingDiff')} />;
  else if (evidence.isError) diffBody = <ErrorState error={evidence.error} onRetry={() => void evidence.refetch()} />;
  else if (!diff) {
    diffBody = (
      <EmptyState
        headingLevel={2}
        icon="gitBranch"
        title={t('review.emptyTitle')}
        description={taskId ? t('review.emptyTask') : t('review.emptyRun')}
        action={
          <ButtonLink to={mcHref} variant="secondary">
            {t('review.openMissionControl')}
          </ButtonLink>
        }
      />
    );
  } else if (!canDownload) diffBody = <ForbiddenState description={t('tasks.diff.noDownloadPermission')} />;
  else if (tooLarge) {
    diffBody = (
      <EmptyState
        headingLevel={2}
        title={t('review.tooLargeTitle')}
        description={t('tasks.diff.tooLarge', { size: formatBytes(MAX_INLINE_ARTIFACT_BYTES, i18n.language) })}
        action={
          <a className="link" href={artifactDownloadPath(diff.artifact.id)} download>
            {t('tasks.diff.download')}
          </a>
        }
      />
    );
  } else if (text.isPending) diffBody = <Busy label={t('review.loadingDiff')} />;
  else if (text.isError) diffBody = <ErrorState error={text.error} onRetry={() => void text.refetch()} title={t('tasks.diff.loadError')} />;
  else if (!file) diffBody = <EmptyState headingLevel={2} title={t('review.unparsableTitle')} description={t('tasks.diff.unparsable')} />;
  else {
    diffBody = (
      <DiffPane
        file={file}
        modelKey={`${diff.artifact.id}/${fileIndex}/${file.path}`}
        rows={rows}
        threads={fileThreads}
        findingsByLine={findingsByLine}
        mode={mode}
        modes={modes}
        onMode={(m) => update({ view: m })}
        canComment={canComment}
        commentBlocked={commentReason ? <CommentBlocked reason={commentReason} /> : null}
        draftKey={draftHere ? anchorKey(draftHere.side, draftHere.endLine) : null}
        form={form}
        renderThread={renderThread}
        onComment={(side, start, end) => setDraft({ path: file.path, side, startLine: start, endLine: end ?? start })}
        selection={selection && selection.path === file.path ? selection.sel : null}
        onSelection={(sel) => setSelection({ path: file.path, sel })}
        reveal={reveal}
        toolbar={navToolbar}
      />
    );
  }

  const threadsInScope = taskId ? threads.filter((th) => th.task_id === taskId || th.task_id === null) : threads;
  const openCount = threadsInScope.filter((th) => th.status === 'OPEN').length;
  const findingItems = findings.data?.items ?? [];
  const gates = quality.data?.gate_executions ?? [];

  const side = (
    <Tabs value={panel} onValueChange={(v) => update({ panel: v === 'threads' ? null : v })}>
      <TabsList label={t('review.panels.label')} variant="pills">
        <TabsTrigger value="threads">{t('review.panels.threads', { count: openCount })}</TabsTrigger>
        <TabsTrigger value="findings">{t('review.panels.findings', { count: findingItems.length })}</TabsTrigger>
        <TabsTrigger value="checks">{t('review.panels.checks')}</TabsTrigger>
        <TabsTrigger value="submit">{t('review.panels.submit')}</TabsTrigger>
      </TabsList>
      <TabsContent value="threads">
        {!repositoryId && !repositories.isPending && !worktrees.isPending ? (
          <p className="text-sm text-fg-muted">{t('review.diff.noRepository')}</p>
        ) : threadsQ.isPending && repositoryId ? (
          <Busy label={t('review.loading')} className="h-24" />
        ) : threadsQ.isError ? (
          <ErrorState error={threadsQ.error} onRetry={() => void threadsQ.refetch()} />
        ) : (
          <ThreadsSummary threads={threadsInScope} onOpen={openThread} />
        )}
      </TabsContent>
      <TabsContent value="findings">
        {!canQuality ? (
          <ForbiddenState />
        ) : findings.isPending ? (
          <Busy label={t('review.loading')} className="h-24" />
        ) : findings.isError ? (
          <ErrorState error={findings.error} onRetry={() => void findings.refetch()} />
        ) : (
          <FindingsPanel
            findings={findingItems}
            gates={gates}
            evidenceStatus={evidenceStatus}
            workspaceId={workspaceId}
            onOpenLine={(path, line) => {
              if (!files.some((f) => f.path === path)) return;
              update({ file: path, view: mode === 'text' ? 'text' : mode });
              setReveal({ side: 'head', line, nonce: Date.now() });
            }}
          />
        )}
      </TabsContent>
      <TabsContent value="checks">
        {!canQuality ? (
          <ForbiddenState />
        ) : quality.isPending ? (
          <Busy label={t('review.loading')} className="h-24" />
        ) : quality.isError ? (
          <ErrorState error={quality.error} onRetry={() => void quality.refetch()} />
        ) : (
          <div className="flex flex-col gap-4">
            <ChecksPanel category="tests" gates={gates} taskId={taskId} evidenceStatus={evidenceStatus} workspaceId={workspaceId} />
            <ChecksPanel category="security" gates={gates} taskId={taskId} evidenceStatus={evidenceStatus} workspaceId={workspaceId} />
            <Link className="text-sm text-link underline" to={`${base}/quality/runs/${runId}`}>
              {t('review.checks.openQuality')}
            </Link>
          </div>
        )}
      </TabsContent>
      <TabsContent value="submit">
        <SubmitReviewPanel
          run={runData}
          role={membership.role}
          workspaceId={workspaceId}
          scopeTask={scopeTask ?? (diff?.task_id ? (taskList.find((x) => x.id === diff.task_id) ?? null) : null)}
          threads={threadsInScope}
          approvals={{ isPending: approvals.isPending && canApprovals, isError: approvals.isError, error: approvals.error, data: approvals.data?.items, refetch: approvals.refetch }}
        />
      </TabsContent>
    </Tabs>
  );

  const summary = (
    <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-sm" data-testid="review-summary">
      <dt className="text-fg-muted">{t('review.summary.run')}</dt>
      <dd>
        <StatusBadge kind="run" status={runData.status} />
      </dd>
      <dt className="text-fg-muted">{t('review.summary.scope')}</dt>
      <dd className="break-words">{scopeTask ? scopeTask.title : t('review.scope.run')}</dd>
      <dt className="text-fg-muted">{t('review.summary.commit')}</dt>
      <dd>{headSha ? <code title={headSha}>{headSha.slice(0, 12)}</code> : t('review.summary.unknown')}</dd>
      <dt className="text-fg-muted">{t('review.summary.base')}</dt>
      <dd>{baseSha ? <code title={baseSha}>{baseSha.slice(0, 12)}</code> : t('review.summary.unknown')}</dd>
      <dt className="text-fg-muted">{t('review.summary.branch')}</dt>
      <dd className="break-all font-mono text-xs">{worktree?.branch_name ?? t('review.summary.unknown')}</dd>
      <dt className="text-fg-muted">{t('review.summary.threads')}</dt>
      <dd>{t('review.summary.threadsValue', { open: openCount, total: threadsInScope.length })}</dd>
      <dt className="text-fg-muted">{t('review.summary.approvals')}</dt>
      <dd>{approvals.data ? approvals.data.items.length : t('review.summary.unknown')}</dd>
    </dl>
  );

  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4">
      {heading}
      <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-3" data-testid="review-run-bar">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-fg-muted">{t('review.runStatus')}</span>
          <StatusBadge kind="run" status={runData.status} />
          <Badge tone="neutral">{t(`enums.run_mode.${runData.mode}`, { defaultValue: runData.mode })}</Badge>
          <span className="min-w-0 break-words font-medium">{runData.objective}</span>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="review-scope" className="text-xs font-medium">
              {t('review.scope.label')}
            </label>
            <select
              id="review-scope"
              className="select min-h-touch max-w-full sm:max-w-[22rem]"
              value={taskId ?? ''}
              onChange={(e) => {
                setDraft(null);
                update({ task: e.target.value || null, diff: null, file: null });
              }}
            >
              <option value="">{t('review.scope.run')}</option>
              {taskList.map((x) => (
                <option key={x.id} value={x.id}>
                  {x.title}
                </option>
              ))}
            </select>
          </div>
          {diffs.length > 1 ? (
            <div className="flex min-w-0 flex-col gap-1">
              <label htmlFor="review-diff" className="text-xs font-medium">
                {t('review.diffSelect')}
              </label>
              <select
                id="review-diff"
                className="select min-h-touch max-w-full sm:max-w-[26rem]"
                value={diff?.id ?? ''}
                onChange={(e) => {
                  setDraft(null);
                  update({ diff: e.target.value, file: null });
                }}
              >
                {diffs.map((d) => (
                  <option key={d.id} value={d.id}>
                    {t('review.diffOption', {
                      task: taskList.find((x) => x.id === d.task_id)?.title ?? t('review.scope.run'),
                      commit: d.commit_sha?.slice(0, 7) ?? '—',
                    })}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          {!isDesktop && files.length > 0 ? (
            <Dialog open={filesOpen} onOpenChange={setFilesOpen}>
              <DialogTrigger asChild>
                <Button icon="folder">{t('review.files.open_drawer', { count: files.length })}</Button>
              </DialogTrigger>
              <DialogContent variant="drawer-left" title={t('review.files.title')}>
                {explorer}
              </DialogContent>
            </Dialog>
          ) : null}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <StreamStatus state={stream.state} error={stream.error} onReconnect={stream.reconnect} />
          <p role="status" aria-live="polite" className="text-sm text-fg-muted" data-testid="review-live">
            {announcement}
          </p>
        </div>
        <p className="text-xs text-fg-muted">
          {t('review.keys.prefix')} <Kbd>[</Kbd> <Kbd>]</Kbd> {t('review.keys.files')} · <Kbd>p</Kbd> <Kbd>n</Kbd> {t('review.keys.threads')} ·{' '}
          {t('review.keys.comment')}
        </p>
      </div>

      <div className="grid min-w-0 gap-4 md:grid-cols-[14rem,minmax(0,1fr)] xl:grid-cols-[16rem,minmax(0,1fr),24rem]">
        {isDesktop ? <div className="min-w-0">{files.length > 0 ? explorer : null}</div> : null}
        <div className="min-w-0">{diffBody}</div>
        <aside aria-label={t('review.panels.label')} className="min-w-0 md:col-span-2 xl:col-span-1">
          {side}
        </aside>
      </div>
      <ContextPanel id="code-review" title={t('review.summary.title')}>
        {summary}
      </ContextPanel>
    </section>
  );
}

export default CodeReviewPage;
