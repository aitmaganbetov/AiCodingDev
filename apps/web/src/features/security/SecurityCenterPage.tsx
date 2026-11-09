/**
 * Центр безопасности (F7, план §3.7; D-8): находки всех run workspace (`GET /findings`, B1) с
 * серверными фильтрами (severity, status, source, project, run) и клиентскими по загруженной выборке
 * (CWE, контроль), распределение по серьёзности и статусам исправления, сопоставления CWE/контролей
 * только из данных находок, риски зависимостей (gates SCA/SBOM) и секреты (SECRETS) по типу gate,
 * evidence проверок безопасности, решение по находке (FSM + RBAC + step-up) и переход к карточке.
 * Состояние фильтров и выбранной находки — в URL. Тексты находок — недоверенные данные (только текст).
 */
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router';
import { FINDING_SEVERITIES, FINDING_STATUSES, findingKeys, findingsApi, type Finding } from '../../api/findings';
import { projectsApi } from '../../api/projects';
import { runKeys, runsApi } from '../../api/runs';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { EmptyState, ErrorState, LoadingState } from '../../components/states';
import { StatusBadge } from '../../components/StatusBadge';
import { Button } from '../../components/ui/Button';
import { cx } from '../../components/ui/cx';
import { Icon } from '../../components/ui/Icon';
import { Timestamp } from '../../components/ui/Time';
import { SeverityBadge } from '../../components/ui/VerificationBadge';
import { VirtualList } from '../../components/ui/VirtualList';
import { useCurrentWorkspace } from '../../workspace';
import { FindingTransitionActions } from '../quality/FindingTransitions';
import { LiveStatus, useWorkspaceInvalidation } from '../quality/live';
import { Field, FieldList, Hash, TextLink, workspacePath } from '../quality/shared';
import { RUN_REPORTS_LIMIT, useEvidenceStatuses, useRunGateIndex, useWorkspaceFindings } from './data';
import {
  applyClientFilters,
  distinctRunIds,
  FINDING_SOURCES,
  filtersToQuery,
  parseFilters,
  securityKind,
  UNRESOLVED_STATUSES,
  withParam,
  type SecurityFilters,
} from './model';
import { KindTile, MappingsTile, RemediationTile, SecurityEvidenceTile, SeverityTile } from './SecurityPanels';

const PRESSED = 'aria-pressed:border-border-active aria-pressed:bg-accent-soft aria-pressed:text-accent-soft-fg';
const FILTER_OPTIONS_LIMIT = 100;
const LIVE_TYPES = ['run.completed'] as const;
const UNRESOLVED_VALUE = 'unresolved';

function statusSelectValue(status: SecurityFilters['status']): string {
  if (status.length === 0) return '';
  if (status.length === UNRESOLVED_STATUSES.length && UNRESOLVED_STATUSES.every((s) => status.includes(s))) return UNRESOLVED_VALUE;
  return status[0] ?? '';
}

function Filters({
  workspaceId,
  filters,
  setParams,
}: {
  workspaceId: string;
  filters: SecurityFilters;
  setParams: (update: (p: URLSearchParams) => URLSearchParams) => void;
}) {
  const { t } = useTranslation();
  const projects = useQuery({
    queryKey: ['projects', workspaceId, 'security-filter'],
    queryFn: ({ signal }) => projectsApi.list(workspaceId, { limit: FILTER_OPTIONS_LIMIT }, signal),
  });
  const runs = useQuery({
    queryKey: [...runKeys.list(workspaceId, filters.project || undefined), 'security-filter'],
    queryFn: ({ signal }) => runsApi.list(workspaceId, { limit: 50, ...(filters.project ? { project_id: filters.project } : {}) }, signal),
  });
  const toggleSeverity = (s: (typeof FINDING_SEVERITIES)[number]) =>
    setParams((p) => withParam(p, 'severity', filters.severity.includes(s) ? filters.severity.filter((x) => x !== s) : [...filters.severity, s]));
  const anyActive =
    filters.severity.length + filters.status.length + filters.source.length > 0 || Boolean(filters.project || filters.run || filters.cwe || filters.control);
  return (
    <div role="group" aria-label={t('security.filters.label')} className="mb-6 flex flex-col gap-4 rounded-lg border border-border bg-surface p-4">
      <div role="group" aria-label={t('security.filters.severity')} className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium" aria-hidden="true">
          {t('security.filters.severity')}
        </span>
        {FINDING_SEVERITIES.map((s) => {
          const pressed = filters.severity.includes(s);
          return (
            <button key={s} type="button" className={cx('btn-secondary btn-sm', PRESSED)} aria-pressed={pressed} onClick={() => toggleSeverity(s)} data-severity-filter={s}>
              {pressed ? <Icon name="check" className="h-4 w-4" /> : null}
              {t(`ds.severity.${s}`)}
            </button>
          );
        })}
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="security-status" className="text-sm font-medium">
            {t('security.filters.status')}
          </label>
          <select
            id="security-status"
            className="select"
            value={statusSelectValue(filters.status)}
            onChange={(e) =>
              setParams((p) => withParam(p, 'status', e.target.value === UNRESOLVED_VALUE ? UNRESOLVED_STATUSES : e.target.value ? [e.target.value] : []))
            }
          >
            <option value="">{t('security.filters.anyStatus')}</option>
            <option value={UNRESOLVED_VALUE}>{t('security.filters.unresolved')}</option>
            {FINDING_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`quality.enums.finding_status.${s}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="security-source" className="text-sm font-medium">
            {t('security.filters.source')}
          </label>
          <select
            id="security-source"
            className="select"
            value={filters.source[0] ?? ''}
            onChange={(e) => setParams((p) => withParam(p, 'source', e.target.value ? [e.target.value] : []))}
          >
            <option value="">{t('security.filters.anySource')}</option>
            {FINDING_SOURCES.map((s) => (
              <option key={s} value={s}>
                {t(`quality.enums.finding_source.${s}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="security-project" className="text-sm font-medium">
            {t('security.filters.project')}
          </label>
          <select
            id="security-project"
            className="select"
            value={filters.project}
            onChange={(e) => setParams((p) => withParam(withParam(p, 'run', ''), 'project', e.target.value))}
          >
            <option value="">{t('security.filters.anyProject')}</option>
            {(projects.data?.items ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
            {filters.project && !projects.data?.items.some((p) => p.id === filters.project) ? <option value={filters.project}>{filters.project}</option> : null}
          </select>
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="security-run" className="text-sm font-medium">
            {t('security.filters.run')}
          </label>
          <select id="security-run" className="select" value={filters.run} onChange={(e) => setParams((p) => withParam(p, 'run', e.target.value))}>
            <option value="">{t('security.filters.anyRun')}</option>
            {(runs.data?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.objective.length > 60 ? `${r.objective.slice(0, 60)}…` : r.objective}
              </option>
            ))}
            {filters.run && !runs.data?.items.some((r) => r.id === filters.run) ? <option value={filters.run}>{filters.run}</option> : null}
          </select>
        </div>
      </div>
      {projects.isError || runs.isError ? <p className="text-xs text-danger-text">{t('security.filters.optionsError')}</p> : null}
      <div className="flex flex-wrap items-center gap-2">
        {filters.cwe ? (
          <button type="button" className="btn-secondary btn-sm" onClick={() => setParams((p) => withParam(p, 'cwe', ''))} data-testid="chip-cwe">
            {t('security.filters.cweChip', { value: filters.cwe })}
            <Icon name="close" className="h-4 w-4" />
          </button>
        ) : null}
        {filters.control ? (
          <button type="button" className="btn-secondary btn-sm" onClick={() => setParams((p) => withParam(p, 'control', ''))} data-testid="chip-control">
            {t('security.filters.controlChip', { value: filters.control })}
            <Icon name="close" className="h-4 w-4" />
          </button>
        ) : null}
        {anyActive ? (
          <Button
            size="sm"
            variant="ghost"
            icon="refresh"
            onClick={() =>
              setParams((p) => {
                const next = new URLSearchParams(p);
                for (const k of ['severity', 'status', 'source', 'project', 'run', 'cwe', 'control', 'finding']) next.delete(k);
                return next;
              })
            }
          >
            {t('security.filters.reset')}
          </Button>
        ) : null}
        {filters.cwe || filters.control ? <span className="text-xs text-fg-muted">{t('security.filters.clientNote')}</span> : null}
      </div>
    </div>
  );
}

function FindingRow({ f, workspaceId, selected, onSelect }: { f: Finding; workspaceId: string; selected: boolean; onSelect: () => void }) {
  const { t } = useTranslation();
  const title = f.title ?? f.rule_id;
  return (
    <div className={cx('flex min-w-0 flex-col gap-1.5 py-1', selected && 'rounded-md ring-2 ring-border-active ring-offset-1 ring-offset-surface')} data-finding-id={f.id}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <SeverityBadge severity={f.severity} />
        <StatusBadge kind="finding" status={f.status} labelKey={`quality.enums.finding_status.${f.status}`} />
        <TextLink to={workspacePath(workspaceId, `quality/findings/${f.id}`)}>
          <span className="whitespace-pre-line break-words">{title}</span>
        </TextLink>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-muted">
        <span className="break-all font-mono">{f.rule_id}</span>
        {f.cwe_id ? <span className="font-mono">{f.cwe_id}</span> : null}
        {(f.control_refs ?? []).map((c) => (
          <span key={c} className="font-mono">
            {c}
          </span>
        ))}
        {f.source ? <span>{t(`quality.enums.finding_source.${f.source}`, { defaultValue: f.source })}</span> : null}
        {f.file_path ? <span className="break-all font-mono">{f.line ? `${f.file_path}:${f.line}` : f.file_path}</span> : null}
        {f.created_at ? <Timestamp value={f.created_at} /> : null}
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs">
        {f.evidence_id ? (
          <TextLink to={workspacePath(workspaceId, `quality/evidence/${f.evidence_id}`)} srSuffix={title.slice(0, 80)}>
            {t('quality.gate.openEvidence')}
          </TextLink>
        ) : (
          <span className="text-fg-muted">{t('quality.gate.noEvidence')}</span>
        )}
        {f.run_id ? (
          <TextLink to={workspacePath(workspaceId, `quality/runs/${f.run_id}?tab=findings`)} srSuffix={title.slice(0, 80)}>
            {t('security.list.openRun')}
          </TextLink>
        ) : null}
        <button type="button" className={cx('btn-secondary btn-sm', PRESSED)} aria-pressed={selected} onClick={onSelect}>
          {t('security.list.decide')}
          <span className="sr-only">: {title.slice(0, 80)}</span>
        </button>
      </div>
    </div>
  );
}

/** Выбранная находка: свежая версия (getFinding), профиль run (critical-ограничение), решения FSM. */
function FindingDetail({
  findingId,
  workspaceId,
  focusOnMount,
  onClose,
}: {
  findingId: string;
  workspaceId: string;
  focusOnMount: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const finding = useQuery({ queryKey: findingKeys.detail(findingId), queryFn: ({ signal }) => findingsApi.get(findingId, signal) });
  const runId = finding.data?.run_id;
  const run = useQuery({ queryKey: runKeys.detail(runId ?? ''), queryFn: ({ signal }) => runsApi.get(runId ?? '', signal), enabled: Boolean(runId) });
  // Фокус — в панель только после выбора пользователем (не при открытии ссылки с ?finding=).
  useEffect(() => {
    if (focusOnMount) headingRef.current?.focus();
  }, [findingId, focusOnMount]);
  const f = finding.data;
  return (
    <section aria-labelledby="security-detail-title" className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4 shadow-e1" data-testid="finding-detail">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h2 id="security-detail-title" ref={headingRef} tabIndex={-1} className="text-lg font-semibold">
          {t('security.detail.title')}
        </h2>
        <Button size="sm" variant="ghost" icon="close" onClick={onClose}>
          {t('security.detail.close')}
        </Button>
      </div>
      {finding.isPending ? (
        <LoadingState label={t('security.detail.loading')} />
      ) : finding.isError ? (
        <ErrorState error={finding.error} onRetry={() => void finding.refetch()} />
      ) : f ? (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <SeverityBadge severity={f.severity} />
            <span data-testid="detail-status">
              <StatusBadge kind="finding" status={f.status} labelKey={`quality.enums.finding_status.${f.status}`} />
            </span>
          </div>
          <p className="whitespace-pre-line break-words font-medium" data-testid="detail-title">
            {f.title ?? f.rule_id}
          </p>
          <FieldList>
            <Field label={t('quality.finding.rule')}>
              <span className="break-all font-mono text-xs">{f.rule_id}</span>
            </Field>
            <Field label={t('quality.finding.cwe')}>{f.cwe_id}</Field>
            <Field label={t('quality.finding.controls')}>{f.control_refs?.length ? f.control_refs.join(', ') : null}</Field>
            <Field label={t('quality.finding.location')}>
              {f.file_path ? <span className="break-all font-mono text-xs">{f.line ? `${f.file_path}:${f.line}` : f.file_path}</span> : null}
            </Field>
            <Field label={t('quality.finding.fixedIn')}>{f.fixed_in_commit_sha ? <Hash value={f.fixed_in_commit_sha} /> : null}</Field>
            <Field label={t('quality.finding.resolvedBy')}>{f.resolved_by?.display_name}</Field>
            <Field label={t('quality.finding.resolutionReason')}>
              {f.resolution_reason ? <span className="whitespace-pre-line">{f.resolution_reason}</span> : null}
            </Field>
          </FieldList>
          <TextLink to={workspacePath(workspaceId, `quality/findings/${f.id}`)}>{t('security.detail.openCard')}</TextLink>
          <h3 className="font-semibold">{t('quality.transition.title')}</h3>
          {run.isError ? <p className="text-sm text-fg-muted">{t('quality.transition.profileUnknown')}</p> : null}
          <FindingTransitionActions finding={f} profile={run.data?.quality_profile} idPrefix="security-detail" />
        </>
      ) : null}
    </section>
  );
}

export function SecurityCenterPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  const [params, setSearchParams] = useSearchParams();
  const filters = useMemo(() => parseFilters(params), [params]);
  const query = useMemo(() => filtersToQuery(filters), [filters]);
  const selected = params.get('finding') ?? '';
  const setParams = (update: (p: URLSearchParams) => URLSearchParams) => setSearchParams((p) => update(p), { replace: true });
  const [focusTarget, setFocusTarget] = useState<string | null>(null);
  const listHeadingRef = useRef<HTMLHeadingElement>(null);

  const findings = useWorkspaceFindings(workspaceId ?? '', query);
  const loaded = useMemo(() => findings.data?.pages.flatMap((p) => p.items) ?? [], [findings.data]);
  const visible = useMemo(() => applyClientFilters(loaded, filters), [loaded, filters]);
  const more = Boolean(findings.hasNextPage);
  const runIds = useMemo(() => distinctRunIds(loaded, RUN_REPORTS_LIMIT), [loaded]);
  const index = useRunGateIndex(runIds);
  const evidenceIds = useMemo(() => index.securityGates.flatMap((g) => (g.evidence_id ? [g.evidence_id] : [])), [index.securityGates]);
  const evidenceStatuses = useEvidenceStatuses(evidenceIds);
  const kinds = useMemo(() => {
    const dependency: Finding[] = [];
    const secrets: Finding[] = [];
    let unknown = 0;
    for (const f of loaded) {
      const k = securityKind(f, index.gateTypes, index.loadedRuns);
      if (k === 'dependency') dependency.push(f);
      else if (k === 'secrets') secrets.push(f);
      else if (k === 'unknown') unknown += 1;
    }
    return { dependency, secrets, unknown };
  }, [loaded, index]);
  const live = useWorkspaceInvalidation(workspaceId, LIVE_TYPES, [['findings'], ['quality']]);

  if (!workspaceId) return null;
  const sample = { items: loaded, more };
  const loadingSummary = findings.isPending;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('nav.security')}>
        <LiveStatus state={live.state} onReconnect={live.reconnect} />
      </PageHeading>
      <p className="mb-4 max-w-prose text-fg-muted">{t('security.description')}</p>
      <Filters workspaceId={workspaceId} filters={filters} setParams={setParams} />
      {findings.isError && loaded.length === 0 ? (
        <ErrorState error={findings.error} onRetry={() => void findings.refetch()} />
      ) : (
        <>
          <div className="mb-6 grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-4">
            <SeverityTile sample={sample} loading={loadingSummary} />
            <RemediationTile sample={sample} loading={loadingSummary} />
            <div className="md:col-span-2">
              <MappingsTile
                sample={sample}
                loading={loadingSummary}
                cwe={filters.cwe}
                control={filters.control}
                onCwe={(v) => setParams((p) => withParam(p, 'cwe', v))}
                onControl={(v) => setParams((p) => withParam(p, 'control', v))}
              />
            </div>
            <KindTile kind="dependency" items={kinds.dependency} unclassified={kinds.unknown} loading={loadingSummary || (index.pending && runIds.length > 0)} workspaceId={workspaceId} />
            <KindTile kind="secrets" items={kinds.secrets} unclassified={kinds.unknown} loading={loadingSummary || (index.pending && runIds.length > 0)} workspaceId={workspaceId} />
            <div className="md:col-span-2">
              <SecurityEvidenceTile index={index} statuses={evidenceStatuses} workspaceId={workspaceId} runCount={runIds.length} />
            </div>
          </div>
          <div className={cx('grid grid-cols-1 gap-4', selected && 'xl:grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)]')}>
            <section aria-labelledby="security-list-title" className="flex min-w-0 flex-col gap-3">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <h2 id="security-list-title" ref={listHeadingRef} tabIndex={-1} className="text-lg font-semibold">
                  {t('security.list.title')}
                </h2>
                <p className="text-sm text-fg-muted" data-testid="security-list-count" data-count={visible.length}>
                  {t(more ? 'security.list.countMore' : 'security.list.count', { count: visible.length, loaded: loaded.length })}
                </p>
              </div>
              {findings.isPending ? (
                <LoadingState label={t('security.list.loading')} />
              ) : visible.length === 0 ? (
                <EmptyState headingLevel={3} title={t('security.list.emptyTitle')} description={t('security.list.emptyDescription')} />
              ) : (
                <VirtualList
                  items={visible}
                  getKey={(f) => f.id}
                  label={t('security.list.label')}
                  estimateSize={112}
                  className="h-[36rem] max-h-[70vh]"
                  busy={findings.isFetchingNextPage}
                  renderItem={(f) => (
                    <FindingRow
                      f={f}
                      workspaceId={workspaceId}
                      selected={selected === f.id}
                      onSelect={() => {
                        setFocusTarget(selected === f.id ? null : f.id);
                        setParams((p) => withParam(p, 'finding', selected === f.id ? '' : f.id));
                      }}
                    />
                  )}
                />
              )}
              <LoadMore
                shown={loaded.length}
                hasNextPage={findings.hasNextPage}
                isFetchingNextPage={findings.isFetchingNextPage}
                nextPageError={findings.isFetchNextPageError ? findings.error : null}
                onLoadMore={() => void findings.fetchNextPage()}
              />
            </section>
            {selected ? (
              <div className="min-w-0 xl:sticky xl:top-4 xl:self-start">
                <FindingDetail
                  findingId={selected}
                  workspaceId={workspaceId}
                  focusOnMount={focusTarget === selected}
                  onClose={() => {
                    setParams((p) => withParam(p, 'finding', ''));
                    listHeadingRef.current?.focus();
                  }}
                />
              </div>
            ) : null}
          </div>
        </>
      )}
    </section>
  );
}
