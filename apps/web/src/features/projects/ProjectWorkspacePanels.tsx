/**
 * Вкладки рабочего пространства проекта (F6, план §3.5): обзор (режимы, AI-исполнители, политика качества),
 * файлы и изменения (Repository Explorer с выбором commit), ветки, worktrees (B4, без путей хоста), запуски
 * (переходы в Mission Control и Code Review). Только чтение реальных операций контракта; нет данных — явное
 * состояние, а не нули.
 */
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useId, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { projectKeys, projectsApi, type Project, type Repository, type Worktree, type WorktreeStatus } from '../../api/projects';
import { runKeys, runsApi, type RunSummary } from '../../api/runs';
import {
  Badge,
  ButtonLink,
  Callout,
  Card,
  EmptyState,
  ErrorState,
  LoadMore,
  LoadingState,
  StatusBadge,
  Table,
  Timestamp,
  type Column,
} from '../../components/ui';
import { useProvidersStatus } from '../../layout/shellData';
import { can } from '../../lib/permissions';
import { providerLabel, roleLabel } from '../runs/labels';
import { RepositoryExplorer } from './RepositoryExplorer';

export const RUNS_TAB_PAGE = 20;
export const WORKTREES_PAGE = 50;
const WORKTREE_STATUSES: readonly WorktreeStatus[] = ['ACTIVE', 'LOCKED', 'REMOVED', 'ORPHANED'];
const RUN_MODES = ['DEVELOP', 'CREATE', 'DEBUG', 'AUDIT', 'MODERNIZE', 'SECURITY', 'OPTIMIZE', 'TEST', 'MAINTAIN', 'DOCUMENT', 'REVIEW'] as const;

export function reviewPath(workspaceId: string, projectId: string, runId: string, taskId?: string | null): string {
  return `/w/${workspaceId}/projects/${projectId}/review/${runId}${taskId ? `?${new URLSearchParams({ task: taskId }).toString()}` : ''}`;
}

export function missionControlPath(workspaceId: string, runId: string, taskId?: string | null): string {
  return `/w/${workspaceId}/mission-control?${new URLSearchParams({ run: runId, ...(taskId ? { task: taskId } : {}) }).toString()}`;
}

/** Репозитории проекта одним списком (≤ 50, как привязывает UI) — для выбора во вкладках. */
export function useProjectRepositories(projectId: string) {
  return useQuery({
    queryKey: [...projectKeys.repositories(projectId), 'all'],
    queryFn: ({ signal }) => projectsApi.repositories(projectId, { limit: 50 }, signal),
  });
}

function repoName(r: Repository): string {
  return r.provider === 'github' ? (r.remote_url ?? r.id) : (r.local_root_ref ?? r.id);
}

function Hash({ sha }: { sha: string | null | undefined }) {
  const { t } = useTranslation();
  if (!sha) return <span className="text-fg-muted">{t('projects.workspace.none')}</span>;
  return (
    <code className="font-mono text-xs" title={sha}>
      {sha.slice(0, 12)}
    </code>
  );
}

function RepoPicker({ repos, value, onChange, label }: { repos: readonly Repository[]; value: string; onChange: (id: string) => void; label: string }) {
  const id = useId();
  if (repos.length <= 1) return null;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <select id={id} className="select min-h-touch max-w-full sm:max-w-[28rem]" value={value} onChange={(e) => onChange(e.target.value)}>
        {repos.map((r) => (
          <option key={r.id} value={r.id}>
            {repoName(r)}
          </option>
        ))}
      </select>
    </div>
  );
}

function RepositoriesGate({ projectId, children }: { projectId: string; children: (repos: Repository[]) => ReactNode }) {
  const { t } = useTranslation();
  const query = useProjectRepositories(projectId);
  if (query.isPending) return <LoadingState label={t('projects.repo.loading')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (query.data.items.length === 0) {
    return <EmptyState headingLevel={3} icon="layers" title={t('projects.repo.emptyTitle')} description={t('projects.workspace.attachFirst')} />;
  }
  return <>{children(query.data.items)}</>;
}

// ---------------------------------------------------------------------------
// Обзор
// ---------------------------------------------------------------------------

export function ProjectOverviewPanel({ project, workspaceId, role }: { project: Project; workspaceId: string; role: string }) {
  const { t } = useTranslation();
  const s = project.settings;
  const repos = useProjectRepositories(project.id);
  const providers = useProvidersStatus(workspaceId, role);
  const runs = useQuery({
    queryKey: [...runKeys.list(workspaceId, project.id), 'overview'],
    queryFn: ({ signal }) => runsApi.list(workspaceId, { project_id: project.id, limit: 5 }, signal),
  });
  const runsHref = `/w/${workspaceId}/projects/${project.id}/runs`;
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[repeat(2,minmax(0,1fr))]">
      <Card as="section" id="project-overview" title={t('projects.detail.overview')} headingLevel={2}>
        {project.description ? <p className="mb-3 whitespace-pre-line break-words">{project.description}</p> : null}
        <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-fg-muted">{t('projects.status')}</dt>
          <dd>{t(`enums.project_status.${project.status}`, { defaultValue: project.status })}</dd>
          <dt className="text-fg-muted">{t('projects.documentationMode')}</dt>
          <dd>
            {t(`enums.documentation_mode.${s.documentation_mode}`, { defaultValue: s.documentation_mode })}
            <span className="block text-xs text-fg-muted">{t(`projects.documentationModeHint.${s.documentation_mode}`, { defaultValue: '' })}</span>
          </dd>
          <dt className="text-fg-muted">{t('common.createdAt')}</dt>
          <dd>
            <Timestamp value={project.created_at} />
          </dd>
          <dt className="text-fg-muted">{t('projects.detail.updatedAt')}</dt>
          <dd>
            <Timestamp value={project.updated_at} />
          </dd>
          <dt className="text-fg-muted">{t('projects.workspace.repositories')}</dt>
          <dd data-testid="overview-repositories">
            {repos.isPending
              ? t('projects.workspace.loading')
              : repos.isError
                ? t('projects.workspace.unavailable')
                : t('projects.workspace.repositoriesCount', { count: repos.data.items.length })}
          </dd>
        </dl>
      </Card>

      <Card as="section" id="project-quality" title={t('projects.workspace.qualityTitle')} headingLevel={2}>
        <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
          <dt className="text-fg-muted">{t('projects.qualityProfile')}</dt>
          <dd data-testid="overview-quality-profile">
            <Badge tone="info">{t(`enums.quality_profile.${s.quality_profile}`, { defaultValue: s.quality_profile })}</Badge>
            <span className="block text-xs text-fg-muted">{t(`projects.qualityProfileHint.${s.quality_profile}`, { defaultValue: '' })}</span>
          </dd>
          <dt className="text-fg-muted">{t('projects.workspace.securityProfile')}</dt>
          <dd>{s.security_profile ?? t('projects.workspace.notSet')}</dd>
          <dt className="text-fg-muted">{t('projects.workspace.routingPolicy')}</dt>
          <dd>{s.routing_policy_id ? <Hash sha={s.routing_policy_id} /> : t('projects.workspace.routingDefault')}</dd>
        </dl>
        <p className="mt-3 text-xs text-fg-muted">{t('projects.workspace.qualityHint')}</p>
        <div className="mt-2 flex flex-wrap gap-3 text-sm">
          <Link className="text-link underline" to={`/w/${workspaceId}/compliance/projects/${project.id}/controls`}>
            {t('projects.workspace.controlsLink')}
          </Link>
          <Link className="text-link underline" to={`/w/${workspaceId}/quality`}>
            {t('projects.workspace.qualityLink')}
          </Link>
        </div>
      </Card>

      <Card as="section" id="project-modes" title={t('projects.workspace.modesTitle')} headingLevel={2}>
        <p className="text-sm text-fg-muted">{t('projects.workspace.modesHint')}</p>
        <ul className="mt-2 flex flex-wrap gap-1" aria-label={t('projects.workspace.modesTitle')}>
          {RUN_MODES.map((m) => (
            <li key={m}>
              <Badge tone="neutral">{t(`enums.run_mode.${m}`, { defaultValue: m })}</Badge>
            </li>
          ))}
        </ul>
        {can(role, 'run:create') ? (
          <div className="mt-3">
            <ButtonLink to={runsHref} variant="secondary" icon="play">
              {t('projects.workspace.newRun')}
            </ButtonLink>
          </div>
        ) : null}
      </Card>

      <Card
        as="section"
        id="project-executors"
        title={t('projects.workspace.executorsTitle')}
        headingLevel={2}
        actions={
          <Link className="text-sm text-link underline" to={`/w/${workspaceId}/settings/providers`}>
            {t('projects.workspace.providersLink')}
          </Link>
        }
      >
        <p className="mb-2 text-xs text-fg-muted">{t('projects.workspace.executorsHint')}</p>
        {!can(role, 'provider:read') ? (
          <p className="text-sm text-fg-muted">{t('state.forbiddenDescription')}</p>
        ) : providers.isPending ? (
          <LoadingState inline label={t('projects.workspace.loading')} />
        ) : providers.isError ? (
          <ErrorState error={providers.error} onRetry={() => void providers.refetch()} />
        ) : providers.data.items.length === 0 ? (
          <p className="text-sm text-fg-muted">{t('projects.workspace.noProviders')}</p>
        ) : (
          <ul className="flex flex-col gap-2" data-testid="overview-executors">
            {providers.data.items.map((p) => {
              const ws = p.workspace_settings;
              const on = p.enabled_globally && ws.enabled;
              return (
                <li key={p.provider_id} className="rounded-md border border-border p-2 text-sm">
                  <p className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">{p.display_name || providerLabel(t, p.code)}</span>
                    <Badge tone={on ? 'success' : 'neutral'}>{on ? t('projects.workspace.providerOn') : t('projects.workspace.providerOff')}</Badge>
                  </p>
                  <p className="text-xs text-fg-muted">
                    {t('projects.workspace.providerLimits', { parallel: ws.max_parallel_sessions, timeout: Math.round(ws.default_timeout_seconds / 60) })}
                  </p>
                  <p className="text-xs text-fg-muted">
                    {ws.allowed_roles.length === 0
                      ? t('projects.workspace.rolesNone')
                      : t('projects.workspace.roles', { roles: ws.allowed_roles.map((r) => roleLabel(t, r)).join(', ') })}
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card
        as="section"
        id="project-recent-runs"
        title={t('projects.workspace.recentRuns')}
        headingLevel={2}
        className="lg:col-span-2"
        actions={
          <Link className="text-sm text-link underline" to={runsHref}>
            {t('projects.workspace.allRuns')}
          </Link>
        }
      >
        {runs.isPending ? (
          <LoadingState inline label={t('projects.workspace.loading')} />
        ) : runs.isError ? (
          <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
        ) : runs.data.items.length === 0 ? (
          <p className="text-sm text-fg-muted">{t('projects.workspace.noRuns')}</p>
        ) : (
          <RunsTable runs={runs.data.items} workspaceId={workspaceId} projectId={project.id} caption={t('projects.workspace.recentRuns')} />
        )}
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Запуски
// ---------------------------------------------------------------------------

function RunsTable({ runs, workspaceId, projectId, caption }: { runs: readonly RunSummary[]; workspaceId: string; projectId: string; caption: string }) {
  const { t } = useTranslation();
  const columns: Column<RunSummary>[] = [
    { id: 'status', header: t('projects.workspace.col.status'), cell: (r) => <StatusBadge kind="run" status={r.status} />, sortValue: (r) => r.status },
    { id: 'mode', header: t('projects.workspace.col.mode'), cell: (r) => t(`enums.run_mode.${r.mode}`, { defaultValue: r.mode }) },
    {
      id: 'objective',
      header: t('projects.workspace.col.objective'),
      rowHeader: true,
      cell: (r) => <span className="line-clamp-2 whitespace-pre-line break-words">{r.objective}</span>,
    },
    { id: 'created', header: t('projects.workspace.col.created'), cell: (r) => <Timestamp value={r.created_at} />, sortValue: (r) => r.created_at },
    {
      id: 'actions',
      header: t('projects.workspace.col.actions'),
      cell: (r) => (
        <span className="flex flex-wrap gap-x-3 gap-y-1">
          <Link className="text-link underline" to={missionControlPath(workspaceId, r.id)}>
            {t('projects.workspace.openMc')}
            <span className="sr-only">: {r.objective.slice(0, 60)}</span>
          </Link>
          <Link className="text-link underline" to={reviewPath(workspaceId, projectId, r.id)}>
            {t('projects.workspace.openReview')}
            <span className="sr-only">: {r.objective.slice(0, 60)}</span>
          </Link>
        </span>
      ),
    },
  ];
  return <Table caption={caption} columns={columns} rows={runs} rowKey={(r) => r.id} density="compact" />;
}

export function ProjectRunsPanel({ workspaceId, projectId, role }: { workspaceId: string; projectId: string; role: string }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: [...runKeys.list(workspaceId, projectId), 'tab'],
    queryFn: ({ pageParam, signal }) =>
      runsApi.list(workspaceId, { project_id: projectId, limit: RUNS_TAB_PAGE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <section aria-labelledby="project-runs-title" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="project-runs-title" className="text-lg font-semibold">
          {t('projects.workspace.runsTitle')}
        </h2>
        {can(role, 'run:create') ? (
          <ButtonLink to={`/w/${workspaceId}/projects/${projectId}/runs`} variant="secondary" icon="play">
            {t('projects.workspace.newRun')}
          </ButtonLink>
        ) : null}
      </div>
      <p className="text-sm text-fg-muted">{t('projects.workspace.runsHint')}</p>
      {query.isPending ? (
        <LoadingState label={t('projects.workspace.loading')} />
      ) : query.isError && items.length === 0 ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState headingLevel={3} icon="play" title={t('projects.workspace.noRunsTitle')} description={t('projects.workspace.noRuns')} />
      ) : (
        <>
          <RunsTable runs={items} workspaceId={workspaceId} projectId={projectId} caption={t('projects.workspace.runsTitle')} />
          <LoadMore
            shown={items.length}
            hasNextPage={query.hasNextPage}
            isFetchingNextPage={query.isFetchingNextPage}
            nextPageError={query.isFetchNextPageError ? query.error : null}
            onLoadMore={() => void query.fetchNextPage()}
          />
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Ветки
// ---------------------------------------------------------------------------

function BranchesTable({ projectId, repo, onOpenFiles }: { projectId: string; repo: Repository; onOpenFiles: (repoId: string, branch: string) => void }) {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: projectKeys.branches(projectId, repo.id),
    queryFn: ({ signal }) => projectsApi.branches(projectId, repo.id, { limit: 200 }, signal),
    staleTime: 30_000,
  });
  if (!repo.local_root_ref) {
    return (
      <Callout tone="neutral" title={t('projects.explorer.unavailableTitle')}>
        {t('projects.workspace.branchesNoLocal')}
      </Callout>
    );
  }
  if (query.isPending) return <LoadingState label={t('projects.workspace.loading')} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  const columns: Column<(typeof query.data.items)[number]>[] = [
    {
      id: 'name',
      header: t('projects.workspace.col.branch'),
      rowHeader: true,
      sortValue: (b) => b.name,
      cell: (b) => (
        <span className="flex flex-wrap items-center gap-2">
          <span className="break-all font-mono">{b.name}</span>
          {b.is_default ? <Badge tone="info">{t('projects.workspace.defaultBranch')}</Badge> : null}
        </span>
      ),
    },
    { id: 'commit', header: t('projects.workspace.col.commit'), cell: (b) => <Hash sha={b.commit_sha} /> },
    {
      id: 'committed',
      header: t('projects.workspace.col.committed'),
      sortValue: (b) => b.committed_at ?? '',
      cell: (b) => (b.committed_at ? <Timestamp value={b.committed_at} /> : t('projects.workspace.none')),
    },
    {
      id: 'actions',
      header: t('projects.workspace.col.actions'),
      cell: (b) => (
        <button type="button" className="min-h-target text-link underline" onClick={() => onOpenFiles(repo.id, b.name)}>
          {t('projects.workspace.openFiles')}
          <span className="sr-only">: {b.name}</span>
        </button>
      ),
    },
  ];
  return (
    <div className="flex flex-col gap-2">
      <Table caption={t('projects.workspace.branchesCaption', { repo: repoName(repo) })} columns={columns} rows={query.data.items} rowKey={(b) => b.name} density="compact" empty={t('projects.workspace.noBranches')} />
      {query.data.truncated || query.data.next_cursor ? <p className="text-xs text-fg-muted">{t('projects.workspace.branchesTruncated')}</p> : null}
    </div>
  );
}

export function BranchesPanel({ projectId, onOpenFiles }: { projectId: string; onOpenFiles: (repoId: string, branch: string) => void }) {
  const { t } = useTranslation();
  const [repoId, setRepoId] = useState('');
  return (
    <section aria-labelledby="project-branches-title" className="flex flex-col gap-3">
      <h2 id="project-branches-title" className="text-lg font-semibold">
        {t('projects.workspace.branchesTitle')}
      </h2>
      <RepositoriesGate projectId={projectId}>
        {(repos) => {
          const repo = repos.find((r) => r.id === repoId) ?? repos[0];
          return (
            <>
              <RepoPicker repos={repos} value={repo?.id ?? ''} onChange={setRepoId} label={t('projects.workspace.repository')} />
              {repo ? <BranchesTable projectId={projectId} repo={repo} onOpenFiles={onOpenFiles} /> : null}
            </>
          );
        }}
      </RepositoriesGate>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Worktrees (B4)
// ---------------------------------------------------------------------------

const WORKTREE_TONE: Record<string, 'success' | 'warning' | 'neutral' | 'danger'> = { ACTIVE: 'success', LOCKED: 'warning', REMOVED: 'neutral', ORPHANED: 'danger' };

export function WorktreesPanel({ workspaceId, projectId }: { workspaceId: string; projectId: string }) {
  const { t } = useTranslation();
  const id = useId();
  const [status, setStatus] = useState<WorktreeStatus | ''>('');
  const query = { ...(status ? { status: [status] } : {}) };
  const worktrees = useInfiniteQuery({
    queryKey: projectKeys.worktrees(projectId, query),
    queryFn: ({ pageParam, signal }) => projectsApi.worktrees(projectId, { ...query, limit: WORKTREES_PAGE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const repos = useProjectRepositories(projectId);
  const repoById = new Map((repos.data?.items ?? []).map((r) => [r.id, r]));
  const items = worktrees.data?.pages.flatMap((p) => p.items) ?? [];
  const columns: Column<Worktree>[] = [
    {
      id: 'branch',
      header: t('projects.workspace.col.branch'),
      rowHeader: true,
      sortValue: (w) => w.branch_name,
      cell: (w) => (
        <span className="flex flex-col">
          <span className="break-all font-mono">{w.branch_name}</span>
          {repoById.size > 1 ? <span className="break-all text-xs text-fg-muted">{repoById.get(w.repository_id) ? repoName(repoById.get(w.repository_id) as Repository) : ''}</span> : null}
        </span>
      ),
    },
    {
      id: 'status',
      header: t('projects.workspace.col.status'),
      sortValue: (w) => w.status,
      cell: (w) => <Badge tone={WORKTREE_TONE[w.status] ?? 'neutral'}>{t(`projects.enums.worktree_status.${w.status}`, { defaultValue: w.status })}</Badge>,
    },
    {
      id: 'commits',
      header: t('projects.workspace.col.commits'),
      cell: (w) => (
        <span className="flex flex-wrap items-center gap-1">
          <Hash sha={w.base_commit_sha} />
          <span aria-hidden="true">→</span>
          <span className="sr-only">{t('projects.workspace.to')}</span>
          <Hash sha={w.head_commit_sha} />
        </span>
      ),
    },
    {
      id: 'run',
      header: t('projects.workspace.col.run'),
      cell: (w) => (
        <span className="flex flex-wrap gap-x-3 gap-y-1">
          <Link className="text-link underline" to={missionControlPath(workspaceId, w.run_id, w.task_id)}>
            {t('projects.workspace.openMc')}
            <span className="sr-only">: {w.branch_name}</span>
          </Link>
          {w.task_id ? (
            <Link className="text-link underline" to={`/w/${workspaceId}/projects/${projectId}/runs/${w.run_id}/tasks/${w.task_id}`}>
              {t('projects.workspace.openTask')}
              <span className="sr-only">: {w.branch_name}</span>
            </Link>
          ) : null}
          <Link className="text-link underline" to={reviewPath(workspaceId, projectId, w.run_id, w.task_id)}>
            {t('projects.workspace.openReview')}
            <span className="sr-only">: {w.branch_name}</span>
          </Link>
        </span>
      ),
    },
    {
      id: 'updated',
      header: t('projects.workspace.col.updated'),
      sortValue: (w) => w.updated_at,
      cell: (w) => (
        <span className="flex flex-col text-xs">
          <Timestamp value={w.updated_at} />
          {w.removed_at ? (
            <span className="text-fg-muted">
              {t('projects.workspace.removedAt')} <Timestamp value={w.removed_at} />
            </span>
          ) : null}
        </span>
      ),
    },
  ];
  return (
    <section aria-labelledby="project-worktrees-title" className="flex flex-col gap-3">
      <h2 id="project-worktrees-title" className="text-lg font-semibold">
        {t('projects.workspace.worktreesTitle')}
      </h2>
      <p className="text-sm text-fg-muted">{t('projects.workspace.worktreesHint')}</p>
      <div className="flex flex-col gap-1">
        <label htmlFor={`${id}-status`} className="text-sm font-medium">
          {t('projects.workspace.statusFilter')}
        </label>
        <select id={`${id}-status`} className="select min-h-touch w-full sm:w-64" value={status} onChange={(e) => setStatus(e.target.value as WorktreeStatus | '')}>
          <option value="">{t('projects.workspace.allStatuses')}</option>
          {WORKTREE_STATUSES.map((s) => (
            <option key={s} value={s}>
              {t(`projects.enums.worktree_status.${s}`)}
            </option>
          ))}
        </select>
      </div>
      {worktrees.isPending ? (
        <LoadingState label={t('projects.workspace.loading')} />
      ) : worktrees.isError && items.length === 0 ? (
        <ErrorState error={worktrees.error} onRetry={() => void worktrees.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState headingLevel={3} icon="gitBranch" title={t('projects.workspace.noWorktreesTitle')} description={t('projects.workspace.noWorktrees')} />
      ) : (
        <>
          <Table caption={t('projects.workspace.worktreesTitle')} columns={columns} rows={items} rowKey={(w) => w.id} density="compact" />
          <LoadMore
            shown={items.length}
            hasNextPage={worktrees.hasNextPage}
            isFetchingNextPage={worktrees.isFetchingNextPage}
            nextPageError={worktrees.isFetchNextPageError ? worktrees.error : null}
            onLoadMore={() => void worktrees.fetchNextPage()}
          />
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Файлы и изменения
// ---------------------------------------------------------------------------

export function FilesPanel({
  projectId,
  repoId,
  branch,
  onRepoChange,
}: {
  projectId: string;
  repoId: string | null;
  branch: string | null;
  onRepoChange: (repoId: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="project-files-title" className="flex flex-col gap-3">
      <h2 id="project-files-title" className="text-lg font-semibold">
        {t('projects.workspace.filesTitle')}
      </h2>
      <p className="text-sm text-fg-muted">{t('projects.workspace.filesHint')}</p>
      <RepositoriesGate projectId={projectId}>
        {(repos) => {
          const repo = repos.find((r) => r.id === repoId) ?? repos.find((r) => r.local_root_ref) ?? repos[0];
          if (!repo) return null;
          return (
            <>
              <RepoPicker repos={repos} value={repo.id} onChange={onRepoChange} label={t('projects.workspace.repository')} />
              {repo.local_root_ref ? (
                <RepositoryExplorer key={`${repo.id}:${branch ?? ''}`} projectId={projectId} repo={repo} initialBranch={branch ?? undefined} />
              ) : (
                <Callout tone="neutral" title={t('projects.explorer.unavailableTitle')}>
                  {t('projects.explorer.unavailable')}
                </Callout>
              )}
            </>
          );
        }}
      </RepositoriesGate>
    </section>
  );
}
