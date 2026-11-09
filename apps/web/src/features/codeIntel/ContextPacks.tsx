/**
 * Context Packs — «что использовал агент»: run проекта → задача → попытка → manifest Context Pack
 * (B3 `getAttemptContextPack`): разделы, файлы/символы со строками, commit задачи и снимка, признак
 * устаревания, размер и бюджет, пропущенные фрагменты с причинами; ссылки ведут в Code Explorer.
 * Сверху — агрегаты `analytics.context` проекта за 30 дней (сколько pack собрано, размеры, доля устаревших).
 * Текст фрагментов в manifest не входит (артефакт context_pack) — здесь не показывается.
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { analyticsApi, analyticsKeys } from '../../api/analytics';
import { isApiError } from '../../api/client';
import { runKeys, runsApi } from '../../api/runs';
import { taskKeys, tasksApi, type AttemptContextPack } from '../../api/tasks';
import { Badge, Callout, EmptyState, ErrorState, Skeleton, Table, Timestamp, type Column } from '../../components/ui';
import { formatBytes } from '../projects/RepositoryExplorer';
import { fragmentLines, isRepositoryPath, summarizePack } from './model';
import { Sha } from './shared';
import type { TabProps } from './SymbolSearch';

export const PACK_RUNS_LIMIT = 50;

type Fragment = AttemptContextPack['fragments'][number];
type Omission = AttemptContextPack['omitted'][number];

function ContextAggregates({ workspaceId, projectId }: { workspaceId: string; projectId: string }) {
  const { t, i18n } = useTranslation();
  const query = { project_id: projectId };
  const summary = useQuery({
    queryKey: analyticsKeys.summary(workspaceId, query),
    queryFn: ({ signal }) => analyticsApi.summary(workspaceId, query, signal),
    staleTime: 60_000,
  });
  if (summary.isPending) return <Skeleton lines={2} />;
  if (summary.isError) return <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />;
  const c = summary.data.context;
  const pct = (v: number | null) => (v === null ? t('codeIntel.context.agg.notMeasured') : new Intl.NumberFormat(i18n.language, { style: 'percent', maximumFractionDigits: 1 }).format(v));
  const bytes = (v: number | null) => (v === null ? t('codeIntel.context.agg.notMeasured') : formatBytes(v, i18n.language));
  return (
    <section aria-labelledby="ci-ctx-agg" className="rounded-lg border border-border bg-surface p-3" data-testid="context-aggregates">
      <h3 id="ci-ctx-agg" className="text-sm font-semibold">
        {t('codeIntel.context.agg.title')}
      </h3>
      <p className="text-xs text-fg-muted">{t('codeIntel.context.agg.source')}</p>
      {c.packs === 0 ? (
        <p className="mt-2 text-sm text-fg-muted">{t('codeIntel.context.agg.none')}</p>
      ) : (
        <dl className="mt-2 grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto,1fr,auto,1fr]">
          <dt className="text-fg-muted">{t('codeIntel.context.agg.packs')}</dt>
          <dd>{c.packs}</dd>
          <dt className="text-fg-muted">{t('codeIntel.context.agg.fragments')}</dt>
          <dd>{c.fragments}</dd>
          <dt className="text-fg-muted">{t('codeIntel.context.agg.avgSize')}</dt>
          <dd>{bytes(c.avg_size_bytes)}</dd>
          <dt className="text-fg-muted">{t('codeIntel.context.agg.maxSize')}</dt>
          <dd>{bytes(c.max_size_bytes)}</dd>
          <dt className="text-fg-muted">{t('codeIntel.context.agg.staleRatio')}</dt>
          <dd>{pct(c.stale_fragment_ratio)}</dd>
          <dt className="text-fg-muted">{t('codeIntel.context.agg.usefulness')}</dt>
          <dd>
            {pct(c.usefulness.rate)}
            {c.usefulness.rate !== null ? (
              <span className="block text-xs text-fg-muted">
                {t('codeIntel.context.agg.usefulnessBasis', { packs: c.usefulness.packs_evaluated, referenced: c.usefulness.fragments_referenced, total: c.usefulness.fragments_total })}
              </span>
            ) : null}
          </dd>
        </dl>
      )}
    </section>
  );
}

function Manifest({ pack, href }: { pack: AttemptContextPack; href: TabProps['href'] }) {
  const { t, i18n } = useTranslation();
  const s = summarizePack(pack);
  const bytes = (n: number | null) => (n === null ? '—' : formatBytes(n, i18n.language));
  const fileLink = (f: { path: string; commit: string | null }, lines: { from: number; to: number } | null) => {
    if (!isRepositoryPath(f.path)) return <span className="break-all font-mono">{f.path}</span>;
    // Фрагмент открывается в commit, из которого он взят (иначе — commit снимка/задачи).
    const commit = (f.commit ?? pack.snapshot_commit ?? pack.base_commit).toLowerCase();
    return (
      <Link className="link break-all font-mono" to={href({ tab: 'explorer', commit, file: f.path, line: lines?.from ?? null, end: lines?.to ?? null })}>
        {f.path}
        {lines ? <span className="sr-only">{t('codeIntel.context.linesSr', { from: lines.from, to: lines.to })}</span> : null}
      </Link>
    );
  };
  const fragmentColumns: Column<Fragment>[] = [
    { id: 'path', header: t('codeIntel.context.col.path'), rowHeader: true, sortValue: (f) => f.path, cell: (f) => fileLink(f, fragmentLines(f.lines)) },
    {
      id: 'lines',
      header: t('codeIntel.context.col.lines'),
      sortValue: (f) => fragmentLines(f.lines)?.from ?? null,
      cell: (f) => {
        const l = fragmentLines(f.lines);
        return l ? t('codeIntel.context.lines', { from: l.from, to: l.to }) : <span className="text-fg-muted">{t('codeIntel.context.wholeFile')}</span>;
      },
    },
    { id: 'kind', header: t('codeIntel.context.col.kind'), sortValue: (f) => f.kind, cell: (f) => t(`codeIntel.sourceKind.${f.kind}`, { defaultValue: f.kind }) },
    { id: 'ref', header: t('codeIntel.context.col.ref'), cell: (f) => (f.source_ref ? <span className="break-all font-mono text-xs">{f.source_ref}</span> : <span className="text-fg-muted">—</span>) },
    { id: 'commit', header: t('codeIntel.context.col.commit'), cell: (f) => <Sha sha={f.commit} length={10} /> },
    { id: 'bytes', header: t('codeIntel.context.col.bytes'), sortValue: (f) => f.bytes, cell: (f) => bytes(f.bytes) },
    {
      id: 'stale',
      header: t('codeIntel.context.col.stale'),
      sortValue: (f) => (f.stale ? 1 : 0),
      cell: (f) => (f.stale ? <Badge tone="warning">{t('codeIntel.context.staleYes')}</Badge> : <span className="text-fg-muted">{t('codeIntel.context.staleNo')}</span>),
    },
  ];
  const omittedColumns: Column<Omission>[] = [
    { id: 'path', header: t('codeIntel.context.col.path'), rowHeader: true, sortValue: (o) => o.path, cell: (o) => fileLink(o, null) },
    { id: 'section', header: t('codeIntel.context.col.section'), sortValue: (o) => o.section, cell: (o) => t(`codeIntel.section.${o.section}`, { defaultValue: o.section }) },
    { id: 'kind', header: t('codeIntel.context.col.kind'), cell: (o) => t(`codeIntel.sourceKind.${o.kind}`, { defaultValue: o.kind }) },
    { id: 'reason', header: t('codeIntel.context.col.reason'), sortValue: (o) => o.reason, cell: (o) => t(`codeIntel.omitReason.${o.reason}`, { defaultValue: o.reason }) },
  ];
  return (
    <div className="flex flex-col gap-4" data-testid="context-manifest">
      <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-1 text-sm md:grid-cols-[auto,1fr,auto,1fr]">
        <dt className="text-fg-muted">{t('codeIntel.context.baseCommit')}</dt>
        <dd>
          <Sha sha={pack.base_commit} />
        </dd>
        <dt className="text-fg-muted">{t('codeIntel.context.snapshotCommit')}</dt>
        <dd>
          <Sha sha={pack.snapshot_commit} />
        </dd>
        <dt className="text-fg-muted">{t('codeIntel.context.size')}</dt>
        <dd data-testid="pack-size">
          {formatBytes(pack.size_bytes, i18n.language)}
          {pack.budget?.max_bytes != null ? <span className="text-fg-muted"> {t('codeIntel.context.ofBudget', { budget: formatBytes(pack.budget.max_bytes, i18n.language) })}</span> : null}
        </dd>
        <dt className="text-fg-muted">{t('codeIntel.context.counts')}</dt>
        <dd>{t('codeIntel.context.countsValue', { fragments: pack.fragments_total, omitted: pack.omitted_total })}</dd>
        <dt className="text-fg-muted">{t('codeIntel.context.tokenBudget')}</dt>
        <dd>{new Intl.NumberFormat(i18n.language).format(pack.token_budget)}</dd>
        <dt className="text-fg-muted">{t('codeIntel.context.docs')}</dt>
        <dd>
          {t(`enums.documentation_mode.${pack.documentation_mode}`, { defaultValue: pack.documentation_mode })}
          {pack.docs ? <span className="text-fg-muted"> · {t(`codeIntel.context.docsState.${pack.docs}`)}</span> : null}
        </dd>
        <dt className="text-fg-muted">{t('codeIntel.context.created')}</dt>
        <dd>
          <Timestamp value={pack.created_at} />
        </dd>
        <dt className="text-fg-muted">{t('codeIntel.context.policy')}</dt>
        <dd>
          {t('codeIntel.context.policyValue', { version: pack.policy_version })}
          {pack.stale_policy ? <span className="text-fg-muted"> · {t(`codeIntel.context.stalePolicy.${pack.stale_policy}`)}</span> : null}
          {pack.cache_hit === true ? <span className="text-fg-muted"> · {t('codeIntel.context.cacheHit')}</span> : null}
        </dd>
      </dl>
      {s.staleSnapshot ? (
        <Callout tone="warning" title={t('codeIntel.context.staleTitle')}>
          {t('codeIntel.context.stale', { base: pack.base_commit.slice(0, 12), snapshot: (pack.snapshot_commit ?? '').slice(0, 12) })}
        </Callout>
      ) : null}
      {s.staleFragments > 0 ? <p className="text-sm text-fg">{t('codeIntel.context.staleFragments', { count: s.staleFragments })}</p> : null}
      {pack.warnings.length > 0 ? (
        <Callout tone="info" title={t('codeIntel.context.warningsTitle')}>
          <span className="font-mono">{pack.warnings.join(', ')}</span>
        </Callout>
      ) : null}
      {s.bySection.length === 0 ? (
        <p className="text-sm text-fg-muted">{t('codeIntel.context.noFragments')}</p>
      ) : (
        s.bySection.map((sec) => (
          <section key={sec.section} aria-labelledby={`ci-sec-${sec.section}`} data-section={sec.section}>
            <h4 id={`ci-sec-${sec.section}`} className="mb-1 text-sm font-semibold">
              {t(`codeIntel.section.${sec.section}`, { defaultValue: sec.section })}{' '}
              <span className="font-normal text-fg-muted">{t('codeIntel.context.sectionMeta', { count: sec.fragments.length, size: formatBytes(sec.bytes, i18n.language) })}</span>
            </h4>
            <Table
              caption={t('codeIntel.context.sectionCaption', { section: t(`codeIntel.section.${sec.section}`, { defaultValue: sec.section }) })}
              columns={fragmentColumns}
              rows={sec.fragments}
              rowKey={(f) => `${f.path}:${(f.lines ?? []).join('-')}:${f.kind}:${f.source_ref ?? ''}`}
              density="compact"
              minWidthClass="min-w-[48rem]"
            />
          </section>
        ))
      )}
      {s.fragmentsTruncated ? <p className="text-sm text-fg-muted">{t('codeIntel.context.fragmentsMore', { shown: pack.fragments.length, total: pack.fragments_total })}</p> : null}
      <section aria-labelledby="ci-omitted" data-testid="context-omitted">
        <h4 id="ci-omitted" className="mb-1 text-sm font-semibold">
          {t('codeIntel.context.omittedTitle', { count: pack.omitted_total })}
        </h4>
        {pack.omitted.length === 0 ? (
          <p className="text-sm text-fg-muted">{t('codeIntel.context.omittedNone')}</p>
        ) : (
          <>
            <p className="mb-2 text-sm text-fg">
              {s.omittedByReason.map((r) => t('codeIntel.context.reasonCount', { reason: t(`codeIntel.omitReason.${r.reason}`, { defaultValue: r.reason }), count: r.count })).join(' · ')}
            </p>
            <Table
              caption={t('codeIntel.context.omittedCaption')}
              columns={omittedColumns}
              rows={pack.omitted}
              rowKey={(o) => `${o.path}:${o.section}:${o.kind}:${o.reason}`}
              density="compact"
              minWidthClass="min-w-[36rem]"
            />
            {s.omittedTruncated ? <p className="mt-1 text-sm text-fg-muted">{t('codeIntel.context.omittedMore', { shown: pack.omitted.length, total: pack.omitted_total })}</p> : null}
          </>
        )}
      </section>
      <p className="text-xs text-fg-muted">
        {t('codeIntel.context.hash')} <Sha sha={pack.content_hash} length={16} /> · {t('codeIntel.context.repoNote')}
      </p>
    </div>
  );
}

export function ContextPacks({ workspaceId, projectId, params, update, href }: TabProps) {
  const { t } = useTranslation();
  const uid = useId();
  const runs = useQuery({
    queryKey: [...runKeys.list(workspaceId, projectId), 'code-intel', PACK_RUNS_LIMIT] as const,
    queryFn: ({ signal }) => runsApi.list(workspaceId, { project_id: projectId, limit: PACK_RUNS_LIMIT }, signal),
  });
  const tasks = useQuery({
    queryKey: runKeys.tasks(params.run),
    queryFn: ({ signal }) => runsApi.tasks(params.run, {}, signal),
    enabled: Boolean(params.run),
  });
  const attempts = useQuery({
    queryKey: [...taskKeys.attempts(params.task), 'code-intel'] as const,
    queryFn: ({ signal }) => tasksApi.attempts(params.task, { limit: 50 }, signal),
    enabled: Boolean(params.task),
  });
  const attemptList = [...(attempts.data?.items ?? [])].sort((a, b) => b.attempt_no - a.attempt_no);
  const attemptId = params.attempt || attemptList[0]?.id || '';
  const pack = useQuery({
    queryKey: taskKeys.contextPack(params.task, attemptId),
    queryFn: ({ signal }) => tasksApi.contextPack(params.task, attemptId, signal),
    enabled: Boolean(params.task && attemptId),
    retry: (count, e) => !(isApiError(e) && e.status === 404) && count < 2,
  });
  // Последняя попытка выбирается сама, пока пользователь не выбрал другую.
  const latestId = attemptList[0]?.id;
  useEffect(() => {
    if (!params.attempt && latestId) update({ attempt: latestId });
  }, [params.attempt, latestId, update]);

  const runItems = runs.data?.items ?? [];
  const runKnown = runItems.some((r) => r.id === params.run);
  const taskItems = tasks.data?.tasks ?? [];

  let manifest;
  if (!params.run) manifest = <p className="text-sm text-fg-muted">{t('codeIntel.context.pickRun')}</p>;
  else if (!params.task) manifest = <p className="text-sm text-fg-muted">{t('codeIntel.context.pickTask')}</p>;
  else if (attempts.isPending) manifest = <Skeleton lines={3} />;
  else if (attempts.isError) manifest = <ErrorState error={attempts.error} onRetry={() => void attempts.refetch()} />;
  else if (attemptList.length === 0) manifest = <p className="text-sm text-fg-muted">{t('codeIntel.context.noAttempts')}</p>;
  else if (pack.isPending) manifest = <Skeleton lines={4} />;
  else if (pack.isError)
    manifest =
      isApiError(pack.error) && pack.error.status === 404 ? (
        <EmptyState title={t('codeIntel.context.noneTitle')} description={t('codeIntel.context.none')} icon="layers" />
      ) : (
        <ErrorState error={pack.error} onRetry={() => void pack.refetch()} />
      );
  else manifest = <Manifest pack={pack.data} href={href} />;

  return (
    <div className="flex flex-col gap-4" data-testid="context-packs">
      <ContextAggregates workspaceId={workspaceId} projectId={projectId} />
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor={`${uid}-run`} className="text-sm font-medium text-fg">
            {t('codeIntel.context.run')}
          </label>
          {runs.isPending ? (
            <Skeleton lines={1} className="w-56" />
          ) : runs.isError ? (
            <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
          ) : (
            <select
              id={`${uid}-run`}
              className="select max-w-full sm:max-w-[28rem]"
              value={params.run}
              onChange={(e) => update({ run: e.target.value, task: '', attempt: '' })}
            >
              <option value="">{runItems.length === 0 ? t('codeIntel.context.noRuns') : t('codeIntel.context.selectRun')}</option>
              {params.run && !runKnown ? <option value={params.run}>{t('codeIntel.context.runById', { id: params.run.slice(0, 8) })}</option> : null}
              {runItems.map((r) => (
                <option key={r.id} value={r.id}>
                  {t('codeIntel.context.runOption', { objective: r.objective.length > 70 ? `${r.objective.slice(0, 69)}…` : r.objective, status: t(`enums.run_status.${r.status}`, { defaultValue: r.status }) })}
                </option>
              ))}
            </select>
          )}
        </div>
        {params.run ? (
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor={`${uid}-task`} className="text-sm font-medium text-fg">
              {t('codeIntel.context.task')}
            </label>
            {tasks.isPending ? (
              <Skeleton lines={1} className="w-56" />
            ) : tasks.isError ? (
              <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} />
            ) : (
              <select id={`${uid}-task`} className="select max-w-full sm:max-w-[28rem]" value={params.task} onChange={(e) => update({ task: e.target.value, attempt: '' })}>
                <option value="">{taskItems.length === 0 ? t('codeIntel.context.noTasks') : t('codeIntel.context.selectTask')}</option>
                {taskItems.map((x) => (
                  <option key={x.id} value={x.id}>
                    {t('codeIntel.context.taskOption', { title: x.title.length > 70 ? `${x.title.slice(0, 69)}…` : x.title, role: t(`enums.agent_role.${x.agent_role}`, { defaultValue: x.agent_role }) })}
                  </option>
                ))}
              </select>
            )}
          </div>
        ) : null}
        {params.task && attemptList.length > 0 ? (
          <div className="flex flex-col gap-1">
            <label htmlFor={`${uid}-attempt`} className="text-sm font-medium text-fg">
              {t('codeIntel.context.attempt')}
            </label>
            <select id={`${uid}-attempt`} className="select" value={attemptId} onChange={(e) => update({ attempt: e.target.value })}>
              {attemptList.map((a) => (
                <option key={a.id} value={a.id}>
                  {t('codeIntel.context.attemptOption', { n: a.attempt_no, status: t(`enums.task_status.${a.status}`, { defaultValue: a.status }) })}
                </option>
              ))}
            </select>
          </div>
        ) : null}
      </div>
      <section aria-labelledby="ci-manifest" className="flex flex-col gap-2">
        <h3 id="ci-manifest" className="text-base font-semibold">
          {t('codeIntel.context.manifestTitle')}
        </h3>
        {manifest}
      </section>
    </div>
  );
}
