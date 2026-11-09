import { useInfiniteQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useLocation } from 'react-router';
import { projectKeys, projectsApi, type Project } from '../api/projects';
import { LoadMore } from '../components/LoadMore';
import { PageHeading } from '../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../components/states';
import { Badge } from '../components/ui/Badge';
import { Icon } from '../components/ui/Icon';
import { formatDate } from '../lib/format';
import { can } from '../lib/permissions';
import { rememberWorkspace } from '../lib/storage';
import { useCurrentWorkspace } from '../workspace';
import { WorkspaceNotFound } from './NotFoundPage';

export const PROJECTS_PAGE_SIZE = 50;

function ProjectCard({ project }: { project: Project }) {
  const { t, i18n } = useTranslation();
  const mode = project.settings.documentation_mode;
  const profile = project.settings.quality_profile;
  const base = `/w/${project.workspace_id}/projects/${project.id}`;
  return (
    <li className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4 shadow-e1" data-project-id={project.id}>
      <div className="flex items-start gap-3">
        <span className="rounded-md bg-accent-soft p-2 text-accent-soft-fg">
          <Icon name="folder" className="h-5 w-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="break-words text-lg font-semibold">{project.name}</h2>
          <p className="text-xs text-fg-muted">
            {t('common.createdAt')}: {formatDate(project.created_at, i18n.language)}
          </p>
        </div>
        <Badge tone={project.status === 'ACTIVE' ? 'success' : 'neutral'}>{t(`enums.project_status.${project.status}`, { defaultValue: project.status })}</Badge>
      </div>
      {project.description ? <p className="line-clamp-3 whitespace-pre-line break-words text-sm text-fg-muted">{project.description}</p> : null}
      <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-sm">
        <dt className="text-fg-muted">{t('projects.qualityProfile')}</dt>
        <dd>{t(`enums.quality_profile.${profile}`, { defaultValue: profile })}</dd>
        <dt className="text-fg-muted">{t('projects.documentationMode')}</dt>
        <dd>{t(`enums.documentation_mode.${mode}`, { defaultValue: mode })}</dd>
      </dl>
      <div className="mt-auto flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-3 text-sm">
        <Link to={base} className="font-medium text-accent-soft-fg underline" aria-label={t('projects.openLinkLabel', { name: project.name })}>
          {t('projects.openLink')}
        </Link>
        <Link to={`${base}/runs`} className="font-medium text-accent-soft-fg underline" aria-label={t('projects.runsLinkLabel', { name: project.name })}>
          {t('projects.runsLink')}
        </Link>
        <Link to={`${base}?tab=files`} className="font-medium text-accent-soft-fg underline" aria-label={t('projects.list.filesLabel', { name: project.name })}>
          {t('projects.list.files')}
        </Link>
      </div>
    </li>
  );
}

function ProjectsList({ workspaceId, canCreate }: { workspaceId: string; canCreate: boolean }) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState('');
  const query = useInfiniteQuery({
    queryKey: projectKeys.list(workspaceId),
    queryFn: ({ pageParam, signal }) =>
      projectsApi.list(workspaceId, { limit: PROJECTS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];

  if (query.isPending) return <LoadingState label={t('projects.loading')} />;
  if (query.isError && items.length === 0) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (items.length === 0) {
    return (
      <EmptyState
        title={t('projects.emptyTitle')}
        description={canCreate ? t('projects.emptyDescription') : t('projects.emptyDescriptionReadOnly')}
        action={
          canCreate ? (
            <Link to={`/w/${workspaceId}/projects/new`} className="btn-primary">
              {t('projects.create')}
            </Link>
          ) : undefined
        }
      />
    );
  }
  const needle = filter.trim().toLocaleLowerCase();
  const shown = needle ? items.filter((p) => p.name.toLocaleLowerCase().includes(needle)) : items;
  return (
    <>
      <div className="mb-4 flex flex-col gap-1">
        <label htmlFor="projects-filter" className="text-sm font-medium">
          {t('projects.list.search')}
        </label>
        <input
          id="projects-filter"
          type="search"
          className="input w-full sm:w-80"
          value={filter}
          aria-describedby="projects-filter-hint"
          onChange={(e) => setFilter(e.target.value)}
        />
        <p id="projects-filter-hint" className="text-xs text-fg-muted">
          {t('projects.list.searchHint')}
        </p>
      </div>
      {shown.length === 0 ? <p className="text-sm text-fg-muted">{t('projects.list.noMatch')}</p> : null}
      <ul className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3" aria-label={t('projects.listLabel')}>
        {shown.map((p) => (
          <ProjectCard key={p.id} project={p} />
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

export function ProjectsPage() {
  const { t } = useTranslation();
  const location = useLocation();
  const { workspaceId, membership } = useCurrentWorkspace();

  useEffect(() => {
    if (membership) rememberWorkspace(membership.workspace_id);
  }, [membership]);

  if (!workspaceId || !membership) return <WorkspaceNotFound />;

  // Только UX: кнопка скрыта для ролей без project:create; сервер всё равно проверяет право.
  const canCreate = can(membership.role, 'project:create');
  const state = location.state as { createdProject?: string; createdWorkspace?: string } | null;

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('projects.title', { workspace: membership.name })}>
        {canCreate ? (
          <Link to={`/w/${workspaceId}/projects/new`} className="btn-primary">
            {t('projects.create')}
          </Link>
        ) : null}
      </PageHeading>
      {state?.createdProject || state?.createdWorkspace ? (
        <p role="status" className="mb-4 rounded-md border border-success-border bg-success-soft p-3 text-sm text-success-fg">
          {state.createdProject
            ? t('projects.created', { name: state.createdProject })
            : t('workspaces.created', { name: state.createdWorkspace })}
        </p>
      ) : null}
      <ProjectsList key={workspaceId} workspaceId={workspaceId} canCreate={canCreate} />
    </section>
  );
}
