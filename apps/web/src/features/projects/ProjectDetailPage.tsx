import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useParams } from 'react-router';
import { isApiError } from '../../api/client';
import { projectKeys, projectsApi, type Project, type Repository, type Technology } from '../../api/projects';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  DialogContent,
  DialogTrigger,
  LoadMore,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Timestamp,
  type Column,
} from '../../components/ui';
import { can } from '../../lib/permissions';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { AttachRepositoryForm } from './AttachRepositoryForm';

export const REPOSITORIES_PAGE_SIZE = 50;
export const TECHNOLOGIES_PAGE_SIZE = 50;

function ProjectOverview({ project }: { project: Project }) {
  const { t } = useTranslation();
  const mode = project.settings.documentation_mode;
  const profile = project.settings.quality_profile;
  return (
    <Card as="section" id="project-overview" title={t('projects.detail.overview')}>
      {project.description ? <p className="mb-3 whitespace-pre-line break-words">{project.description}</p> : null}
      <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-fg-muted">{t('projects.status')}</dt>
        <dd>{t(`enums.project_status.${project.status}`, { defaultValue: project.status })}</dd>
        <dt className="text-fg-muted">{t('projects.documentationMode')}</dt>
        <dd>{t(`enums.documentation_mode.${mode}`, { defaultValue: mode })}</dd>
        <dt className="text-fg-muted">{t('projects.qualityProfile')}</dt>
        <dd>{t(`enums.quality_profile.${profile}`, { defaultValue: profile })}</dd>
        <dt className="text-fg-muted">{t('common.createdAt')}</dt>
        <dd>
          <Timestamp value={project.created_at} />
        </dd>
        <dt className="text-fg-muted">{t('projects.detail.updatedAt')}</dt>
        <dd>
          <Timestamp value={project.updated_at} />
        </dd>
      </dl>
    </Card>
  );
}

function RepositoryCard({ repo }: { repo: Repository }) {
  const { t } = useTranslation();
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
      <Callout tone="neutral" title={t('projects.repo.explorerGapTitle')}>
        {t('projects.repo.explorerGap')}
      </Callout>
    </li>
  );
}

function RepositoriesPanel({ projectId, canAttach }: { projectId: string; canAttach: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
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
          onDone={(message) => {
            setOpen(false);
            setNotice(message);
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
            <RepositoryCard key={r.id} repo={r} />
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

/** P7-04: проект — обзор, Repository Explorer (репозитории и индекс), определённый стек. */
export function ProjectDetailPage() {
  const { t } = useTranslation();
  const { projectId = '' } = useParams();
  const { workspaceId, membership } = useCurrentWorkspace();
  const query = useQuery({
    queryKey: projectKeys.detail(projectId),
    queryFn: ({ signal }) => projectsApi.get(projectId, signal),
    enabled: Boolean(projectId),
  });

  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  const listPath = `/w/${workspaceId}/projects`;

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
    <section aria-labelledby="page-title" className="flex flex-col gap-6">
      <div>
        <Link to={listPath} className="text-sm font-medium text-link underline">
          {t('projects.backToList')}
        </Link>
        <div className="mt-2">
          <PageHeading title={project.name}>
            <Link to={`${listPath}/${project.id}/runs`} className="btn-secondary">
              {t('projects.runsLink')}
            </Link>
          </PageHeading>
        </div>
      </div>
      <ProjectOverview project={project} />
      <Tabs defaultValue="repositories">
        <TabsList label={t('projects.detail.tabs')}>
          <TabsTrigger value="repositories">{t('projects.repo.title')}</TabsTrigger>
          <TabsTrigger value="technologies">{t('projects.tech.title')}</TabsTrigger>
        </TabsList>
        <TabsContent value="repositories">
          <RepositoriesPanel projectId={project.id} canAttach={can(membership.role, 'repository:attach')} />
        </TabsContent>
        <TabsContent value="technologies">
          <TechnologiesPanel projectId={project.id} />
        </TabsContent>
      </Tabs>
    </section>
  );
}
