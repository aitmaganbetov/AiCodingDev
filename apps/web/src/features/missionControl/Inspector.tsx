/**
 * Agent Inspector (F4, план §3.3): без выбранной задачи — сводка run (статус, этапы, ожидающие
 * одобрения, команды pause/resume/cancel, переходы к качеству/соответствию/одобрениям, PR завершённого
 * run); с выбранной — роль и CLI-провайдер, текущая задача, статус CLI последней попытки, время,
 * измеренные метрики (попытки, циклы ревью, проверки в 5 состояниях D-8), артефакты, ошибки, последние
 * события задачи, состав контекста попытки (Context Pack, B3) и переходы в Task Detail / diff.
 * Только данные API: чего нет в ответе — не показывается или подписано «нет данных».
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { IdempotentIntent, isApiError } from '../../api/client';
import { runKeys, runsApi, type Run, type RunCommand, type TaskSummary } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { artifactDownloadPath, taskKeys, tasksApi, type Evidence, type GateExecution, type TaskAttempt } from '../../api/tasks';
import { RUN_PAUSABLE, RUN_TERMINAL, StatusBadge } from '../../components/StatusBadge';
import { AgentBadge, Badge, Callout, ErrorState, LoadingState, RelativeTime, Skeleton, Timestamp, VerificationBadge } from '../../components/ui';
import { can } from '../../lib/permissions';
import { problemMessage } from '../../lib/problem';
import { gateVerificationState } from '../../lib/verification';
import { PullRequestsPanel } from '../integrations/PullRequestsPanel';
import { describeEvent } from '../runs/describeEvent';
import { enumLabel, providerLabel, TASK_ACTIVE } from '../runs/labels';
import { durationParts, elapsedMs } from './feedModel';
import { useNow } from './useNow';

/* ───────────────────────────── Run ───────────────────────────── */

/** Команды run (pause/resume/cancel, 202 → переход приходит событием run.state_changed). */
export function RunControls({ run }: { run: Run }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const intent = useRef(new IdempotentIntent());
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [accepted, setAccepted] = useState<RunCommand | null>(null);
  const command = useMutation({
    mutationFn: (cmd: RunCommand) => runsApi.command(run.id, cmd, intent.current.keyFor({ run: run.id, cmd, version: run.version }), run.version),
    onSuccess: (_res, cmd) => {
      intent.current.complete();
      setAccepted(cmd);
      setConfirmCancel(false);
    },
    // 409/412: состояние ушло вперёд — перечитать.
    onSettled: () => void queryClient.invalidateQueries({ queryKey: runKeys.detail(run.id) }),
  });
  const terminal = RUN_TERMINAL.includes(run.status);
  const busy = command.isPending;
  return (
    <div role="group" aria-label={t('run.controls')} className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-2">
        {run.status === 'PAUSED' ? (
          <button type="button" className="btn-primary btn-sm" disabled={busy} onClick={() => command.mutate('resume')}>
            {t('run.resume')}
          </button>
        ) : (
          <button type="button" className="btn-secondary btn-sm" disabled={busy || !RUN_PAUSABLE.includes(run.status)} onClick={() => command.mutate('pause')}>
            {t('run.pause')}
          </button>
        )}
        {!confirmCancel ? (
          <button type="button" className="btn-secondary btn-sm" disabled={busy || terminal} onClick={() => setConfirmCancel(true)}>
            {t('run.cancel')}
          </button>
        ) : null}
      </div>
      {confirmCancel && !terminal ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning-border bg-warning-soft p-3" role="alert">
          <p className="text-sm text-warning-fg">{t('run.cancelConfirm')}</p>
          <button type="button" className="btn-primary btn-sm" disabled={busy} onClick={() => command.mutate('cancel')}>
            {t('run.cancelYes')}
          </button>
          <button type="button" className="btn-secondary btn-sm" onClick={() => setConfirmCancel(false)}>
            {t('run.cancelNo')}
          </button>
        </div>
      ) : null}
      <p role="status" className="text-sm text-fg-muted">
        {busy ? t('run.commandSending') : accepted && !command.isError ? t(`run.commandAccepted.${accepted}`) : ''}
      </p>
      {command.isError ? (
        <p role="alert" className="text-sm font-medium text-danger-text">
          {problemMessage(t, command.error)}
        </p>
      ) : null}
    </div>
  );
}

export function RunInspector({ workspaceId, role, run }: { workspaceId: string; role: string; run: Run }) {
  const { t, i18n } = useTranslation();
  const ws = `/w/${workspaceId}`;
  return (
    <div className="flex flex-col gap-4 text-sm" data-testid="mc-run-inspector">
      <p className="break-words font-medium text-fg">{run.objective}</p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-fg-muted">
        <dt>{t('run.mode')}</dt>
        <dd className="text-fg">{t(`enums.run_mode.${run.mode}`, { defaultValue: run.mode })}</dd>
        {run.blocked_reason ? (
          <>
            <dt>{t('run.blockedReason')}</dt>
            <dd className="text-fg" data-testid="run-blocked-reason">
              {enumLabel(t, 'blocked_reason', run.blocked_reason)}
            </dd>
          </>
        ) : null}
        <dt>{t('run.pendingApprovals')}</dt>
        <dd className="text-fg" data-testid="run-pending-approvals">
          {run.pending_approvals ?? 0}
        </dd>
        <dt>{t('run.requestedBy')}</dt>
        <dd className="break-words text-fg">{run.requested_by.display_name}</dd>
        <dt>{t('run.createdAt')}</dt>
        <dd className="text-fg">{new Intl.DateTimeFormat(i18n.language, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(run.created_at))}</dd>
        {run.started_at ? (
          <>
            <dt>{t('missionControl.started')}</dt>
            <dd className="text-fg">
              <RelativeTime value={run.started_at} />
            </dd>
          </>
        ) : null}
      </dl>
      {can(role, 'run:control') ? <RunControls run={run} /> : null}
      <nav aria-label={t('runs.nav.label')}>
        <ul className="flex flex-col gap-1">
          {can(role, 'quality:read') ? (
            <li>
              <Link className="link" to={`${ws}/quality/runs/${run.id}`}>
                {t('runs.nav.quality')}
              </Link>
            </li>
          ) : null}
          {can(role, 'quality:read') ? (
            <li>
              <Link className="link" to={`${ws}/compliance/runs/${run.id}`}>
                {t('runs.nav.compliance')}
              </Link>
            </li>
          ) : null}
          {can(role, 'approval:read') ? (
            <li>
              <Link className="link" to={`${ws}/approvals?run_id=${encodeURIComponent(run.id)}`}>
                {t('runs.nav.approvals')}
              </Link>
            </li>
          ) : null}
          <li>
            <Link className="link" to={`${ws}/projects/${run.project_id}/runs`}>
              {t('runs.backToRuns')}
            </Link>
          </li>
        </ul>
      </nav>
      {/* PR создаются только из завершённого run (P8-04). */}
      {run.status === 'COMPLETED' ? <PullRequestsPanel workspaceId={workspaceId} projectId={run.project_id} role={role} run={{ id: run.id, status: run.status }} /> : null}
    </div>
  );
}

/* ───────────────────────────── Задача ───────────────────────────── */

function Duration({ ms }: { ms: number }) {
  const { t } = useTranslation();
  const { h, m, s } = durationParts(ms);
  return <>{h > 0 ? t('missionControl.duration.hm', { h, m }) : m > 0 ? t('missionControl.duration.ms', { m, s }) : t('missionControl.duration.s', { s })}</>;
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-1.5">
      <h3 id={id} className="text-sm font-semibold text-fg">
        {title}
      </h3>
      {children}
    </section>
  );
}

function ContextPackSection({ taskId, attempt }: { taskId: string; attempt: TaskAttempt | undefined }) {
  const { t } = useTranslation();
  const pack = useQuery({
    queryKey: taskKeys.contextPack(taskId, attempt?.id ?? ''),
    queryFn: ({ signal }) => tasksApi.contextPack(taskId, attempt?.id ?? '', signal),
    enabled: Boolean(attempt),
    retry: (count, error) => !(isApiError(error) && error.status === 404) && count < 2,
  });
  let body;
  if (!attempt) body = <p className="text-fg-muted">{t('missionControl.inspector.noAttempts')}</p>;
  else if (pack.isPending) body = <Skeleton lines={2} />;
  else if (pack.isError)
    body =
      isApiError(pack.error) && pack.error.status === 404 ? (
        <p className="text-fg-muted" data-testid="context-pack-none">
          {t('missionControl.inspector.contextPack.none')}
        </p>
      ) : (
        <ErrorState error={pack.error} onRetry={() => void pack.refetch()} />
      );
  else {
    const p = pack.data;
    const stale = p.snapshot_commit !== null && p.snapshot_commit !== p.base_commit;
    body = (
      <div className="flex flex-col gap-2" data-testid="context-pack">
        <p className="text-xs text-fg-muted">
          {t('missionControl.inspector.contextPack.summary', { fragments: p.fragments_total, omitted: p.omitted_total, kb: Math.round(p.size_bytes / 1024) })}{' '}
          <span className="font-mono">{p.base_commit.slice(0, 12)}</span>
        </p>
        {stale ? <Callout tone="warning">{t('missionControl.inspector.contextPack.stale')}</Callout> : null}
        {p.fragments.length > 0 ? (
          <ul className="flex max-h-56 flex-col gap-0.5 overflow-y-auto rounded-md border border-border bg-canvas-secondary p-2 text-xs" aria-label={t('missionControl.inspector.contextPack.files')}>
            {p.fragments.map((f, i) => (
              <li key={`${f.path}-${i}`} className="flex flex-wrap items-baseline gap-x-2" data-section={f.section}>
                <span className="break-all font-mono text-fg">{f.path}</span>
                {f.lines && f.lines.length === 2 ? <span className="text-fg-muted">{t('missionControl.inspector.contextPack.lines', { from: f.lines[0], to: f.lines[1] })}</span> : null}
                <span className="text-fg-muted">{t(`missionControl.inspector.contextPack.section.${f.section}`, { defaultValue: f.section })}</span>
                {f.stale ? <Badge tone="warning">{t('missionControl.inspector.contextPack.staleFragment')}</Badge> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-fg-muted">{t('missionControl.inspector.contextPack.empty')}</p>
        )}
        {p.fragments_total > p.fragments.length ? (
          <p className="text-xs text-fg-muted">{t('missionControl.inspector.contextPack.more', { shown: p.fragments.length, total: p.fragments_total })}</p>
        ) : null}
        {p.omitted.length > 0 ? (
          <details className="text-xs">
            <summary className="min-h-target cursor-pointer">{t('missionControl.inspector.contextPack.omitted', { count: p.omitted_total })}</summary>
            <ul className="mt-1 flex flex-col gap-0.5">
              {p.omitted.map((o, i) => (
                <li key={`${o.path}-${i}`} className="break-all">
                  <span className="font-mono">{o.path}</span> — {t(`missionControl.inspector.contextPack.reason.${o.reason}`, { defaultValue: o.reason })}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
        {p.warnings.length > 0 ? <p className="text-xs text-fg-muted">{t('missionControl.inspector.contextPack.warnings', { codes: p.warnings.join(', ') })}</p> : null}
      </div>
    );
  }
  return (
    <Section id="mc-insp-context" title={t('missionControl.inspector.contextPack.title')}>
      {body}
    </Section>
  );
}

export function TaskInspector({
  role,
  summary,
  gates,
  events,
  taskHref,
  onClose,
}: {
  role: string;
  summary: TaskSummary | undefined;
  gates: readonly GateExecution[] | null;
  events: readonly RunEvent[];
  taskHref: (taskId: string, tab?: string) => string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const taskId = summary?.id ?? '';
  const task = useQuery({ queryKey: taskKeys.detail(taskId), queryFn: ({ signal }) => tasksApi.get(taskId, signal), enabled: Boolean(taskId) });
  const attempts = useQuery({
    queryKey: [...taskKeys.attempts(taskId), 'latest'] as const,
    queryFn: ({ signal }) => tasksApi.attempts(taskId, { limit: 20 }, signal),
    enabled: Boolean(taskId),
  });
  const canEvidence = can(role, 'evidence:read');
  const evidence = useQuery({
    queryKey: [...taskKeys.evidence(taskId), 'inspector'] as const,
    queryFn: ({ signal }) => tasksApi.evidence(taskId, { limit: 100 }, signal),
    enabled: Boolean(taskId) && canEvidence,
  });
  const latest = useMemo(() => [...(attempts.data?.items ?? [])].sort((a, b) => b.attempt_no - a.attempt_no)[0], [attempts.data]);
  const data = task.data ?? summary;
  const running = data ? TASK_ACTIVE.includes(data.status) : false;
  const now = useNow(running);
  if (!summary) return <p className="text-sm text-fg-muted">{t('missionControl.inspector.unknownTask')}</p>;
  if (!data) return <LoadingState label={t('tasks.loading')} />;

  const execution = latest?.executions?.at(-1);
  const provider = providerLabel(t, data.current_provider ?? latest?.provider);
  const elapsed = elapsedMs(latest?.started_at ?? null, latest?.finished_at ?? null, now);
  const mine = gates ? gates.filter((g) => g.task_id === taskId) : null;
  const evidenceById = new Map<string, Evidence>((evidence.data?.items ?? []).map((e) => [e.id, e]));
  const recent = events.filter((e) => e.payload.task_id === taskId).slice(-8).reverse();
  const errors = [
    data.blocked_reason ? enumLabel(t, 'blocked_reason', data.blocked_reason) : null,
    latest?.failure_category ? t(`tasks.enums.failure.${latest.failure_category}`, { defaultValue: latest.failure_category }) : null,
    ...(mine ?? []).filter((g) => g.status === 'FAIL' || g.status === 'ERROR').map((g) => `${g.gate_type}: ${g.error_code ?? g.status}`),
  ].filter((x): x is string => Boolean(x));
  const artifacts = (attempts.data?.items ?? []).flatMap((a) =>
    (a.executions ?? []).filter((x) => x.output_artifact_id).map((x) => ({ attempt: a.attempt_no, id: x.output_artifact_id as string })),
  );

  return (
    <div className="flex flex-col gap-4 text-sm" data-testid="mc-task-inspector" data-task-id={taskId}>
      <div className="flex flex-col gap-2">
        <AgentBadge agentRole={data.agent_role} provider={provider || undefined} size="md" />
        <p className="break-words text-base font-semibold text-fg" data-testid="inspector-task-title">
          {data.title}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge kind="task" status={data.status} />
          <button type="button" className="btn-ghost btn-sm" onClick={onClose}>
            {t('missionControl.inspector.toRun')}
          </button>
        </div>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-fg-muted">
        <dt>{t('missionControl.inspector.cliStatus')}</dt>
        <dd className="text-fg" data-testid="inspector-cli-status">
          {execution ? (
            <>
              {t(`tasks.enums.execution.${execution.status}`, { defaultValue: execution.status })}
              {execution.exit_code !== null && execution.exit_code !== undefined ? ` · ${t('tasks.attempts.exitCode', { code: execution.exit_code })}` : ''}
            </>
          ) : attempts.isPending ? (
            '…'
          ) : (
            t('missionControl.inspector.noExecution')
          )}
        </dd>
        <dt>{t('missionControl.inspector.elapsed')}</dt>
        <dd className="text-fg" data-testid="inspector-elapsed">
          {elapsed !== null ? <Duration ms={elapsed} /> : t('missionControl.inspector.notStarted')}
        </dd>
        <dt>{t('run.col.attempts')}</dt>
        <dd className="text-fg">{t('run.attempts', { count: data.attempt_count, max: data.max_attempts })}</dd>
        {task.data?.review_loop_count !== undefined ? (
          <>
            <dt>{t('tasks.reviewLoops')}</dt>
            <dd className="text-fg">{task.data.review_loop_count}</dd>
          </>
        ) : null}
        {latest?.routing_reason ? (
          <>
            <dt>{t('tasks.attempts.routing')}</dt>
            <dd className="break-words text-fg">{latest.routing_reason}</dd>
          </>
        ) : null}
        <dt>{t('tasks.updatedAt')}</dt>
        <dd className="text-fg">
          <Timestamp value={data.updated_at} />
        </dd>
      </dl>
      {attempts.isError ? <ErrorState error={attempts.error} onRetry={() => void attempts.refetch()} /> : null}

      <Section id="mc-insp-gates" title={t('missionControl.inspector.gates')}>
        {mine === null ? (
          <p className="text-fg-muted">{can(role, 'quality:read') ? t('missionControl.inspector.gatesLoading') : t('state.forbiddenDescription')}</p>
        ) : mine.length === 0 ? (
          <p className="text-fg-muted">{t('tasks.gates.empty')}</p>
        ) : (
          <ul className="flex flex-col gap-1" data-testid="inspector-gates">
            {mine.map((g) => {
              const ev = g.evidence_id ? evidenceById.get(g.evidence_id) : undefined;
              const result = gateVerificationState(g, ev ? ev.verification_status : null, { blockedUpstream: data.status === 'BLOCKED' });
              return (
                <li key={g.id} className="flex flex-wrap items-center gap-2" data-gate-id={g.id}>
                  <span className="font-mono text-xs">{g.gate_type}</span>
                  <VerificationBadge result={result} />
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      {errors.length > 0 ? (
        <Section id="mc-insp-errors" title={t('missionControl.inspector.errors')}>
          <ul className="flex flex-col gap-1" data-testid="inspector-errors">
            {errors.map((e) => (
              <li key={e} className="break-words text-danger-text">
                {e}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      <Section id="mc-insp-artifacts" title={t('missionControl.inspector.artifacts')}>
        {artifacts.length === 0 && (evidence.data?.items.length ?? 0) === 0 ? (
          <p className="text-fg-muted">{t('missionControl.inspector.noArtifacts')}</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {artifacts.map((a) => (
              <li key={a.id}>
                {can(role, 'artifact:download') ? (
                  <a className="link" href={artifactDownloadPath(a.id)} download>
                    {t('missionControl.inspector.cliArtifact', { attempt: a.attempt })}
                  </a>
                ) : (
                  t('missionControl.inspector.cliArtifact', { attempt: a.attempt })
                )}
              </li>
            ))}
            {(evidence.data?.items.length ?? 0) > 0 ? (
              <li>
                <Link className="link" to={taskHref(taskId, 'evidence')}>
                  {t('missionControl.inspector.evidenceCount', { count: evidence.data?.items.length ?? 0 })}
                </Link>
              </li>
            ) : null}
          </ul>
        )}
      </Section>

      <Section id="mc-insp-events" title={t('missionControl.inspector.recentEvents')}>
        {recent.length === 0 ? (
          <p className="text-fg-muted">{t('tasks.events.empty')}</p>
        ) : (
          <ol className="flex flex-col gap-1" data-testid="inspector-events">
            {recent.map((e) => (
              <li key={e.event_id} className="flex flex-col text-xs" data-seq={e.seq}>
                <span className="text-fg-muted">
                  <Timestamp value={e.timestamp} format="time" /> · {t(`event.type.${e.type}`, { defaultValue: e.type })}
                </span>
                <span className="line-clamp-2 break-words text-fg">{describeEvent(t, e, () => data.title)}</span>
              </li>
            ))}
          </ol>
        )}
      </Section>

      <ContextPackSection taskId={taskId} attempt={latest} />

      <nav aria-label={t('missionControl.inspector.links')} className="flex flex-wrap gap-2">
        <Link className="btn-secondary btn-sm" to={taskHref(taskId)}>
          {t('missionControl.inspector.openTask')}
        </Link>
        <Link className="btn-secondary btn-sm" to={taskHref(taskId, 'diff')}>
          {t('missionControl.inspector.openDiff')}
        </Link>
      </nav>
    </div>
  );
}

export function useTaskSummary(tasks: readonly TaskSummary[], taskId: string | null): TaskSummary | undefined {
  return useMemo(() => (taskId ? tasks.find((x) => x.id === taskId) : undefined), [tasks, taskId]);
}
