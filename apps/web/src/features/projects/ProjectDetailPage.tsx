import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { isApiError } from '../../api/client';
import { projectKeys, projectsApi, type Repository, type Technology } from '../../api/projects';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import {
  Badge,
  Button,
  ButtonLink,
  Callout,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTrigger,
  LoadMore,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Timestamp,
  useToast,
  type Column,
} from '../../components/ui';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { PullRequestsPanel } from '../integrations/PullRequestsPanel';
import { VerificationStatus } from '../integrations/VerificationStatus';
import { AttachRepositoryForm } from './AttachRepositoryForm';
import { RepositoryExplorer } from './RepositoryExplorer';
import { BranchesPanel, FilesPanel, ProjectOverviewPanel, ProjectRunsPanel, WorktreesPanel } from './ProjectWorkspacePanels';
import { useInPageSearchParams } from '../runs/useInPageSearchParams';

/** Вкладки рабочего пространства проекта (F6); выбранная — в `?tab=` (deep link). */
export const PROJECT_TABS = ['overview', 'repositories', 'files', 'branches', 'worktrees', 'runs', 'pull-requests', 'technologies'] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];

export const REPOSITORIES_PAGE_SIZE = 50;
export const TECHNOLOGIES_PAGE_SIZE = 50;

/**
 * Отвязать репозиторий от проекта (`detachRepository`). Действие подтверждается: прогоны и история остаются,
 * но проект репозиторий больше не показывает. Рабочую копию на диске платформа не удаляет — об этом сказано
 * в диалоге, чтобы «отвязать» не путали с «удалить файлы».
 */
function DetachRepository({ projectId, repo }: { projectId: string; repo: Repository }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { workspaceId } = useCurrentWorkspace();
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detach = useMutation({
    mutationFn: () => projectsApi.detachRepository(projectId, repo.id),
    onSuccess: async () => {
      setOpen(false);
      setError(null);
      toast({ title: t('projects.repo.detached'), tone: 'success' });
      await queryClient.invalidateQueries({ queryKey: projectKeys.repositories(projectId) });
      // Каталог и репозиторий GitHub снова свободны — подсказки формы привязки устарели.
      if (workspaceId) await queryClient.invalidateQueries({ queryKey: projectKeys.repositoryRoots(workspaceId) });
    },
    // Сервер объясняет отказ по существу («незавершённых прогонов — N»), и это важнее общего «Конфликт».
    onError: (e) => setError(isApiError(e) && e.detail ? e.detail : problemMessage(t, e)),
  });
  const location = repo.provider === 'github' ? repo.remote_url : repo.local_root_ref;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setError(null);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="secondary" icon="unplug" data-testid={`detach-${repo.id}`}>
          {t('projects.repo.detach')}
        </Button>
      </DialogTrigger>
      <DialogContent
        title={t('projects.repo.detachTitle')}
        description={t('projects.repo.detachDescription', { repository: location ?? repo.id })}
        footer={
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="danger" loading={detach.isPending} onClick={() => detach.mutate()} data-testid="detach-confirm">
              {t('projects.repo.detachConfirm')}
            </Button>
            <DialogClose className="btn-secondary">{t('projects.repo.detachCancel')}</DialogClose>
          </div>
        }
      >
        <p className="text-sm">{t('projects.repo.detachKeepsFiles')}</p>
        {error ? (
          <p role="alert" className="mt-2 text-sm font-medium text-danger-text">
            {error}
          </p>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function RepositoryCard({ projectId, repo, canDetach }: { projectId: string; repo: Repository; canDetach: boolean }) {
  const { t } = useTranslation();
  const [explorerOpen, setExplorerOpen] = useState(false);
  const explorerId = `repo-explorer-${repo.id}`;
  const snapshot = repo.latest_index_snapshot ?? null;
  const location = repo.provider === 'github' ? repo.remote_url : repo.local_root_ref;
  return (
    <li className="card flex flex-col gap-3" data-repository-id={repo.id}>
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="info">{t(`projects.enums.repository_provider.${repo.provider}`, { defaultValue: repo.provider })}</Badge>
        <h3 className="break-all font-semibold">{location ?? t('projects.repo.noLocation')}</h3>
      </div>
      <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-fg-muted">{t('projects.repo.defaultBranch')}</dt>
        <dd className="break-all font-mono">{repo.default_branch}</dd>
        <dt className="text-fg-muted">{t('projects.repo.lastSynced')}</dt>
        <dd>{repo.last_synced_at ? <Timestamp value={repo.last_synced_at} /> : t('projects.repo.neverSynced')}</dd>
        <dt className="text-fg-muted">{t('projects.repo.indexSnapshot')}</dt>
        <dd>
          {snapshot ? (
            <span className="flex flex-wrap items-center gap-2">
              <code className="font-mono" title={snapshot.commit_sha}>
                {snapshot.commit_sha.slice(0, 12)}
              </code>
              <Badge tone={snapshot.status === 'READY' || snapshot.status === 'COMPLETED' ? 'success' : 'neutral'}>{snapshot.status}</Badge>
            </span>
          ) : (
            t('projects.repo.notIndexed')
          )}
        </dd>
      </dl>
      {repo.local_root_ref ? (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" icon="folder" aria-expanded={explorerOpen} aria-controls={explorerId} onClick={() => setExplorerOpen((v) => !v)}>
              {explorerOpen ? t('projects.explorer.close') : t('projects.explorer.open')}
            </Button>
            {canDetach ? <DetachRepository projectId={projectId} repo={repo} /> : null}
          </div>
          <div id={explorerId} hidden={!explorerOpen}>
            {explorerOpen ? <RepositoryExplorer projectId={projectId} repo={repo} /> : null}
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <Callout tone="neutral" title={t('projects.explorer.unavailableTitle')}>
            {t('projects.explorer.unavailable')}
          </Callout>
          {canDetach ? (
            <div>
              <DetachRepository projectId={projectId} repo={repo} />
            </div>
          ) : null}
        </div>
      )}
    </li>
  );
}

function RepositoriesPanel({ workspaceId, projectId, canAttach }: { workspaceId: string; projectId: string; canAttach: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // provider=github: проверки доступа на воркере (OQ-47 (2)) — статус, пока репозиторий не привязан.
  const [verifications, setVerifications] = useState<string[]>([]);
  const query = useInfiniteQuery({
    queryKey: projectKeys.repositories(projectId),
    queryFn: ({ pageParam, signal }) =>
      projectsApi.repositories(projectId, { limit: REPOSITORIES_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];

  const attachDialog = canAttach ? (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="primary" icon="layers">
          {t('projects.repo.attachOpen')}
        </Button>
      </DialogTrigger>
      <DialogContent title={t('projects.repo.attachTitle')} description={t('projects.repo.attachDescription')}>
        <AttachRepositoryForm
          projectId={projectId}
          onDone={(message, verificationId) => {
            setOpen(false);
            setNotice(message);
            if (verificationId) setVerifications((v) => [...v, verificationId]);
          }}
        />
      </DialogContent>
    </Dialog>
  ) : null;

  let body;
  if (query.isPending) body = <LoadingState label={t('projects.repo.loading')} />;
  else if (query.isError && items.length === 0) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else if (items.length === 0) {
    body = (
      <EmptyState
        headingLevel={3}
        icon="layers"
        title={t('projects.repo.emptyTitle')}
        description={canAttach ? t('projects.repo.emptyDescription') : t('projects.repo.emptyDescriptionReadOnly')}
      />
    );
  } else {
    body = (
      <>
        <ul className="flex flex-col gap-3" aria-label={t('projects.repo.listLabel')}>
          {items.map((r) => (
            <RepositoryCard key={r.id} projectId={projectId} repo={r} canDetach={canAttach} />
          ))}
        </ul>
        <LoadMore
          shown={items.length}
          hasNextPage={query.hasNextPage}
          isFetchingNextPage={query.isFetchingNextPage}
          nextPageError={query.isFetchNextPageError ? query.error : null}
          onLoadMore={() => void query.fetchNextPage()}
        />
      </>
    );
  }

  return (
    <section aria-labelledby="repositories-title" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="repositories-title" className="text-lg font-semibold">
          {t('projects.repo.title')}
        </h2>
        {attachDialog}
      </div>
      {notice ? (
        <p role="status" className="rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-fg">
          {notice}
        </p>
      ) : null}
      {verifications.map((id) => (
        <VerificationStatus
          key={id}
          workspaceId={workspaceId}
          verificationId={id}
          onSettled={() => void queryClient.invalidateQueries({ queryKey: projectKeys.repositories(projectId) })}
        />
      ))}
      {body}
    </section>
  );
}

function TechnologiesPanel({ projectId }: { projectId: string }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: projectKeys.technologies(projectId),
    queryFn: ({ pageParam, signal }) =>
      projectsApi.technologies(projectId, { limit: TECHNOLOGIES_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  const columns: Column<Technology>[] = [
    { id: 'name', header: t('projects.tech.name'), rowHeader: true, cell: (x) => x.name, sortValue: (x) => x.name },
    { id: 'ecosystem', header: t('projects.tech.ecosystem'), cell: (x) => x.ecosystem, sortValue: (x) => x.ecosystem },
    { id: 'version', header: t('projects.tech.version'), cell: (x) => <span className="font-mono">{x.version_label}</span>, sortValue: (x) => x.version_label },
    { id: 'support', header: t('projects.tech.support'), cell: (x) => x.support_status ?? '—', sortValue: (x) => x.support_status },
    { id: 'detected', header: t('projects.tech.detectedFrom'), cell: (x) => <span className="break-all">{x.detected_from ?? '—'}</span> },
    {
      id: 'confirmed',
      header: t('projects.tech.confirmedBy'),
      cell: (x) => x.confirmed_by?.display_name ?? t('projects.tech.notConfirmed'),
    },
  ];

  return (
    <section aria-labelledby="technologies-title" className="flex flex-col gap-4">
      <h2 id="technologies-title" className="text-lg font-semibold">
        {t('projects.tech.title')}
      </h2>
      {query.isPending ? (
        <LoadingState label={t('projects.tech.loading')} />
      ) : query.isError && items.length === 0 ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : (
        <>
          <Table
            caption={t('projects.tech.title')}
            columns={columns}
            rows={items}
            rowKey={(x) => x.technology_version_id}
            defaultSort={{ columnId: 'name', direction: 'asc' }}
            empty={t('projects.tech.empty')}
          />
          {items.length > 0 ? (
            <LoadMore
              shown={items.length}
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

/**
 * Рабочее пространство проекта (F6, план §3.5; прежде P7-04): обзор (режимы, AI-исполнители, политика качества,
 * последние запуски), репозитории (привязка local/GitHub с проверкой 202), файлы и изменения, ветки, worktrees (B4),
 * запуски (Mission Control, Code Review), pull requests, технологии. Вкладка — в URL (`?tab=`).
 */
export function ProjectDetailPage() {
  const { t } = useTranslation();
  const { projectId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [params, setParams] = useInPageSearchParams();
  const query = useQuery({
    queryKey: projectKeys.detail(projectId),
    queryFn: ({ signal }) => projectsApi.get(projectId, signal),
    enabled: Boolean(projectId),
  });

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const listPath = `/w/${workspaceId}/projects`;
  const tabParam = params.get('tab');
  const tab: ProjectTab = (PROJECT_TABS as readonly string[]).includes(tabParam ?? '') ? (tabParam as ProjectTab) : 'overview';
  const setTab = (next: string, extra: Record<string, string> = {}) => {
    const p = new URLSearchParams();
    if (next !== 'overview') p.set('tab', next);
    for (const [k, v] of Object.entries(extra)) p.set(k, v);
    setParams(p);
  };

  if (query.isPending) {
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('projects.detail.titleFallback')} />
        <LoadingState label={t('projects.detail.loading')} />
      </section>
    );
  }
  if (query.isError) {
    const notFound = isApiError(query.error) && query.error.status === 404;
    return (
      <section aria-labelledby="page-title">
        <PageHeading title={t('projects.detail.titleFallback')} />
        {notFound ? (
          <EmptyState
            title={t('projects.detail.notFoundTitle')}
            description={t('projects.detail.notFound')}
            action={
              <Link to={listPath} className="btn-secondary">
                {t('projects.backToList')}
              </Link>
            }
          />
        ) : (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        )}
      </section>
    );
  }

  const project = query.data;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-4">
      <div>
        <Link to={listPath} className="text-sm font-medium text-link underline">
          {t('projects.backToList')}
        </Link>
        <div className="mt-2">
          <PageHeading title={project.name}>
            <div className="flex flex-wrap gap-2">
              <ButtonLink to={`${listPath}/${project.id}/runs`} variant="secondary" icon="play">
                {t('projects.runsLink')}
              </ButtonLink>
            </div>
          </PageHeading>
        </div>
        <p className="flex flex-wrap items-center gap-2 text-sm" data-testid="project-badges">
          <Badge tone={project.status === 'ACTIVE' ? 'success' : 'neutral'}>{t(`enums.project_status.${project.status}`, { defaultValue: project.status })}</Badge>
          <Badge tone="info">
            {t('projects.qualityProfile')}: {t(`enums.quality_profile.${project.settings.quality_profile}`, { defaultValue: project.settings.quality_profile })}
          </Badge>
          <Badge tone="neutral">
            {t('projects.documentationMode')}: {t(`enums.documentation_mode.${project.settings.documentation_mode}`, { defaultValue: project.settings.documentation_mode })}
          </Badge>
        </p>
      </div>
      <Tabs value={tab} onValueChange={(v) => setTab(v)}>
        <TabsList label={t('projects.detail.tabs')}>
          {PROJECT_TABS.map((x) => (
            <TabsTrigger key={x} value={x}>
              {x === 'pull-requests' ? t('integrations.pr.title') : t(`projects.workspace.tab.${x}`)}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="overview">
          <ProjectOverviewPanel project={project} workspaceId={workspaceId} role={membership.role} />
        </TabsContent>
        <TabsContent value="repositories">
          <RepositoriesPanel workspaceId={workspaceId} projectId={project.id} canAttach={can(membership.role, 'repository:attach')} />
        </TabsContent>
        <TabsContent value="files">
          <FilesPanel
            projectId={project.id}
            repoId={params.get('repo')}
            branch={params.get('branch')}
            onRepoChange={(repo) => setTab('files', { repo })}
          />
        </TabsContent>
        <TabsContent value="branches">
          <BranchesPanel projectId={project.id} onOpenFiles={(repo, branch) => setTab('files', { repo, branch })} />
        </TabsContent>
        <TabsContent value="worktrees">
          <WorktreesPanel workspaceId={workspaceId} projectId={project.id} />
        </TabsContent>
        <TabsContent value="runs">
          <ProjectRunsPanel workspaceId={workspaceId} projectId={project.id} role={membership.role} />
        </TabsContent>
        <TabsContent value="pull-requests">
          <PullRequestsPanel workspaceId={workspaceId} projectId={project.id} role={membership.role} />
        </TabsContent>
        <TabsContent value="technologies">
          <TechnologiesPanel projectId={project.id} />
        </TabsContent>
      </Tabs>
    </section>
  );
}
