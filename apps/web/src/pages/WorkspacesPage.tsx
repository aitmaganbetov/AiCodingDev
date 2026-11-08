import { useInfiniteQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { api } from '../api/client';
import { LoadMore } from '../components/LoadMore';
import { PageHeading } from '../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../components/states';
import { formatDate } from '../lib/format';

export const WORKSPACES_PAGE_SIZE = 50;

export function WorkspacesPage() {
  const { t, i18n } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: ['workspaces'],
    queryFn: ({ pageParam, signal }) =>
      api.listWorkspaces({ limit: WORKSPACES_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  const createLink = (
    <Link to="/workspaces/new" className="btn-primary">
      {t('workspaces.create')}
    </Link>
  );

  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('workspaces.title')}>{items.length > 0 ? createLink : null}</PageHeading>
      {query.isPending ? (
        <LoadingState label={t('workspaces.loading')} />
      ) : query.isError && items.length === 0 ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState title={t('workspaces.emptyTitle')} description={t('workspaces.emptyDescription')} action={createLink} />
      ) : (
        <>
          <ul className="grid gap-3 sm:grid-cols-2">
            {items.map((w) => (
              <li key={w.id} className="card flex flex-col gap-2">
                <h2 className="text-lg font-semibold">
                  <Link to={`/w/${w.id}/projects`} className="rounded text-blue-800 underline-offset-4 hover:underline">
                    {w.name}
                  </Link>
                </h2>
                <dl className="grid grid-cols-[auto,1fr] gap-x-3 gap-y-1 text-sm">
                  <dt className="text-slate-600">{t('workspaces.slug')}</dt>
                  <dd className="break-all">{w.slug}</dd>
                  <dt className="text-slate-600">{t('workspaces.role')}</dt>
                  <dd>{t(`enums.workspace_role.${w.role}`, { defaultValue: w.role })}</dd>
                  <dt className="text-slate-600">{t('common.createdAt')}</dt>
                  <dd>{formatDate(w.created_at, i18n.language)}</dd>
                </dl>
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
      )}
    </section>
  );
}
