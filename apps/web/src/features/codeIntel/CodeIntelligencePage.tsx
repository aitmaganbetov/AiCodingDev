/**
 * Code Intelligence (F8 редизайна, план §3.6): проект → репозиторий → commit (снимок индекса) и вкладки
 * Code Explorer · Символы · Граф зависимостей · Context Packs · Индексация · Знания · История · Поиск.
 * Источники — только API: explorer (tree/file/branches/commits), B3 (symbols, dependencies, indexing jobs,
 * context pack), knowledge (technologies, documentation sources/policy), analytics.context, `GET /search`.
 * Состояние — в URL (`model.ts`): ссылкой на файл/строки, граф или Context Pack можно поделиться.
 */
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useId, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { IndexingJobStatus } from '../../api/codeIntel';
import { projectKeys, projectsApi, type Repository } from '../../api/projects';
import { runKeys, runsApi } from '../../api/runs';
import { PageHeading } from '../../components/PageHeading';
import { Badge, ButtonLink, Callout, EmptyState, ErrorState, Skeleton, Tabs, TabsContent, TabsList, TabsTrigger, Timestamp } from '../../components/ui';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { useProjectOptions } from '../knowledge/ProjectSelect';
import { RepositoryExplorer, commitLabel } from '../projects/RepositoryExplorer';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';
import { ContextPacks } from './ContextPacks';
import { DependencyGraph } from './DependencyGraph';
import { HistoryPanel } from './HistoryPanel';
import { IndexingJobs, latestJobQuery } from './IndexingJobs';
import { KnowledgePanel } from './KnowledgePanel';
import { CODE_TABS, JOB_ACTIVE, codeHref, codeParamsToSearch, parseCodeParams, type CodeParams } from './model';
import { SearchPanel } from './SearchPanel';
import { JobStatusBadge, NoIndexState, Sha } from './shared';
import { SymbolSearch } from './SymbolSearch';

export const REPOSITORIES_LIMIT = 50;
export const COMMIT_CHOICES = 20;

function repoLabel(r: Repository): string {
  return r.provider === 'github' ? (r.remote_url ?? r.id) : (r.local_root_ref ?? r.id);
}

/** Сводка индекса выбранного репозитория: последний снимок и последнее задание индексации. */
function IndexSummary({ workspaceId, projectId, repo, indexingHref }: { workspaceId: string; projectId: string; repo: Repository; indexingHref: string }) {
  const { t } = useTranslation();
  const snapshot = repo.latest_index_snapshot ?? null;
  const latest = useQuery({
    ...latestJobQuery(projectId, repo.id),
    refetchInterval: (q) => (q.state.data?.items.some((j) => JOB_ACTIVE.includes(j.status)) ? 15_000 : false),
  });
  const job = latest.data?.items[0];
  const active = job ? JOB_ACTIVE.includes(job.status) : false;
  return (
    <div className="flex flex-col gap-2" data-testid="index-summary">
      <dl className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <dt className="text-fg-muted">{t('codeIntel.index.snapshot')}</dt>
          <dd className="flex flex-wrap items-center gap-2" data-testid="index-snapshot">
            {snapshot ? (
              <>
                <Sha sha={snapshot.commit_sha} />
                <Badge tone={snapshot.status === 'READY' ? 'success' : 'neutral'}>{t(`codeIntel.snapshotStatus.${snapshot.status}`, { defaultValue: snapshot.status })}</Badge>
              </>
            ) : (
              <Badge tone="neutral">{t('codeIntel.index.none')}</Badge>
            )}
          </dd>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <dt className="text-fg-muted">{t('codeIntel.index.lastJob')}</dt>
          <dd className="flex flex-wrap items-center gap-2" data-testid="index-last-job">
            {latest.isPending ? (
              <Skeleton lines={1} className="w-32" />
            ) : latest.isError ? (
              <span className="text-fg-muted">{t('codeIntel.index.jobUnavailable')}</span>
            ) : job ? (
              <>
                <JobStatusBadge status={job.status} />
                <span className="text-fg-muted">
                  <Sha sha={job.commit_sha} length={10} /> · <Timestamp value={job.finished_at ?? job.started_at ?? job.created_at} />
                </span>
                <Link className="link" to={indexingHref}>
                  {t('codeIntel.index.allJobs')}
                </Link>
              </>
            ) : (
              <span className="text-fg-muted">{t('codeIntel.index.noJobs')}</span>
            )}
          </dd>
        </div>
      </dl>
      {!snapshot && active ? (
        <Callout tone="info" title={t('codeIntel.index.buildingTitle')}>
          {t('codeIntel.index.building')}
        </Callout>
      ) : null}
      {!snapshot && !active && !latest.isPending ? <NoIndexState workspaceId={workspaceId} projectId={projectId} indexingHref={indexingHref} /> : null}
      {snapshot && job?.status === 'FAILED' ? (
        <Callout tone="warning" title={t('codeIntel.index.lastFailedTitle')}>
          {t('codeIntel.index.lastFailed')}
        </Callout>
      ) : null}
    </div>
  );
}

function CommitSelect({
  projectId,
  repo,
  value,
  onChange,
}: {
  projectId: string;
  repo: Repository;
  value: string;
  onChange: (sha: string) => void;
}) {
  const { t } = useTranslation();
  const id = useId();
  const snapshot = repo.latest_index_snapshot ?? null;
  const recent = useQuery({
    queryKey: [...projectKeys.commits(projectId, repo.id, repo.default_branch), 'code-intel', COMMIT_CHOICES] as const,
    queryFn: ({ signal }) => projectsApi.commits(projectId, repo.id, { limit: COMMIT_CHOICES }, signal),
    staleTime: 30_000,
    enabled: Boolean(repo.local_root_ref),
  });
  const commits = recent.data?.items ?? [];
  const known = value === '' || commits.some((c) => c.sha === value);
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {t('codeIntel.selector.commit')}
      </label>
      <select id={id} className="select max-w-full sm:max-w-[30rem]" value={value} onChange={(e) => onChange(e.target.value)} data-testid="code-commit">
        <option value="">
          {snapshot ? t('codeIntel.selector.latestSnapshot', { sha: snapshot.commit_sha.slice(0, 12) }) : t('codeIntel.selector.latestNone')}
        </option>
        {!known ? <option value={value}>{t('codeIntel.selector.commitById', { sha: value.slice(0, 12) })}</option> : null}
        {commits.length > 0 ? (
          <optgroup label={t('codeIntel.selector.recent', { branch: repo.default_branch })}>
            {commits.map((c) => (
              <option key={c.sha} value={c.sha}>
                {commitLabel(c)}
                {c.sha === snapshot?.commit_sha ? ` ${t('codeIntel.selector.indexedMark')}` : ''}
              </option>
            ))}
          </optgroup>
        ) : null}
      </select>
    </div>
  );
}

export function CodeIntelligencePage() {
  const { t } = useTranslation();
  const uid = useId();
  const { workspaceId } = useCurrentWorkspace();
  const ws = workspaceId ?? '';
  const [search, setSearch] = useInPageSearchParams();
  const params = useMemo(() => parseCodeParams(search), [search]);
  const update = useCallback((patch: Partial<CodeParams>) => setSearch(codeParamsToSearch({ ...params, ...patch })), [params, setSearch]);
  const [jobStatus, setJobStatus] = useState<IndexingJobStatus | ''>('');

  const projects = useProjectOptions(ws);
  // Ссылка из Mission Control (`?tab=context&run=…`) без проекта: проект — из run.
  const runForProject = useQuery({
    queryKey: runKeys.detail(params.run),
    queryFn: ({ signal }) => runsApi.get(params.run, signal),
    enabled: Boolean(params.run) && !params.project,
  });
  const firstProject = projects.data?.items[0]?.id;
  const runProject = runForProject.data?.project_id;
  useEffect(() => {
    if (params.project) return;
    if (params.run) {
      if (runProject) update({ project: runProject });
    } else if (firstProject) update({ project: firstProject });
  }, [params.project, params.run, runProject, firstProject, update]);

  const projectId = params.project;
  const repos = useQuery({
    queryKey: [...projectKeys.repositories(projectId), 'all'],
    queryFn: ({ signal }) => projectsApi.repositories(projectId, { limit: REPOSITORIES_LIMIT }, signal),
    enabled: Boolean(projectId),
  });
  const repoItems = repos.data?.items ?? [];
  const repo = repoItems.find((r) => r.id === params.repo) ?? repoItems[0];

  const base = useMemo(() => ({ project: params.project, repo: repo?.id ?? params.repo, commit: params.commit }), [params.project, params.repo, params.commit, repo?.id]);
  const href = useCallback((patch: Partial<CodeParams>) => codeHref(ws, { ...base, ...patch }), [ws, base]);

  if (!workspaceId) return <WorkspaceNotFound />;

  const projectItems = projects.data?.items ?? [];
  const projectKnown = projectItems.some((p) => p.id === projectId);

  let content;
  if (projects.isPending || (!projectId && (projectItems.length > 0 || runForProject.isPending))) content = <Skeleton lines={4} />;
  else if (projects.isError) content = <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />;
  else if (!projectId && runForProject.isError) content = <ErrorState error={runForProject.error} onRetry={() => void runForProject.refetch()} />;
  else if (projectItems.length === 0 && !projectId)
    content = (
      <EmptyState
        icon="folder"
        title={t('codeIntel.empty.noProjectsTitle')}
        description={t('codeIntel.empty.noProjects')}
        action={
          <ButtonLink to={`/w/${workspaceId}/projects`} variant="secondary" icon="folder">
            {t('codeIntel.empty.toProjects')}
          </ButtonLink>
        }
      />
    );
  else if (repos.isPending) content = <Skeleton lines={4} />;
  else if (repos.isError) content = <ErrorState error={repos.error} onRetry={() => void repos.refetch()} />;
  else if (!repo)
    content = (
      <EmptyState
        icon="layers"
        title={t('codeIntel.empty.noReposTitle')}
        description={t('codeIntel.empty.noRepos')}
        action={
          <ButtonLink to={`/w/${workspaceId}/projects/${projectId}?tab=repositories`} variant="secondary" icon="plus">
            {t('codeIntel.empty.toRepositories')}
          </ButtonLink>
        }
      />
    );
  else {
    const snapshot = repo.latest_index_snapshot ?? null;
    const explorerCommit = params.commit || snapshot?.commit_sha || '';
    const highlight = params.line !== null ? { from: params.line, to: params.end ?? params.line } : null;
    const tabProps = { workspaceId, projectId, repositoryId: repo.id, params, update, href };
    content = (
      <>
        <IndexSummary workspaceId={workspaceId} projectId={projectId} repo={repo} indexingHref={href({ tab: 'indexing' })} />
        <Tabs value={params.tab} onValueChange={(v) => update({ tab: v as CodeParams['tab'] })}>
          <TabsList label={t('codeIntel.tabsLabel')}>
            {CODE_TABS.map((x) => (
              <TabsTrigger key={x} value={x}>
                {t(`codeIntel.tab.${x}`)}
              </TabsTrigger>
            ))}
          </TabsList>
          <div key={repo.id}>
            <TabsContent value="explorer">
              {repo.local_root_ref ? (
                <div className="flex flex-col gap-3">
                  <p className="text-sm text-fg-muted" data-testid="explorer-commit-note">
                    {params.commit ? t('codeIntel.explorer.atCommit') : snapshot ? t('codeIntel.explorer.atSnapshot') : t('codeIntel.explorer.atHead', { branch: repo.default_branch })}{' '}
                    {explorerCommit ? <Sha sha={explorerCommit} /> : null}
                  </p>
                  <RepositoryExplorer
                    key={`${explorerCommit}:${params.file}:${params.line ?? ''}:${params.end ?? ''}`}
                    projectId={projectId}
                    repo={repo}
                    commit={explorerCommit}
                    initialFile={params.file || undefined}
                    highlight={highlight}
                  />
                  {params.file ? (
                    <p className="flex flex-wrap gap-3 text-sm">
                      <Link className="link" to={href({ tab: 'graph', groot: params.file, gsym: '', gsymName: '' })}>
                        {t('codeIntel.explorer.graphOfFile', { path: params.file })}
                      </Link>
                    </p>
                  ) : null}
                </div>
              ) : (
                <Callout tone="neutral" title={t('projects.explorer.unavailableTitle')}>
                  {t('projects.explorer.unavailable')}
                </Callout>
              )}
            </TabsContent>
            <TabsContent value="symbols">
              {/* key: форма заново берёт значения из URL после перехода по ссылке (например, «Сделать корнем»). */}
              <SymbolSearch key={`${params.q}\n${params.kind}\n${params.spath}`} {...tabProps} />
            </TabsContent>
            <TabsContent value="graph">
              <DependencyGraph key={`${params.groot}\n${params.gsym}\n${params.depth}\n${params.dir}`} {...tabProps} />
            </TabsContent>
            <TabsContent value="context">
              <ContextPacks {...tabProps} />
            </TabsContent>
            <TabsContent value="indexing">
              <IndexingJobs projectId={projectId} repositoryId={repo.id} statusFilter={jobStatus} onStatusFilter={setJobStatus} />
            </TabsContent>
            <TabsContent value="knowledge">
              <KnowledgePanel workspaceId={workspaceId} projectId={projectId} />
            </TabsContent>
            <TabsContent value="history">
              <HistoryPanel projectId={projectId} repo={repo} href={href} />
            </TabsContent>
            <TabsContent value="search">
              <SearchPanel workspaceId={workspaceId} projectId={projectId} />
            </TabsContent>
          </div>
        </Tabs>
      </>
    );
  }

  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4">
      <PageHeading title={t('nav.code')} />
      <p className="text-sm text-fg-muted">{t('codeIntel.intro')}</p>
      {projectItems.length > 0 || projectId ? (
        <div className="flex flex-wrap items-end gap-3" role="group" aria-label={t('codeIntel.selector.label')} data-testid="code-selector">
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor={`${uid}-project`} className="text-sm font-medium text-fg">
              {t('codeIntel.selector.project')}
            </label>
            <select
              id={`${uid}-project`}
              className="select max-w-full sm:max-w-[22rem]"
              value={projectId}
              onChange={(e) => update({ ...EMPTY_SCOPE, project: e.target.value })}
              data-testid="code-project"
            >
              {projectId && !projectKnown ? <option value={projectId}>{t('codeIntel.selector.projectById', { id: projectId.slice(0, 8) })}</option> : null}
              {projectItems.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          {repoItems.length > 0 ? (
            <div className="flex min-w-0 flex-col gap-1">
              <label htmlFor={`${uid}-repo`} className="text-sm font-medium text-fg">
                {t('codeIntel.selector.repository')}
              </label>
              <select
                id={`${uid}-repo`}
                className="select max-w-full sm:max-w-[26rem]"
                value={repo?.id ?? ''}
                onChange={(e) => update({ ...EMPTY_SCOPE, project: projectId, repo: e.target.value, tab: params.tab })}
                data-testid="code-repo"
              >
                {repoItems.map((r) => (
                  <option key={r.id} value={r.id}>
                    {repoLabel(r)}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          {repo ? <CommitSelect projectId={projectId} repo={repo} value={params.commit} onChange={(sha) => update({ commit: sha })} /> : null}
        </div>
      ) : null}
      {content}
    </section>
  );
}

/** Смена проекта/репозитория сбрасывает выбор внутри них (файл, граф, поиск, run). */
const EMPTY_SCOPE: Partial<CodeParams> = {
  repo: '',
  commit: '',
  file: '',
  line: null,
  end: null,
  q: '',
  kind: '',
  spath: '',
  groot: '',
  gsym: '',
  gsymName: '',
  run: '',
  task: '',
  attempt: '',
};
