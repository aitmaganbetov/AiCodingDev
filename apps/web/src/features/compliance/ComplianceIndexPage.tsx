import { useInfiniteQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { projectsApi } from '../../api/projects';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { useCurrentWorkspace } from '../../workspace';
import { RecentRuns, TextLink, workspacePath } from '../quality/shared';

const PROJECTS_PAGE_SIZE = 50;

function Projects({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: ['projects', workspaceId, 'compliance'],
    queryFn: ({ pageParam, signal }) => projectsApi.list(workspaceId, { limit: PROJECTS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  if (query.isPending) return <LoadingState />;
  if (query.isError && items.length === 0) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (items.length === 0) return <EmptyState headingLevel={3} title={t('compliance.index.noProjects')} />;
  return (
    <>
      <ul className="flex flex-col gap-2" aria-label={t('compliance.index.projects')}>
        {items.map((p) => (
          <li key={p.id} className="card flex flex-wrap items-center justify-between gap-2">
            <span className="font-medium">{p.name}</span>
            <TextLink to={workspacePath(workspaceId, `compliance/projects/${p.id}/controls`)} srSuffix={p.name}>
              {t('compliance.openControls')}
            </TextLink>
          </li>
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

export function ComplianceIndexPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('compliance.title')} />
      <p className="mb-6 text-fg-muted">{t('compliance.index.description')}</p>
      <div className="grid grid-cols-1 gap-8 lg:grid-cols-2">
        <section aria-labelledby="compliance-projects">
          <h2 id="compliance-projects" className="mb-3 text-lg font-semibold">
            {t('compliance.index.projects')}
          </h2>
          <Projects workspaceId={workspaceId} />
        </section>
        <section aria-labelledby="compliance-runs">
          <h2 id="compliance-runs" className="mb-3 text-lg font-semibold">
            {t('compliance.index.runs')}
          </h2>
          <RecentRuns
            workspaceId={workspaceId}
            label={t('compliance.index.runs')}
            actions={(r) => (
              <TextLink to={workspacePath(workspaceId, `compliance/runs/${r.id}`)} srSuffix={r.objective.slice(0, 80)}>
                {t('compliance.index.openMatrix')}
              </TextLink>
            )}
          />
        </section>
      </div>
    </section>
  );
}
