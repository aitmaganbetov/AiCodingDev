/**
 * «Стандарты и соответствие» (F7, план §3.8, ТЗ §13): реестр стандартов и контроли выбранного проекта
 * (применимость, блокирующий статус, утверждение — `listProjectControls`; глобального реестра в API
 * нет), фильтры по стандарту/контролю/применимости/блокированию, проекты и запуски со ссылками на
 * матрицу соответствия run (evidence, отсутствующие доказательства, отчёты JSON/Markdown).
 * Баннер «не сертификация» — постоянный.
 */
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { projectsApi } from '../../api/projects';
import { standardsApi, standardsKeys, type ControlApprovalState, type ProjectControl } from '../../api/standards';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { Badge } from '../../components/ui/Badge';
import { Table, type Column } from '../../components/ui/Table';
import { Tile } from '../../components/ui/Tile';
import { useCurrentWorkspace } from '../../workspace';
import { RecentRuns, TextLink, workspacePath } from '../quality/shared';
import { ComplianceDisclaimer } from './ComplianceDisclaimer';
import { filterControls, standardsRegistry, type ControlFilters, type StandardEntry } from './model';
import { BlockingCell } from './ProjectControlsPage';

const PROJECTS_PAGE_SIZE = 50;
const CONTROLS_LIMIT = 200;
const APPROVAL_STATES: readonly ControlApprovalState[] = ['draft', 'approved', 'missing'];
const APPLICABILITY = ['APPLICABLE', 'NOT_APPLICABLE', 'PENDING_REVIEW'] as const;

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
            <span className="min-w-0 break-words font-medium">{p.name}</span>
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

function Registry({ entries, standard, onPick }: { entries: StandardEntry[]; standard: string; onPick: (code: string) => void }) {
  const { t } = useTranslation();
  if (entries.length === 0) return <p className="text-sm text-fg-muted">{t('compliance.registry.empty')}</p>;
  return (
    <ul className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-3" aria-label={t('compliance.registry.title')} data-testid="standards-registry">
      {entries.map((e) => (
        <li key={`${e.code}@${e.edition}`} className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-surface p-3" data-standard={e.code}>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <span className="break-words font-semibold">
              {e.code} <span className="font-normal text-fg-muted">({e.edition})</span>
            </span>
            <button type="button" className="btn-ghost btn-sm" aria-pressed={standard === e.code} onClick={() => onPick(standard === e.code ? '' : e.code)}>
              {standard === e.code ? t('compliance.registry.unfilter') : t('compliance.registry.filter')}
              <span className="sr-only">: {e.code}</span>
            </button>
          </div>
          {e.title ? <span className="break-words text-sm text-fg-muted">{e.title}</span> : null}
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-sm">
            <dt className="text-fg-muted">{t('compliance.registry.controls')}</dt>
            <dd className="tabular-nums">{e.controls}</dd>
            <dt className="text-fg-muted">{t('compliance.registry.applicable')}</dt>
            <dd className="tabular-nums">{e.applicable}</dd>
            <dt className="text-fg-muted">{t('compliance.registry.notApplicable')}</dt>
            <dd className="tabular-nums">{e.notApplicable}</dd>
            <dt className="text-fg-muted">{t('compliance.registry.pending')}</dt>
            <dd className="tabular-nums" data-testid="registry-pending">
              {e.pendingApproval}
            </dd>
            <dt className="text-fg-muted">{t('compliance.registry.blocking')}</dt>
            <dd className="tabular-nums">{e.blocking}</dd>
          </dl>
          <p className="text-xs text-fg-muted">
            {Object.entries(e.methods)
              .map(([m, n]) => `${t(`compliance.enums.method.${m}`, { defaultValue: m })}: ${n}`)
              .join(' · ')}
          </p>
        </li>
      ))}
    </ul>
  );
}

function ControlsTable({ rows }: { rows: ProjectControl[] }) {
  const { t } = useTranslation();
  const columns: Column<ProjectControl>[] = [
    {
      id: 'standard',
      header: t('compliance.col.standard'),
      cell: (c) => (
        <span>
          {c.standard_code} <span className="text-fg-muted">({c.edition})</span>
        </span>
      ),
      sortValue: (c) => c.standard_code,
    },
    {
      id: 'control',
      header: t('compliance.col.control'),
      rowHeader: true,
      cell: (c) => (
        <span>
          <span className="font-mono text-xs">{c.control_code}</span> <span className="text-fg-muted">§ {c.clause_ref}</span>
          <span className="block text-xs">{c.description}</span>
        </span>
      ),
      sortValue: (c) => c.control_code,
    },
    {
      id: 'applicability',
      header: t('compliance.col.applicability'),
      cell: (c) => (
        <div className="flex flex-col items-start gap-1">
          <Badge tone={c.approval_state === 'approved' ? 'success' : 'warning'}>{t(`compliance.enums.approval_state.${c.approval_state}`)}</Badge>
          <span className="text-xs">{t(`compliance.enums.applicability.${c.applicability}`, { defaultValue: c.applicability })}</span>
        </div>
      ),
    },
    {
      id: 'check',
      header: t('compliance.col.check'),
      cell: (c) => (
        <span>
          {t(`compliance.enums.method.${c.verification_method}`)}
          {c.gate_type ? <span className="block font-mono text-xs text-fg-muted">{c.gate_type}</span> : null}
        </span>
      ),
    },
    { id: 'blocking', header: t('compliance.controls.blocking'), cell: (c) => <BlockingCell control={c} />, sortValue: (c) => (c.effective_blocking ? 0 : 1) },
  ];
  return (
    <Table
      caption={t('compliance.index.controlsCaption')}
      columns={columns}
      rows={rows}
      rowKey={(c) => c.control_id}
      rowProps={(c) => ({ 'data-control': c.control_code })}
      empty={t('compliance.controls.empty')}
      minWidthClass="min-w-[52rem]"
      density="compact"
    />
  );
}

function ProjectStandards({
  workspaceId,
  projectId,
  filters,
  setParam,
}: {
  workspaceId: string;
  projectId: string;
  filters: ControlFilters & { approval: string };
  setParam: (k: string, v: string) => void;
}) {
  const { t } = useTranslation();
  const query = filters.approval ? { approval_state: filters.approval as ControlApprovalState } : {};
  const controls = useQuery({
    queryKey: standardsKeys.controls(projectId, { ...query, limit: CONTROLS_LIMIT }),
    queryFn: ({ signal }) => standardsApi.controls(projectId, { ...query, limit: CONTROLS_LIMIT }, signal),
  });
  const items = useMemo(() => controls.data?.items ?? [], [controls.data]);
  const registry = useMemo(() => standardsRegistry(items), [items]);
  const filtered = useMemo(() => filterControls(items, filters), [items, filters]);
  const standards = registry.map((e) => e.code);
  return (
    <div className="flex flex-col gap-4">
      <Tile
        id="compliance-registry"
        icon="book"
        headingLevel={2}
        title={t('compliance.registry.title')}
        loading={controls.isPending}
        error={controls.error}
        onRetry={() => void controls.refetch()}
        source={t(controls.data?.next_cursor ? 'compliance.registry.sourcePartial' : 'compliance.registry.source', { count: items.length })}
      >
        {controls.data?.quality_profile ? (
          <p className="text-sm">
            {t('quality.profile')}: {t(`enums.quality_profile.${controls.data.quality_profile}`)}
          </p>
        ) : null}
        {controls.data?.disclaimer ? <p className="text-xs text-fg-muted">{controls.data.disclaimer}</p> : null}
        <Registry entries={registry} standard={filters.standard} onPick={(c) => setParam('standard', c)} />
      </Tile>
      <section aria-labelledby="compliance-controls-title" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="compliance-controls-title" className="text-lg font-semibold">
            {t('compliance.index.controlsTitle')}
          </h2>
          <TextLink to={workspacePath(workspaceId, `compliance/projects/${projectId}/controls`)}>{t('compliance.index.manageControls')}</TextLink>
        </div>
        <div role="group" aria-label={t('compliance.index.controlFilters')} className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="cmp-standard" className="text-sm font-medium">
              {t('compliance.col.standard')}
            </label>
            <select id="cmp-standard" className="select" value={filters.standard} onChange={(e) => setParam('standard', e.target.value)}>
              <option value="">{t('quality.finding.any')}</option>
              {[...new Set([...standards, ...(filters.standard ? [filters.standard] : [])])].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="cmp-q" className="text-sm font-medium">
              {t('compliance.index.controlSearch')}
            </label>
            <input id="cmp-q" type="search" className="input" value={filters.q} onChange={(e) => setParam('q', e.target.value)} />
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="cmp-approval" className="text-sm font-medium">
              {t('compliance.controls.approvalState')}
            </label>
            <select id="cmp-approval" className="select" value={filters.approval} onChange={(e) => setParam('approval', e.target.value)}>
              <option value="">{t('quality.finding.any')}</option>
              {APPROVAL_STATES.map((s) => (
                <option key={s} value={s}>
                  {t(`compliance.enums.approval_state.${s}`)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="cmp-applicability" className="text-sm font-medium">
              {t('compliance.col.applicability')}
            </label>
            <select id="cmp-applicability" className="select" value={filters.applicability} onChange={(e) => setParam('applicability', e.target.value)}>
              <option value="">{t('quality.finding.any')}</option>
              {APPLICABILITY.map((a) => (
                <option key={a} value={a}>
                  {t(`compliance.enums.applicability.${a}`)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1">
            <label htmlFor="cmp-blocking" className="text-sm font-medium">
              {t('compliance.controls.blocking')}
            </label>
            <select id="cmp-blocking" className="select" value={filters.blocking} onChange={(e) => setParam('blocking', e.target.value)}>
              <option value="">{t('quality.finding.any')}</option>
              <option value="yes">{t('compliance.controls.blocks')}</option>
              <option value="no">{t('compliance.controls.notBlocks')}</option>
            </select>
          </div>
        </div>
        {controls.isPending ? <LoadingState label={t('compliance.controls.loading')} /> : controls.isError ? null : <ControlsTable rows={filtered} />}
      </section>
    </div>
  );
}

/** Выбор проекта для реестра (по умолчанию — первый проект workspace). */
function useProjectPicker({ workspaceId, value, onChange }: { workspaceId: string; value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  const projects = useQuery({
    queryKey: ['projects', workspaceId, 'compliance-picker'],
    queryFn: ({ signal }) => projectsApi.list(workspaceId, { limit: 100 }, signal),
  });
  const items = projects.data?.items ?? [];
  const effective = value || items[0]?.id || '';
  const element = (
    <div className="flex min-w-0 flex-col gap-1 self-start">
      <label htmlFor="cmp-project" className="text-sm font-medium">
        {t('compliance.index.project')}
      </label>
      <select id="cmp-project" className="select" value={effective} onChange={(e) => onChange(e.target.value)} disabled={items.length === 0}>
        {items.length === 0 ? <option value="">{t('compliance.index.noProjects')}</option> : null}
        {items.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      {projects.isError ? <span className="text-xs text-danger-text">{t('quality.overview.projectsError')}</span> : null}
    </div>
  );
  return { effective, element, pending: projects.isPending };
}

export function ComplianceIndexPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };
  const blocking = params.get('blocking');
  const filters = {
    standard: params.get('standard') ?? '',
    applicability: params.get('applicability') ?? '',
    blocking: blocking === 'yes' || blocking === 'no' ? blocking : '',
    q: params.get('q') ?? '',
    approval: params.get('approval') ?? '',
  } as const;
  const picker = useProjectPicker({ workspaceId: workspaceId ?? '', value: params.get('project') ?? '', onChange: (v) => setParam('project', v) });
  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('compliance.title')} />
      <ComplianceDisclaimer className="mb-4" />
      <p className="mb-6 max-w-prose text-fg-muted">{t('compliance.index.description')}</p>
      <div className="mb-4 flex flex-wrap items-end gap-4">{picker.element}</div>
      {picker.pending ? (
        <LoadingState />
      ) : picker.effective ? (
        <ProjectStandards workspaceId={workspaceId} projectId={picker.effective} filters={filters} setParam={setParam} />
      ) : null}
      <div className="mt-8 grid grid-cols-1 gap-8 lg:grid-cols-2">
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
          <p className="mb-3 text-sm text-fg-muted">{t('compliance.index.runsHint')}</p>
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
