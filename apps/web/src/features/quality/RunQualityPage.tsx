import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useSearchParams } from 'react-router';
import {
  FINDING_SEVERITIES,
  FINDING_STATUSES,
  findingKeys,
  findingsApi,
  type Finding,
  type FindingSeverity,
  type FindingStatus,
} from '../../api/findings';
import {
  artifactDownloadHref,
  gateCountsAsPass,
  GATE_NOT_PASS_LOOKALIKE,
  qualityApi,
  qualityKeys,
  type ArtifactRef,
  type EvidenceVerificationStatus,
  type GateExecution,
  type QualityReport,
} from '../../api/quality';
import { runKeys, runsApi, type Run } from '../../api/runs';
import { LoadMore } from '../../components/LoadMore';
import { PageHeading } from '../../components/PageHeading';
import { ErrorState, LoadingState } from '../../components/states';
import { RUN_TERMINAL, StatusBadge } from '../../components/StatusBadge';
import { Callout } from '../../components/ui/Badge';
import { Button } from '../../components/ui/Button';
import { Table, type Column } from '../../components/ui/Table';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../../components/ui/Tabs';
import { Timestamp } from '../../components/ui/Time';
import { problemMessage } from '../../lib/problem';
import { useCurrentWorkspace } from '../../workspace';
import { LiveStatus, useRunInvalidation } from './live';
import { Field, FieldList, GateStatusBadge, Hash, RecentRuns, runViewPath, SeverityBadge, TextLink, workspacePath } from './shared';

export const FINDINGS_PAGE_SIZE = 50;
export const EVIDENCE_PAGE_SIZE = 50;
const TABS = ['gates', 'findings', 'evidence', 'report'] as const;
type Tab = (typeof TABS)[number];

/** События, после которых сводка качества/находки run могли измениться. */
export const QUALITY_EVENT_TYPES = ['gate.completed', 'run.completed', 'run.state_changed'] as const;

function VerdictSummary({ report }: { report: QualityReport }) {
  const { t } = useTranslation();
  const required = report.gate_executions.filter((g) => g.required);
  const passed = required.filter((g) => gateCountsAsPass(g.status)).length;
  const lookalikes = required.filter((g) => GATE_NOT_PASS_LOOKALIKE.includes(g.status));
  const open = FINDING_SEVERITIES.map((s) => [s, report.findings_summary[s] ?? 0] as const).filter(([, n]) => n > 0);
  const byStatus = Object.entries(report.findings_by_status ?? {}).filter(([, n]) => n > 0);
  return (
    <div className="card mb-6 flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-lg font-semibold">{t('quality.verdict')}</h2>
        <span data-testid="quality-verdict">
          <GateStatusBadge status={report.verdict} />
        </span>
        <span className="text-sm text-fg-muted">{t(`quality.verdictHint.${report.verdict}`, { defaultValue: '' })}</span>
      </div>
      <FieldList>
        <Field label={t('quality.profile')}>{t(`enums.quality_profile.${report.quality_profile}`, { defaultValue: report.quality_profile })}</Field>
        <Field label={t('quality.requiredGates')} testId="required-gates">
          {t('quality.requiredGatesValue', { passed, total: required.length })}
        </Field>
        <Field label={t('quality.openFindings')} testId="open-findings">
          {open.length === 0
            ? t('quality.noOpenFindings')
            : open.map(([s, n]) => `${t(`quality.enums.severity.${s}`)}: ${n}`).join(', ')}
        </Field>
        <Field label={t('quality.findingsTotal')}>{String(report.findings_total)}</Field>
        {byStatus.length > 0 ? (
          <Field label={t('quality.findingsByStatus')}>
            {byStatus.map(([s, n]) => `${t(`quality.enums.finding_status.${s}`, { defaultValue: s })}: ${n}`).join(', ')}
          </Field>
        ) : null}
      </FieldList>
      {lookalikes.length > 0 ? (
        <Callout tone="warning" title={t('quality.notPassTitle')}>
          <p data-testid="not-pass-warning">
            {t('quality.notPassDescription', { gates: lookalikes.map((g) => `${g.gate_type} (${t(`quality.enums.gate_status.${g.status}`)})`).join(', ') })}
          </p>
        </Callout>
      ) : null}
    </div>
  );
}

function GatesTable({ gates, workspaceId }: { gates: GateExecution[]; workspaceId: string }) {
  const { t } = useTranslation();
  const columns: Column<GateExecution>[] = [
    { id: 'gate', header: t('quality.gate.type'), cell: (g) => <span className="font-mono text-xs">{g.gate_type}</span>, sortValue: (g) => g.gate_type, rowHeader: true },
    { id: 'required', header: t('quality.gate.required'), cell: (g) => (g.required ? t('quality.gate.requiredYes') : t('quality.gate.requiredNo')), sortValue: (g) => (g.required ? 0 : 1) },
    { id: 'status', header: t('quality.gate.status'), cell: (g) => <GateStatusBadge status={g.status} />, sortValue: (g) => g.status },
    {
      id: 'counts',
      header: t('quality.gate.countsAsPass'),
      cell: (g) =>
        gateCountsAsPass(g.status) ? (
          t('quality.gate.countsYes')
        ) : (
          <span className={GATE_NOT_PASS_LOOKALIKE.includes(g.status) ? 'font-semibold text-danger-text' : undefined}>
            {t(`quality.gate.countsNo.${g.status}`, { defaultValue: t('quality.gate.countsNoDefault') })}
          </span>
        ),
    },
    {
      id: 'reason',
      header: t('quality.gate.reason'),
      cell: (g) => (
        <span className="break-words">
          {[g.error_code, g.not_applicable_reason].filter(Boolean).join(' · ') || '—'}
        </span>
      ),
    },
    { id: 'commit', header: t('quality.gate.commit'), cell: (g) => (g.commit_sha ? <Hash value={g.commit_sha} /> : '—') },
    { id: 'finished', header: t('quality.gate.finished'), cell: (g) => (g.finished_at ? <Timestamp value={g.finished_at} /> : '—'), sortValue: (g) => g.finished_at ?? null },
    {
      id: 'evidence',
      header: t('quality.gate.evidence'),
      cell: (g) =>
        g.evidence_id ? (
          <TextLink to={workspacePath(workspaceId, `quality/evidence/${g.evidence_id}`)} srSuffix={g.gate_type}>
            {t('quality.gate.openEvidence')}
          </TextLink>
        ) : (
          <span className="text-fg-muted">{t('quality.gate.noEvidence')}</span>
        ),
    },
  ];
  return (
    <Table
      caption={t('quality.gate.caption')}
      columns={columns}
      rows={gates}
      rowKey={(g) => g.id}
      rowProps={(g) => ({ 'data-gate-status': g.status })}
      empty={t('quality.gate.empty')}
      minWidthClass="min-w-[56rem]"
    />
  );
}

function FindingsPanel({ runId, workspaceId }: { runId: string; workspaceId: string }) {
  const { t } = useTranslation();
  const [severity, setSeverity] = useState<FindingSeverity | ''>('');
  const [status, setStatus] = useState<FindingStatus | ''>('');
  const query = { ...(severity ? { severity: [severity] } : {}), ...(status ? { status: [status] } : {}) };
  const findings = useInfiniteQuery({
    queryKey: findingKeys.list(runId, query),
    queryFn: ({ pageParam, signal }) =>
      findingsApi.list(runId, { ...query, limit: FINDINGS_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = findings.data?.pages.flatMap((p) => p.items) ?? [];
  const columns: Column<Finding>[] = [
    { id: 'severity', header: t('quality.finding.severity'), cell: (f) => <SeverityBadge value={f.severity} /> },
    {
      id: 'title',
      header: t('quality.finding.title'),
      rowHeader: true,
      cell: (f) => (
        <TextLink to={workspacePath(workspaceId, `quality/findings/${f.id}`)}>
          <span className="break-words">{f.title ?? f.rule_id}</span>
        </TextLink>
      ),
    },
    { id: 'rule', header: t('quality.finding.rule'), cell: (f) => <span className="break-all font-mono text-xs">{f.rule_id}</span> },
    {
      id: 'status',
      header: t('quality.finding.status'),
      cell: (f) => <StatusBadge kind="finding" status={f.status} labelKey={`quality.enums.finding_status.${f.status}`} />,
    },
    {
      id: 'location',
      header: t('quality.finding.location'),
      cell: (f) => (f.file_path ? <span className="break-all font-mono text-xs">{f.line ? `${f.file_path}:${f.line}` : f.file_path}</span> : '—'),
    },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div role="group" aria-label={t('quality.finding.filters')} className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor="finding-severity" className="text-sm font-medium">
            {t('quality.finding.severity')}
          </label>
          <select id="finding-severity" className="input" value={severity} onChange={(e) => setSeverity(e.target.value as FindingSeverity | '')}>
            <option value="">{t('quality.finding.any')}</option>
            {FINDING_SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {t(`quality.enums.severity.${s}`)}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="finding-status" className="text-sm font-medium">
            {t('quality.finding.status')}
          </label>
          <select id="finding-status" className="input" value={status} onChange={(e) => setStatus(e.target.value as FindingStatus | '')}>
            <option value="">{t('quality.finding.any')}</option>
            {FINDING_STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`quality.enums.finding_status.${s}`)}
              </option>
            ))}
          </select>
        </div>
      </div>
      {findings.isPending ? (
        <LoadingState label={t('quality.finding.loading')} />
      ) : findings.isError && items.length === 0 ? (
        <ErrorState error={findings.error} onRetry={() => void findings.refetch()} />
      ) : (
        <>
          <Table
            caption={t('quality.finding.caption')}
            columns={columns}
            rows={items}
            rowKey={(f) => f.id}
            rowProps={(f) => ({ 'data-finding-id': f.id })}
            empty={t('quality.finding.empty')}
          />
          <LoadMore
            shown={items.length}
            hasNextPage={findings.hasNextPage}
            isFetchingNextPage={findings.isFetchingNextPage}
            nextPageError={findings.isFetchNextPageError ? findings.error : null}
            onLoadMore={() => void findings.fetchNextPage()}
          />
        </>
      )}
    </div>
  );
}

const VERIFICATION_STATUSES: readonly EvidenceVerificationStatus[] = ['VERIFIED', 'PENDING', 'UNVERIFIED', 'REJECTED'];

function EvidencePanel({ runId, workspaceId, gates }: { runId: string; workspaceId: string; gates: GateExecution[] }) {
  const { t } = useTranslation();
  const tasks = useQuery({ queryKey: runKeys.tasks(runId), queryFn: ({ signal }) => runsApi.tasks(runId, { limit: 200 }, signal) });
  const [taskId, setTaskId] = useState('');
  const [verification, setVerification] = useState<EvidenceVerificationStatus | ''>('');
  const query = verification ? { verification_status: verification } : {};
  const evidence = useInfiniteQuery({
    queryKey: qualityKeys.taskEvidence(taskId, query),
    queryFn: ({ pageParam, signal }) =>
      qualityApi.taskEvidence(taskId, { ...query, limit: EVIDENCE_PAGE_SIZE, ...(pageParam ? { cursor: pageParam } : {}) }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: Boolean(taskId),
  });
  const items = evidence.data?.pages.flatMap((p) => p.items) ?? [];
  const gateEvidence = gates.filter((g) => g.evidence_id);

  return (
    <div className="flex flex-col gap-6">
      <section aria-labelledby="gate-evidence-heading" className="flex flex-col gap-2">
        <h3 id="gate-evidence-heading" className="font-semibold">
          {t('quality.evidence.gateTitle')}
        </h3>
        {gateEvidence.length === 0 ? (
          <p className="text-sm text-fg-muted">{t('quality.evidence.gateEmpty')}</p>
        ) : (
          <ul className="flex flex-col gap-1 text-sm" aria-label={t('quality.evidence.gateTitle')}>
            {gateEvidence.map((g) => (
              <li key={g.id} className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-xs">{g.gate_type}</span>
                <GateStatusBadge status={g.status} />
                <TextLink to={workspacePath(workspaceId, `quality/evidence/${g.evidence_id}`)} srSuffix={g.gate_type}>
                  {t('quality.gate.openEvidence')}
                </TextLink>
              </li>
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="task-evidence-heading" className="flex flex-col gap-3">
        <h3 id="task-evidence-heading" className="font-semibold">
          {t('quality.evidence.taskTitle')}
        </h3>
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1">
            <label htmlFor="evidence-task" className="text-sm font-medium">
              {t('quality.evidence.task')}
            </label>
            <select id="evidence-task" className="input" value={taskId} onChange={(e) => setTaskId(e.target.value)} disabled={!tasks.data}>
              <option value="">{t('quality.evidence.chooseTask')}</option>
              {(tasks.data?.tasks ?? []).map((task) => (
                <option key={task.id} value={task.id}>
                  {task.title}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label htmlFor="evidence-verification" className="text-sm font-medium">
              {t('quality.evidence.verification')}
            </label>
            <select
              id="evidence-verification"
              className="input"
              value={verification}
              onChange={(e) => setVerification(e.target.value as EvidenceVerificationStatus | '')}
            >
              <option value="">{t('quality.finding.any')}</option>
              {VERIFICATION_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {t(`quality.enums.verification.${s}`)}
                </option>
              ))}
            </select>
          </div>
        </div>
        {tasks.isError ? <ErrorState error={tasks.error} onRetry={() => void tasks.refetch()} /> : null}
        {!taskId ? (
          <p className="text-sm text-fg-muted">{t('quality.evidence.chooseTaskHint')}</p>
        ) : evidence.isPending ? (
          <LoadingState label={t('quality.evidence.loading')} />
        ) : evidence.isError && items.length === 0 ? (
          <ErrorState error={evidence.error} onRetry={() => void evidence.refetch()} />
        ) : (
          <>
            <Table
              caption={t('quality.evidence.caption')}
              columns={[
                {
                  id: 'kind',
                  header: t('quality.evidence.kind'),
                  rowHeader: true,
                  cell: (e) => (
                    <TextLink to={workspacePath(workspaceId, `quality/evidence/${e.id}`)}>
                      <span className="font-mono text-xs">{e.kind}</span>
                    </TextLink>
                  ),
                },
                { id: 'producer', header: t('quality.evidence.producer'), cell: (e) => <span className="break-all">{e.producer}</span> },
                {
                  id: 'verification',
                  header: t('quality.evidence.verification'),
                  cell: (e) => (
                    <StatusBadge kind="verification" status={e.verification_status} labelKey={`quality.enums.verification.${e.verification_status}`} />
                  ),
                },
                { id: 'exit', header: t('quality.evidence.exitCode'), cell: (e) => (e.exit_code ?? '—').toString() },
                { id: 'commit', header: t('quality.evidence.commit'), cell: (e) => (e.commit_sha ? <Hash value={e.commit_sha} /> : '—') },
                { id: 'sha', header: t('quality.evidence.sha256'), cell: (e) => <Hash value={e.artifact.sha256} /> },
              ]}
              rows={items}
              rowKey={(e) => e.id}
              empty={t('quality.evidence.empty')}
            />
            <LoadMore
              shown={items.length}
              hasNextPage={evidence.hasNextPage}
              isFetchingNextPage={evidence.isFetchingNextPage}
              nextPageError={evidence.isFetchNextPageError ? evidence.error : null}
              onLoadMore={() => void evidence.fetchNextPage()}
            />
          </>
        )}
      </section>
    </div>
  );
}

/** Сохранение JSON в файл через object URL (без внешних зависимостей). */
function saveJson(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function ReportPanel({ runId }: { runId: string }) {
  const { t } = useTranslation();
  const [state, setState] = useState<{ loading: boolean; error: unknown; artifact: ArtifactRef | null; generatedAt: string | null; disclaimer: string | null }>(
    { loading: false, error: null, artifact: null, generatedAt: null, disclaimer: null },
  );
  const download = async () => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const report = await qualityApi.runReport(runId);
      saveJson(report, `run-${runId}-report.json`);
      setState({
        loading: false,
        error: null,
        artifact: report.report_artifact ?? null,
        generatedAt: report.generated_at,
        disclaimer: report.disclaimer ?? null,
      });
    } catch (error) {
      setState((s) => ({ ...s, loading: false, error }));
    }
  };
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-fg-muted">{t('quality.report.description')}</p>
      <div>
        <Button variant="primary" loading={state.loading} onClick={() => void download()}>
          {t('quality.report.download')}
        </Button>
      </div>
      <p role="status" className="text-sm text-fg-muted">
        {state.generatedAt ? (
          <>
            {t('quality.report.ready')} <Timestamp value={state.generatedAt} />
          </>
        ) : null}
      </p>
      {state.error ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {problemMessage(t, state.error)}
        </p>
      ) : null}
      {state.disclaimer ? <Callout tone="info">{state.disclaimer}</Callout> : null}
      {state.artifact ? (
        <FieldList label={t('quality.report.artifact')}>
          <Field label={t('quality.report.artifact')}>
            <a className="font-medium text-link underline" href={artifactDownloadHref(state.artifact.id)}>
              {t('quality.report.artifactDownload')}
            </a>
          </Field>
          <Field label={t('quality.evidence.sha256')}>
            <code className="break-all font-mono text-xs">{state.artifact.sha256}</code>
          </Field>
        </FieldList>
      ) : null}
    </div>
  );
}

function RunContext({ run, workspaceId }: { run: Run | undefined; workspaceId: string }) {
  const { t } = useTranslation();
  if (!run) return null;
  return (
    <div className="mb-4 flex flex-col gap-2">
      <p className="whitespace-pre-line break-words text-fg-muted">{run.objective}</p>
      <div className="flex flex-wrap items-center gap-4 text-sm">
        <StatusBadge kind="run" status={run.status} />
        <TextLink to={runViewPath(workspaceId, run.project_id, run.id)}>{t('quality.openRun')}</TextLink>
        <TextLink to={workspacePath(workspaceId, `compliance/runs/${run.id}`)}>{t('quality.openCompliance')}</TextLink>
      </div>
    </div>
  );
}

export function RunQualityPage() {
  const { t } = useTranslation();
  const { runId = '' } = useParams();
  const { workspaceId } = useCurrentWorkspace();
  const [params, setParams] = useSearchParams();
  const tabParam = params.get('tab');
  const tab: Tab = TABS.includes(tabParam as Tab) ? (tabParam as Tab) : 'gates';

  const run = useQuery({ queryKey: runKeys.detail(runId), queryFn: ({ signal }) => runsApi.get(runId, signal) });
  const quality = useQuery({ queryKey: qualityKeys.run(runId), queryFn: ({ signal }) => qualityApi.runQuality(runId, signal) });
  const runRef = useRef(run.data);
  useEffect(() => {
    runRef.current = run.data;
  });
  const keys = useMemo(() => [qualityKeys.run(runId), findingKeys.all(runId), runKeys.detail(runId)], [runId]);
  // Завершённому run поток не нужен (сервер закрывает его после run.completed).
  const liveRunId = run.data && !RUN_TERMINAL.includes(run.data.status) ? runId : null;
  const live = useRunInvalidation(liveRunId, QUALITY_EVENT_TYPES, keys, () => RUN_TERMINAL.includes(runRef.current?.status ?? ''));

  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('quality.runTitle')}>
        <LiveStatus state={live.state} onReconnect={live.reconnect} />
      </PageHeading>
      <RunContext run={run.data} workspaceId={workspaceId} />
      {quality.isPending ? (
        <LoadingState label={t('quality.loading')} />
      ) : quality.isError ? (
        <ErrorState error={quality.error} onRetry={() => void quality.refetch()} />
      ) : (
        <>
          <VerdictSummary report={quality.data} />
          <Tabs
            value={tab}
            onValueChange={(v) => {
              const next = new URLSearchParams(params);
              next.set('tab', v);
              setParams(next, { replace: true });
            }}
          >
            <TabsList label={t('quality.tabs.label')}>
              {TABS.map((v) => (
                <TabsTrigger key={v} value={v}>
                  {t(`quality.tabs.${v}`)}
                </TabsTrigger>
              ))}
            </TabsList>
            <TabsContent value="gates">
              <GatesTable gates={quality.data.gate_executions} workspaceId={workspaceId} />
            </TabsContent>
            <TabsContent value="findings">
              <FindingsPanel runId={runId} workspaceId={workspaceId} />
            </TabsContent>
            <TabsContent value="evidence">
              <EvidencePanel runId={runId} workspaceId={workspaceId} gates={quality.data.gate_executions} />
            </TabsContent>
            <TabsContent value="report">
              <ReportPanel runId={runId} />
            </TabsContent>
          </Tabs>
        </>
      )}
    </section>
  );
}

export function QualityIndexPage() {
  const { t } = useTranslation();
  const { workspaceId } = useCurrentWorkspace();
  if (!workspaceId) return null;
  return (
    <section aria-labelledby="page-title">
      <PageHeading title={t('quality.title')} />
      <p className="mb-4 text-fg-muted">{t('quality.indexDescription')}</p>
      <h2 className="mb-3 text-lg font-semibold">{t('quality.runs.title')}</h2>
      <RecentRunsForQuality workspaceId={workspaceId} />
    </section>
  );
}

function RecentRunsForQuality({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  return (
    <RecentRuns
      workspaceId={workspaceId}
      label={t('quality.runs.listLabel')}
      actions={(r) => (
        <>
          <TextLink to={workspacePath(workspaceId, `quality/runs/${r.id}`)} srSuffix={r.objective.slice(0, 80)}>
            {t('quality.runs.openQuality')}
          </TextLink>
          <TextLink to={workspacePath(workspaceId, `compliance/runs/${r.id}`)} srSuffix={r.objective.slice(0, 80)}>
            {t('quality.openCompliance')}
          </TextLink>
        </>
      )}
    />
  );
}
