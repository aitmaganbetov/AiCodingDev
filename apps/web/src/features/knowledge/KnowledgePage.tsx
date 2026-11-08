import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { IdempotentIntent } from '../../api/client';
import { knowledgeApi, knowledgeKeys, type DocumentationSource } from '../../api/knowledge';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import {
  Badge,
  Button,
  Callout,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTrigger,
  LoadMore,
  Table,
  Timestamp,
  useToast,
  type Column,
} from '../../components/ui';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { DocumentationPolicyPanel } from './DocumentationPolicyPanel';
import { RegisterSourceForm } from './RegisterSourceForm';

export const SOURCES_PAGE_SIZE = 50;
type EnabledFilter = 'all' | 'enabled' | 'disabled';

/**
 * Обновление кэша источника: явный запрос человека = согласие на загрузку вне run (API_DESIGN,
 * refreshDocumentationSource). Поэтому — подтверждение в диалоге с адресом источника.
 */
function RefreshAction({ source, onResult }: { source: DocumentationSource; onResult: (message: string, ok: boolean) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const intent = useRef(new IdempotentIntent());
  const refresh = useMutation({
    mutationFn: () => knowledgeApi.refreshSource(source.id, intent.current.keyFor({ source: source.id })),
    onSuccess: async (accepted) => {
      intent.current.complete();
      setOpen(false);
      const message = t('knowledge.refresh.queued', { uri: source.uri_or_file_ref, action: accepted.external_action_id });
      onResult(message, true);
      toast({ title: t('knowledge.refresh.queuedShort'), tone: 'info' });
      await queryClient.invalidateQueries({ queryKey: knowledgeKeys.allSources(source.workspace_id) });
    },
    onError: (error) => {
      setOpen(false);
      onResult(problemMessage(t, error), false);
    },
  });
  if (!source.enabled) return <span className="text-sm text-fg-muted">{t('knowledge.refresh.disabled')}</span>;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" aria-label={t('knowledge.refresh.buttonLabel', { uri: source.uri_or_file_ref })}>
          {t('knowledge.refresh.button')}
        </Button>
      </DialogTrigger>
      <DialogContent
        title={t('knowledge.refresh.confirmTitle')}
        description={t('knowledge.refresh.confirmDescription')}
        footer={
          <>
            <DialogClose asChild>
              <Button>{t('action.cancel')}</Button>
            </DialogClose>
            <Button variant="primary" loading={refresh.isPending} onClick={() => refresh.mutate()}>
              {t('knowledge.refresh.confirm')}
            </Button>
          </>
        }
      >
        <p className="break-all font-mono text-sm">{source.uri_or_file_ref}</p>
        <p className="mt-3 text-sm">{t('knowledge.refresh.consent')}</p>
      </DialogContent>
    </Dialog>
  );
}

function SourcesPanel({ workspaceId, canRegister, canRefresh }: { workspaceId: string; canRegister: boolean; canRefresh: boolean }) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<EnabledFilter>('all');
  const [registerOpen, setRegisterOpen] = useState(false);
  const [notice, setNotice] = useState<{ text: string; ok: boolean } | null>(null);
  const query = useInfiniteQuery({
    queryKey: knowledgeKeys.sources(workspaceId, filter),
    queryFn: ({ pageParam, signal }) =>
      knowledgeApi.listSources(
        workspaceId,
        {
          limit: SOURCES_PAGE_SIZE,
          ...(filter === 'all' ? {} : { enabled: filter === 'enabled' }),
          ...(pageParam ? { cursor: pageParam } : {}),
        },
        signal,
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  // OQ-45 (c): имена технологий источников — из списка технологий workspace (без выбора проекта).
  const technologies = useQuery({
    queryKey: [...knowledgeKeys.technologies(workspaceId), 'names'] as const,
    queryFn: ({ signal }) => knowledgeApi.technologies(workspaceId, { limit: 200 }, signal),
  });
  const techNames = useMemo(
    () => new Map((technologies.data?.items ?? []).map((x) => [x.technology_version_id, `${x.name} ${x.version_label}`])),
    [technologies.data],
  );

  const columns: Column<DocumentationSource>[] = [
    {
      id: 'uri',
      header: t('knowledge.col.uri'),
      rowHeader: true,
      cell: (s) => <span className="break-all font-mono text-xs">{s.uri_or_file_ref}</span>,
      sortValue: (s) => s.uri_or_file_ref,
      className: 'max-w-[20rem]',
    },
    {
      id: 'technology',
      header: t('knowledge.col.technology'),
      cell: (s) =>
        s.technology_version_id ? (techNames.get(s.technology_version_id) ?? (technologies.isPending ? '…' : t('knowledge.techUnknown'))) : '—',
      sortValue: (s) => (s.technology_version_id ? (techNames.get(s.technology_version_id) ?? '') : ''),
    },
    { id: 'type', header: t('knowledge.col.type'), cell: (s) => t(`knowledge.enums.source_type.${s.source_type}`, { defaultValue: s.source_type }) },
    {
      id: 'trust',
      header: t('knowledge.col.trust'),
      cell: (s) => (
        <Badge tone={s.trust_level === 'official' ? 'success' : s.trust_level === 'internal' ? 'info' : 'neutral'}>
          {t(`knowledge.enums.trust_level.${s.trust_level}`, { defaultValue: s.trust_level })}
        </Badge>
      ),
      sortValue: (s) => s.trust_level,
    },
    { id: 'refresh', header: t('knowledge.col.refreshPolicy'), cell: (s) => t(`knowledge.enums.refresh_policy.${s.refresh_policy}`, { defaultValue: s.refresh_policy }) },
    {
      id: 'enabled',
      header: t('knowledge.col.enabled'),
      cell: (s) => <Badge tone={s.enabled ? 'success' : 'neutral'}>{s.enabled ? t('knowledge.on') : t('knowledge.off')}</Badge>,
      sortValue: (s) => (s.enabled ? 0 : 1),
    },
    {
      id: 'snapshot',
      header: t('knowledge.col.snapshot'),
      cell: (s) =>
        s.latest_snapshot ? (
          <span className="flex flex-col gap-1">
            <span>{s.latest_snapshot.status}</span>
            <Timestamp value={s.latest_snapshot.fetched_at} className="text-xs text-fg-muted" />
          </span>
        ) : (
          t('knowledge.notFetched')
        ),
      sortValue: (s) => s.latest_snapshot?.fetched_at,
    },
    { id: 'license', header: t('knowledge.col.license'), cell: (s) => <span className="break-words">{s.license_info ?? '—'}</span> },
    ...(canRefresh
      ? [
          {
            id: 'actions',
            header: t('knowledge.col.actions'),
            cell: (s: DocumentationSource) => <RefreshAction source={s} onResult={(text, ok) => setNotice({ text, ok })} />,
          },
        ]
      : []),
  ];

  let body;
  if (query.isPending) body = <LoadingState label={t('knowledge.loading')} />;
  else if (query.isError && items.length === 0) body = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  else if (items.length === 0 && filter === 'all') {
    body = (
      <EmptyState
        headingLevel={3}
        icon="book"
        title={t('knowledge.emptyTitle')}
        description={canRegister ? t('knowledge.emptyDescription') : t('knowledge.emptyDescriptionReadOnly')}
      />
    );
  } else {
    body = (
      <>
        <Table
          caption={t('knowledge.sourcesTitle')}
          columns={columns}
          rows={items}
          rowKey={(s) => s.id}
          empty={t('knowledge.emptyFiltered')}
          minWidthClass="min-w-[56rem]"
          rowProps={(s) => ({ 'data-source-id': s.id })}
        />
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
    <section aria-labelledby="sources-title" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h2 id="sources-title" className="text-lg font-semibold">
          {t('knowledge.sourcesTitle')}
        </h2>
        <div className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1">
            <label htmlFor="sources-filter" className="text-sm font-medium">
              {t('knowledge.filter')}
            </label>
            <select id="sources-filter" className="input" value={filter} onChange={(e) => setFilter(e.target.value as EnabledFilter)}>
              <option value="all">{t('knowledge.filterAll')}</option>
              <option value="enabled">{t('knowledge.filterEnabled')}</option>
              <option value="disabled">{t('knowledge.filterDisabled')}</option>
            </select>
          </div>
          {canRegister ? (
            <Dialog open={registerOpen} onOpenChange={setRegisterOpen}>
              <DialogTrigger asChild>
                <Button variant="primary">{t('knowledge.register.open')}</Button>
              </DialogTrigger>
              <DialogContent title={t('knowledge.register.title')} size="lg">
                <RegisterSourceForm
                  workspaceId={workspaceId}
                  onDone={(text) => {
                    setRegisterOpen(false);
                    setNotice({ text, ok: true });
                  }}
                />
              </DialogContent>
            </Dialog>
          ) : null}
        </div>
      </div>
      <p role="status" className={notice && !notice.ok ? 'text-sm font-medium text-danger-text' : 'text-sm text-success-fg'}>
        {notice?.text}
      </p>
      {body}
    </section>
  );
}

/** P7-12 Knowledge/Documentation: источники документации, обновление кэша (с согласием), режим документации проекта. */
export function KnowledgePage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6">
      <PageHeading title={t('knowledge.title')} />
      <Callout tone="info" className="-mt-2">
        {t('knowledge.intro')}
      </Callout>
      <SourcesPanel
        key={workspaceId}
        workspaceId={workspaceId}
        canRegister={can(membership.role, 'documentation:register')}
        canRefresh={can(membership.role, 'documentation:refresh')}
      />
      <DocumentationPolicyPanel key={`policy-${workspaceId}`} workspaceId={workspaceId} canEdit={can(membership.role, 'project:update')} />
    </section>
  );
}
