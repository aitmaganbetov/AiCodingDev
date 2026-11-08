import { useInfiniteQuery } from '@tanstack/react-query';
import { useMemo, useRef, useState, type FormEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { auditApi, auditKeys, type AuditEvent, type AuditFilters } from '../../api/audit';
import { TextField, focusFirstInvalid } from '../../components/form';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { Badge, Button, CodeBlock, LoadMore, Table, Timestamp, type Column } from '../../components/ui';
import { WorkspaceNotFound } from '../../pages/NotFoundPage';
import { useCurrentWorkspace } from '../../workspace';
import { draftFromFilters, filtersFromDraft, filtersFromSearch, searchFromFilters, type FilterDraft, type FilterKey } from './filters';

export const AUDIT_PAGE_SIZE = 50;

function FiltersForm({ filters, search, onApply }: { filters: AuditFilters; search: string; onApply: (f: AuditFilters) => void }) {
  const { t } = useTranslation();
  const formRef = useRef<HTMLFormElement>(null);
  const [draft, setDraft] = useState<FilterDraft>(() => draftFromFilters(filters));
  const [errors, setErrors] = useState<Partial<Record<FilterKey, string>>>({});
  // URL изменился (применение, сброс, «назад») — черновик берётся из URL без перемонтирования
  // формы: фокус остаётся на кнопке, которой фильтр применили.
  const [seenSearch, setSeenSearch] = useState(search);
  if (seenSearch !== search) {
    setSeenSearch(search);
    setDraft(draftFromFilters(filters));
    setErrors({});
  }
  const set = (key: keyof FilterDraft) => (e: { target: { value: string } }) => setDraft({ ...draft, [key]: e.target.value });

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const result = filtersFromDraft(draft);
    const messages: Partial<Record<FilterKey, string>> = {};
    for (const [k, v] of Object.entries(result.errors)) messages[k as FilterKey] = t(`audit.filterError.${v}`);
    setErrors(messages);
    if (Object.keys(messages).length > 0) {
      requestAnimationFrame(() => focusFirstInvalid(formRef.current));
      return;
    }
    onApply(result.filters);
  };
  const reset = () => {
    setDraft(draftFromFilters({}));
    setErrors({});
    onApply({});
  };

  return (
    <form ref={formRef} noValidate onSubmit={onSubmit} className="card flex flex-col gap-4" aria-label={t('audit.filters')}>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <TextField id="audit-action" label={t('audit.field.action')} hint={t('audit.field.actionHint')} autoComplete="off" value={draft.action} onChange={set('action')} />
        <TextField
          id="audit-resource-type"
          label={t('audit.field.resourceType')}
          hint={t('audit.field.resourceTypeHint')}
          autoComplete="off"
          value={draft.resource_type}
          onChange={set('resource_type')}
        />
        <TextField id="audit-resource-id" label={t('audit.field.resourceId')} autoComplete="off" value={draft.resource_id} onChange={set('resource_id')} />
        <TextField
          id="audit-actor-id"
          label={t('audit.field.actorId')}
          hint={t('audit.field.actorIdHint')}
          autoComplete="off"
          value={draft.actor_id}
          onChange={set('actor_id')}
          error={errors.actor_id}
        />
        <TextField id="audit-from" type="datetime-local" label={t('audit.field.from')} value={draft.from} onChange={set('from')} error={errors.from} />
        <TextField id="audit-to" type="datetime-local" label={t('audit.field.to')} value={draft.to} onChange={set('to')} error={errors.to} />
      </div>
      <div className="flex flex-wrap gap-3">
        <Button type="submit" variant="primary">
          {t('audit.apply')}
        </Button>
        <Button onClick={reset}>{t('audit.reset')}</Button>
      </div>
    </form>
  );
}

function outcomeOf(e: AuditEvent): string | null {
  const v = (e.metadata as Record<string, unknown>).outcome;
  return typeof v === 'string' ? v : null;
}

function AuditTable({ workspaceId, filters }: { workspaceId: string; filters: AuditFilters }) {
  const { t } = useTranslation();
  const query = useInfiniteQuery({
    queryKey: auditKeys.list(workspaceId, filters),
    queryFn: ({ pageParam, signal }) =>
      auditApi.list(workspaceId, { ...filters, limit: AUDIT_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = query.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Object.keys(filters).length > 0;

  const columns: Column<AuditEvent>[] = [
    { id: 'time', header: t('audit.col.time'), cell: (e) => <Timestamp value={e.created_at} />, className: 'whitespace-nowrap' },
    {
      id: 'action',
      header: t('audit.col.action'),
      rowHeader: true,
      cell: (e) => <span className="break-all font-mono text-xs">{e.action}</span>,
    },
    {
      id: 'actor',
      header: t('audit.col.actor'),
      cell: (e) => (
        <span className="flex flex-col gap-1">
          <span>{t(`audit.enums.actor_type.${e.actor_type}`, { defaultValue: e.actor_type })}</span>
          {e.actor_id ? <span className="break-all font-mono text-xs text-fg-muted">{e.actor_id}</span> : null}
        </span>
      ),
    },
    {
      id: 'resource',
      header: t('audit.col.resource'),
      cell: (e) => (
        <span className="flex flex-col gap-1">
          <span className="font-mono text-xs">{e.resource_type}</span>
          <span className="break-all font-mono text-xs text-fg-muted">{e.resource_id}</span>
        </span>
      ),
    },
    {
      id: 'outcome',
      header: t('audit.col.outcome'),
      cell: (e) => {
        const o = outcomeOf(e);
        if (!o) return '—';
        const tone = o === 'success' ? 'success' : o === 'denied' || o === 'failure' || o === 'error' ? 'danger' : 'neutral';
        return <Badge tone={tone}>{t(`audit.enums.outcome.${o}`, { defaultValue: o })}</Badge>;
      },
    },
    {
      id: 'details',
      header: t('audit.col.details'),
      cell: (e) =>
        Object.keys(e.metadata).length > 0 ? (
          <details>
            <summary className="min-h-touch cursor-pointer py-2 text-sm font-medium text-link">
              {t('audit.metadata')}
              <span className="sr-only">: {e.action}</span>
            </summary>
            <CodeBlock code={JSON.stringify(e.metadata, null, 2)} label={t('audit.metadataLabel', { action: e.action })} language="json" maxHeightClass="max-h-64" />
          </details>
        ) : (
          '—'
        ),
      className: 'min-w-[14rem]',
    },
  ];

  if (query.isPending) return <LoadingState label={t('audit.loading')} />;
  if (query.isError && items.length === 0) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  if (items.length === 0) {
    return <EmptyState icon="audit" title={filtered ? t('audit.emptyFilteredTitle') : t('audit.emptyTitle')} description={filtered ? t('audit.emptyFiltered') : undefined} />;
  }
  return (
    <>
      <Table
        caption={t('audit.tableCaption')}
        columns={columns}
        rows={items}
        rowKey={(e) => e.id}
        minWidthClass="min-w-[60rem]"
        rowProps={(e) => ({ 'data-audit-id': e.id })}
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

/** P7-13 Audit: журнал аудита workspace с фильтрами (в URL) и cursor-пагинацией. */
export function AuditPage() {
  const { t } = useTranslation();
  const { workspaceId, membership } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const search = params.toString();
  const filters = useMemo(() => filtersFromSearch(new URLSearchParams(search)), [search]);
  if (!workspaceId || !membership) return <WorkspaceNotFound />;
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6">
      <PageHeading title={t('audit.title')} />
      <p className="-mt-4 text-sm text-fg-muted">{t('audit.intro')}</p>
      <FiltersForm search={search} filters={filters} onApply={(f) => setParams(searchFromFilters(f))} />
      <section aria-labelledby="audit-results-title" className="flex flex-col gap-3">
        <h2 id="audit-results-title" className="text-lg font-semibold">
          {t('audit.results')}
        </h2>
        <AuditTable key={`${workspaceId}?${search}`} workspaceId={workspaceId} filters={filters} />
      </section>
    </section>
  );
}
