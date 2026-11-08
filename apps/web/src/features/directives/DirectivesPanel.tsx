import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { directiveKeys, directivesApi, directiveTypesFor } from '../../api/directives';
import type { Run, TaskSummary } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { useRunEvents } from '../../api/useRunEvents';
import { Dialog, DialogTrigger, EmptyState, ErrorState, LoadMore, LoadingState, Timestamp } from '../../components/ui';
import { can } from '../../lib/permissions';
import { DirectiveDetails, directiveTargetLabel } from './DirectiveDetails';
import { DirectiveDialogContent } from './DirectiveDialog';

const PAGE_SIZE = 20;
const DIRECTIVE_EVENTS = new Set(['directive.impact_ready', 'directive.state_changed']);

interface Props {
  run: Run;
  role: string;
  workspaceId: string;
  tasks: readonly TaskSummary[];
  taskHref: (taskId: string) => string;
  approvalsHref: string;
  isTerminal: () => boolean;
}

/** P7-07: поручения run — список с ходом обработки и создание нового (impact preview → подтверждение). */
export function DirectivesPanel({ run, role, workspaceId, tasks, taskHref, approvalsHref, isTerminal }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [summaries, setSummaries] = useState<ReadonlyMap<string, string>>(new Map());
  const [open, setOpen] = useState(false);
  const titles = new Map(tasks.map((x) => [x.id, x.title]));
  const taskTitle = (id: string) => titles.get(id);
  const canCreate = can(role, 'directive:create') && directiveTypesFor(role).length > 0;
  const canConfirm = can(role, 'directive:confirm');

  const onEvents = useCallback(
    (events: RunEvent[]) => {
      const ready = events.filter((e) => e.type === 'directive.impact_ready' && typeof e.payload.summary === 'string');
      if (ready.length > 0) {
        setSummaries((prev) => {
          const next = new Map(prev);
          for (const e of ready) next.set(String(e.payload.directive_id), String(e.payload.summary));
          return next;
        });
      }
      if (events.some((e) => DIRECTIVE_EVENTS.has(e.type))) void queryClient.invalidateQueries({ queryKey: directiveKeys.all(run.id) });
    },
    [queryClient, run.id],
  );
  useRunEvents(run.id, { onEvents, isTerminal, limit: 0 });

  const list = useInfiniteQuery({
    queryKey: directiveKeys.list(run.id),
    queryFn: ({ pageParam, signal }) => directivesApi.list(run.id, { limit: PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

  return (
    <section aria-labelledby="directives-title" className="flex flex-col gap-3" data-testid="directives-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="directives-title" className="text-lg font-semibold">
          {t('directives.title')}
        </h2>
        {canCreate ? (
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger className="btn-primary">{t('directives.new')}</DialogTrigger>
            {open ? (
              <DirectiveDialogContent
                run={run}
                role={role}
                workspaceId={workspaceId}
                tasks={tasks}
                impactSummaries={summaries}
                taskHref={taskHref}
                approvalsHref={approvalsHref}
                canConfirm={canConfirm}
              />
            ) : null}
          </Dialog>
        ) : null}
      </div>
      <p className="text-sm text-fg-muted">{t('directives.explain')}</p>
      {list.isPending ? (
        <LoadingState label={t('directives.loading')} />
      ) : list.isError && items.length === 0 ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState headingLevel={3} title={t('directives.emptyTitle')} description={canCreate ? t('directives.emptyDescription') : t('directives.readOnly')} />
      ) : (
        <ul className="flex flex-col gap-3" aria-label={t('directives.listLabel')}>
          {items.map((d) => (
            <li key={d.id} className="card flex flex-col gap-2" data-testid="directive-card" data-directive-id={d.id}>
              <article aria-labelledby={`directive-${d.id}`} className="flex flex-col gap-2">
                <h3 id={`directive-${d.id}`} className="font-semibold">
                  {t(`directives.enums.type.${d.directive_type}`, { defaultValue: d.directive_type })}
                  <span className="font-normal text-fg-muted"> · {directiveTargetLabel(t, d, taskTitle)}</span>
                </h3>
                <p className="whitespace-pre-wrap break-words text-sm">{d.message}</p>
                <p className="text-xs text-fg-muted">
                  {d.submitted_by.display_name} · <Timestamp value={d.created_at} />
                </p>
                <DirectiveDetails
                  directive={d}
                  impactSummary={summaries.get(d.id)}
                  taskTitle={taskTitle}
                  taskHref={taskHref}
                  approvalsHref={approvalsHref}
                  canAct={canConfirm}
                  headingLevel={4}
                />
              </article>
            </li>
          ))}
        </ul>
      )}
      {list.hasNextPage || list.isFetchNextPageError ? (
        <LoadMore
          shown={items.length}
          hasNextPage={list.hasNextPage}
          isFetchingNextPage={list.isFetchingNextPage}
          nextPageError={list.isFetchNextPageError ? list.error : null}
          onLoadMore={() => void list.fetchNextPage()}
        />
      ) : null}
    </section>
  );
}
