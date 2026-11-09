/**
 * Git-aware History: последние commit выбранной ветки (`listRepositoryBranches` + `listRepositoryCommits`,
 * cursor-пагинация) с отметкой проиндексированного commit и переходом «Файлы в этом commit»
 * (Code Explorer в этом commit; поиск символов и граф — по снимку commit, если он проиндексирован).
 * Тема commit и имя автора — недоверенный текст (только как текст).
 */
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { projectKeys, projectsApi, type Repository, type RepositoryCommitSummary } from '../../api/projects';
import { Badge, ErrorState, LoadMore, Skeleton, Table, Timestamp, type Column } from '../../components/ui';
import { BRANCHES_PAGE_SIZE } from '../projects/RepositoryExplorer';
import { Sha } from './shared';
import type { TabProps } from './SymbolSearch';

export const HISTORY_PAGE_SIZE = 30;

export function HistoryPanel({ projectId, repo, href }: Pick<TabProps, 'projectId' | 'href'> & { repo: Repository }) {
  const { t } = useTranslation();
  const uid = useId();
  const [branch, setBranch] = useState(repo.default_branch);
  const indexed = repo.latest_index_snapshot?.commit_sha ?? null;
  const branches = useQuery({
    queryKey: projectKeys.branches(projectId, repo.id),
    queryFn: ({ signal }) => projectsApi.branches(projectId, repo.id, { limit: BRANCHES_PAGE_SIZE }, signal),
    staleTime: 30_000,
  });
  const ref = branch === repo.default_branch ? '' : branch;
  const commits = useInfiniteQuery({
    queryKey: [...projectKeys.commits(projectId, repo.id, branch), 'history'] as const,
    queryFn: ({ pageParam, signal }) =>
      projectsApi.commits(projectId, repo.id, { limit: HISTORY_PAGE_SIZE, ...(ref ? { commit: ref } : {}), ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const names = branches.data?.items.map((b) => b.name) ?? [];
  if (!names.includes(repo.default_branch)) names.unshift(repo.default_branch);
  const items = commits.data?.pages.flatMap((p) => p.items) ?? [];

  const columns: Column<RepositoryCommitSummary>[] = [
    {
      id: 'sha',
      header: t('codeIntel.history.col.commit'),
      cell: (c) => (
        <span className="flex flex-col gap-1">
          <Sha sha={c.sha} />
          <span className="flex flex-wrap gap-1">
            {c.sha === indexed ? (
              <Badge tone="success" icon="layers">
                {t('codeIntel.history.indexed')}
              </Badge>
            ) : null}
            {c.parents.length > 1 ? <Badge tone="neutral">{t('codeIntel.history.merge')}</Badge> : null}
          </span>
        </span>
      ),
    },
    {
      id: 'subject',
      header: t('codeIntel.history.col.subject'),
      rowHeader: true,
      cell: (c) => <span className="break-words">{c.subject || <span className="text-fg-muted">{t('codeIntel.history.noSubject')}</span>}</span>,
    },
    { id: 'author', header: t('codeIntel.history.col.author'), sortValue: (c) => c.author_name, cell: (c) => <span className="break-words">{c.author_name}</span> },
    {
      id: 'date',
      header: t('codeIntel.history.col.date'),
      sortValue: (c) => c.committed_at,
      cell: (c) => (c.committed_at ? <Timestamp value={c.committed_at} /> : <span className="text-fg-muted">—</span>),
    },
    {
      id: 'actions',
      header: t('codeIntel.history.col.actions'),
      cell: (c) => (
        <Link className="link" to={href({ tab: 'explorer', commit: c.sha, file: '', line: null, end: null })}>
          {t('codeIntel.history.viewFiles')}
          <span className="sr-only">: {c.sha.slice(0, 12)}</span>
        </Link>
      ),
    },
  ];

  return (
    <div className="flex flex-col gap-3" data-testid="history-panel">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor={`${uid}-branch`} className="text-sm font-medium text-fg">
            {t('codeIntel.history.branch')}
          </label>
          <select id={`${uid}-branch`} className="select max-w-full" value={branch} onChange={(e) => setBranch(e.target.value)}>
            {names.map((name) => (
              <option key={name} value={name}>
                {name === repo.default_branch ? t('codeIntel.history.defaultBranch', { branch: name }) : name}
              </option>
            ))}
          </select>
        </div>
        {branches.isError ? <p className="text-sm text-fg-muted">{t('codeIntel.history.branchesUnavailable')}</p> : null}
        {branches.data?.truncated ? <p className="text-sm text-fg-muted">{t('codeIntel.history.branchesTruncated')}</p> : null}
      </div>
      {commits.isPending ? (
        <Skeleton lines={5} />
      ) : commits.isError && items.length === 0 ? (
        <ErrorState error={commits.error} onRetry={() => void commits.refetch()} />
      ) : items.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('codeIntel.history.empty')}</p>
      ) : (
        <>
          <Table caption={t('codeIntel.history.caption', { branch })} columns={columns} rows={items} rowKey={(c) => c.sha} density="compact" minWidthClass="min-w-[44rem]" />
          {commits.hasNextPage || commits.isFetchNextPageError ? (
            <LoadMore
              shown={items.length}
              hasNextPage={commits.hasNextPage}
              isFetchingNextPage={commits.isFetchingNextPage}
              nextPageError={commits.isFetchNextPageError ? commits.error : null}
              onLoadMore={() => void commits.fetchNextPage()}
            />
          ) : null}
        </>
      )}
    </div>
  );
}
