import { useInfiniteQuery, type UseInfiniteQueryResult, type InfiniteData } from '@tanstack/react-query';
import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { runKeys, runsApi } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import {
  artifactDownloadPath,
  taskKeys,
  tasksApi,
  type AcpMessage,
  type Evidence,
  type GateExecution,
  type TaskAttempt,
} from '../../api/tasks';
import { Badge, CodeBlock, EmptyState, ErrorState, LoadMore, LoadingState, StatusBadge, Table, Timestamp, type Column } from '../../components/ui';
import { describeEvent } from '../../pages/RunPage';
import { providerLabel, roleLabel } from '../runs/labels';

const PAGE = 50;

function Paged<T>({
  query,
  items,
  children,
  empty,
  loading,
}: {
  query: UseInfiniteQueryResult<InfiniteData<unknown>>;
  items: readonly T[];
  children: ReactNode;
  empty: ReactNode;
  loading: string;
}) {
  if (query.isPending) return <LoadingState label={loading} />;
  if (query.isError && items.length === 0) return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  return (
    <>
      {items.length === 0 ? empty : children}
      {query.hasNextPage || query.isFetchNextPageError ? (
        <LoadMore
          shown={items.length}
          hasNextPage={query.hasNextPage}
          isFetchingNextPage={query.isFetchingNextPage}
          nextPageError={query.isFetchNextPageError ? query.error : null}
          onLoadMore={() => void query.fetchNextPage()}
        />
      ) : null}
    </>
  );
}

function useCursorQuery<T>(key: readonly unknown[], fetchPage: (cursor: string | null, signal: AbortSignal) => Promise<{ items: T[]; next_cursor: string | null }>) {
  const query = useInfiniteQuery({
    queryKey: key,
    queryFn: ({ pageParam, signal }) => fetchPage(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const items = useMemo(() => query.data?.pages.flatMap((p) => p.items) ?? [], [query.data]);
  return { query, items };
}

/** Попытки задачи: исполнитель (роль · CLI), время, категория ошибки, executions с выводом. */
export function TaskAttempts({ taskId, canDownload }: { taskId: string; canDownload: boolean }) {
  const { t } = useTranslation();
  const { query, items } = useCursorQuery<TaskAttempt>(taskKeys.attempts(taskId), (cursor, signal) =>
    tasksApi.attempts(taskId, { limit: PAGE, ...(cursor ? { cursor } : {}) }, signal),
  );
  const columns: Column<TaskAttempt>[] = [
    { id: 'no', header: t('tasks.attempts.no'), rowHeader: true, sortValue: (a) => a.attempt_no, cell: (a) => t('tasks.attempts.noValue', { no: a.attempt_no }) },
    { id: 'status', header: t('tasks.attempts.status'), cell: (a) => <StatusBadge kind="task" status={a.status} /> },
    {
      id: 'executor',
      header: t('tasks.attempts.executor'),
      cell: (a) => [roleLabel(t, a.agent_role), providerLabel(t, a.provider)].filter(Boolean).join(' · '),
    },
    {
      id: 'time',
      header: t('tasks.attempts.time'),
      cell: (a) => (
        <>
          {a.started_at ? <Timestamp value={a.started_at} /> : '—'}
          {a.finished_at ? (
            <>
              {' → '}
              <Timestamp value={a.finished_at} format="time" />
            </>
          ) : null}
        </>
      ),
    },
    {
      id: 'failure',
      header: t('tasks.attempts.failure'),
      cell: (a) => (a.failure_category ? t(`tasks.enums.failure.${a.failure_category}`, { defaultValue: a.failure_category }) : '—'),
    },
    {
      id: 'executions',
      header: t('tasks.attempts.executions'),
      cell: (a) =>
        (a.executions ?? []).length === 0 ? (
          '—'
        ) : (
          <ul className="flex flex-col gap-1">
            {(a.executions ?? []).map((x) => (
              <li key={x.id} className="flex flex-wrap items-center gap-1">
                <Badge tone={x.status === 'EXITED' || x.status === 'COLLECTED' ? 'neutral' : 'warning'}>
                  {t(`tasks.enums.execution.${x.status}`, { defaultValue: x.status })}
                </Badge>
                {x.exit_code !== null && x.exit_code !== undefined ? <span>{t('tasks.attempts.exitCode', { code: x.exit_code })}</span> : null}
                {x.output_artifact_id && canDownload ? (
                  <a className="link" href={artifactDownloadPath(x.output_artifact_id)} download>
                    {t('tasks.attempts.output')}
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        ),
    },
    { id: 'routing', header: t('tasks.attempts.routing'), cell: (a) => a.routing_reason ?? '—' },
  ];
  return (
    <Paged query={query} items={items} loading={t('tasks.attempts.loading')} empty={<EmptyState headingLevel={3} title={t('tasks.attempts.empty')} />}>
      <Table caption={t('tasks.attempts.caption')} columns={columns} rows={items} rowKey={(a) => a.id} minWidthClass="min-w-[56rem]" defaultSort={{ columnId: 'no', direction: 'desc' }} />
    </Paged>
  );
}

/** События и логи задачи: история (getRunActivity, task_id) + live из общего потока run. */
export function TaskEvents({ runId, taskId, live }: { runId: string; taskId: string; live: readonly RunEvent[] }) {
  const { t } = useTranslation();
  const [onlyCli, setOnlyCli] = useState(false);
  const { query, items } = useCursorQuery(runKeys.activity(runId, taskId), (cursor, signal) =>
    runsApi.activity(runId, { task_id: taskId, limit: 100, ...(cursor ? { cursor } : {}) }, signal),
  );
  const events = useMemo(() => {
    const bySeq = new Map<number, RunEvent>();
    for (const a of items) {
      const seq = a.seq ?? 0;
      bySeq.set(seq || -bySeq.size - 1, {
        event_id: a.id,
        run_id: a.run_id,
        seq,
        type: a.event_type,
        timestamp: a.occurred_at,
        schema_version: '1.0',
        payload: a.payload,
      });
    }
    for (const e of live) if (!bySeq.has(e.seq)) bySeq.set(e.seq, e);
    return [...bySeq.values()].sort((a, b) => a.seq - b.seq).filter((e) => !onlyCli || e.type === 'cli.output');
  }, [items, live, onlyCli]);
  const empty = <EmptyState headingLevel={3} title={t('tasks.events.empty')} />;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <input id="task-events-cli" type="checkbox" className="h-4 w-4 accent-[rgb(var(--color-accent))]" checked={onlyCli} onChange={(e) => setOnlyCli(e.target.checked)} />
        <label htmlFor="task-events-cli" className="text-sm">
          {t('tasks.events.onlyCli')}
        </label>
      </div>
      <Paged query={query} items={events} loading={t('tasks.events.loading')} empty={empty}>
        {events.length === 0 ? (
          empty
        ) : (
          <ol aria-label={t('tasks.events.label')} className="flex flex-col divide-y divide-border rounded-lg border border-border bg-surface" data-testid="task-events">
            {events.map((e) => {
              const text = describeEvent(t, e, () => undefined);
              return (
                <li key={e.event_id} className="flex flex-col gap-0.5 px-3 py-2 text-sm" data-seq={e.seq} data-type={e.type}>
                  <div className="flex flex-wrap items-baseline gap-x-2 text-fg-muted">
                    <span className="font-mono text-xs">{t('event.seq', { seq: e.seq })}</span>
                    <Timestamp value={e.timestamp} format="time" />
                    <span className="font-semibold text-fg">{t(`event.type.${e.type}`, { defaultValue: e.type })}</span>
                  </div>
                  {text ? <p className={e.type === 'cli.output' ? 'whitespace-pre-wrap break-words font-mono text-xs' : 'whitespace-pre-line break-words'}>{text}</p> : null}
                </li>
              );
            })}
          </ol>
        )}
      </Paged>
    </div>
  );
}

/** ACP-сообщения задачи: тип, отправитель → получатель, время, payload (недоверенные данные — только текст JSON). */
export function TaskMessages({ taskId }: { taskId: string }) {
  const { t } = useTranslation();
  const { query, items } = useCursorQuery<AcpMessage>(taskKeys.messages(taskId), (cursor, signal) =>
    tasksApi.messages(taskId, { limit: PAGE, ...(cursor ? { cursor } : {}) }, signal),
  );
  return (
    <Paged query={query} items={items} loading={t('tasks.messages.loading')} empty={<EmptyState headingLevel={3} title={t('tasks.messages.empty')} />}>
      <ol aria-label={t('tasks.messages.label')} className="flex flex-col gap-3" data-testid="task-messages">
        {items.map((m) => (
          <li key={m.message_id} className="card flex flex-col gap-2 text-sm">
            <div className="flex flex-wrap items-baseline gap-2">
              <Badge tone="info">{t(`chat.enums.acp_type.${m.type}`, { defaultValue: m.type })}</Badge>
              <span className="font-semibold">
                {[roleLabel(t, m.from.role), providerLabel(t, m.from.provider)].filter(Boolean).join(' · ')}
                <span className="font-normal text-fg-muted"> {t('chat.to', { role: roleLabel(t, m.to.role) })}</span>
              </span>
              <Timestamp value={m.created_at} className="text-xs text-fg-muted" />
            </div>
            <CodeBlock code={JSON.stringify(m.payload, null, 2)} label={t('tasks.messages.payload')} language="json" wrap maxHeightClass="max-h-64" />
          </li>
        ))}
      </ol>
    </Paged>
  );
}

const VERIFICATION_TONE: Record<string, 'success' | 'warning' | 'danger' | 'neutral'> = {
  VERIFIED: 'success',
  PENDING: 'neutral',
  UNVERIFIED: 'warning',
  REJECTED: 'danger',
};

/** Gate executions задачи (из отчёта качества run) со ссылками на evidence. */
export function TaskGates({ gates, evidenceIds }: { gates: readonly GateExecution[]; evidenceIds: ReadonlySet<string> }) {
  const { t } = useTranslation();
  const columns: Column<GateExecution>[] = [
    { id: 'gate', header: t('tasks.gates.gate'), rowHeader: true, sortValue: (g) => g.gate_type, cell: (g) => <span className="font-mono">{g.gate_type}</span> },
    { id: 'required', header: t('tasks.gates.required'), cell: (g) => (g.required ? t('tasks.gates.yes') : t('tasks.gates.no')) },
    { id: 'status', header: t('tasks.gates.status'), cell: (g) => <StatusBadge kind="gate" status={g.status} labelKey={`tasks.enums.gate.${g.status}`} /> },
    { id: 'commit', header: t('tasks.gates.commit'), cell: (g) => (g.commit_sha ? <span className="font-mono text-xs">{g.commit_sha.slice(0, 12)}</span> : '—') },
    {
      id: 'evidence',
      header: t('tasks.gates.evidence'),
      cell: (g) =>
        g.evidence_id ? (
          evidenceIds.has(g.evidence_id) ? (
            <a className="link" href={`#evidence-${g.evidence_id}`}>
              {t('tasks.gates.evidenceLink', { id: g.evidence_id.slice(0, 8) })}
            </a>
          ) : (
            <span className="font-mono text-xs">{g.evidence_id.slice(0, 8)}</span>
          )
        ) : (
          '—'
        ),
    },
    {
      id: 'note',
      header: t('tasks.gates.note'),
      cell: (g) => g.error_code ?? g.not_applicable_reason ?? '—',
    },
  ];
  return (
    <Table
      caption={t('tasks.gates.caption')}
      captionVisible
      columns={columns}
      rows={gates}
      rowKey={(g) => g.id}
      empty={t('tasks.gates.empty')}
      rowProps={(g) => ({ 'data-gate-id': g.id, 'data-status': g.status })}
      minWidthClass="min-w-[44rem]"
    />
  );
}

/** Evidence задачи (AC-14): commit, команда, exit code, время, checksum, артефакт. */
export function TaskEvidenceList({ items, canDownload, canDownloadRaw }: { items: readonly Evidence[]; canDownload: boolean; canDownloadRaw: boolean }) {
  const { t, i18n } = useTranslation();
  if (items.length === 0) return <EmptyState headingLevel={3} title={t('tasks.evidence.empty')} />;
  return (
    <ul className="flex flex-col gap-3" aria-label={t('tasks.evidence.label')} data-testid="task-evidence">
      {items.map((e) => {
        const downloadable = canDownload && (e.artifact.kind !== 'raw_cli_log' || canDownloadRaw);
        return (
          <li key={e.id} id={`evidence-${e.id}`} tabIndex={-1} className="card flex flex-col gap-2 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-focus">
            <div className="flex flex-wrap items-center gap-2">
              <h4 className="font-semibold">{t(`tasks.enums.artifact.${e.artifact.kind}`, { defaultValue: e.kind })}</h4>
              <Badge tone={VERIFICATION_TONE[e.verification_status] ?? 'neutral'}>
                {t(`tasks.enums.verification.${e.verification_status}`, { defaultValue: e.verification_status })}
              </Badge>
            </div>
            <dl className="grid grid-cols-1 gap-x-4 gap-y-1 sm:grid-cols-[auto,1fr]">
              <dt className="text-fg-muted">{t('tasks.evidence.producer')}</dt>
              <dd className="break-words">{e.producer}</dd>
              <dt className="text-fg-muted">{t('tasks.evidence.commit')}</dt>
              <dd className="break-all font-mono text-xs">{e.commit_sha ?? '—'}</dd>
              {e.exit_code !== null && e.exit_code !== undefined ? (
                <>
                  <dt className="text-fg-muted">{t('tasks.evidence.exitCode')}</dt>
                  <dd>{e.exit_code}</dd>
                </>
              ) : null}
              <dt className="text-fg-muted">{t('tasks.evidence.time')}</dt>
              <dd>
                {e.started_at ? <Timestamp value={e.started_at} /> : <Timestamp value={e.created_at} />}
                {e.finished_at ? (
                  <>
                    {' → '}
                    <Timestamp value={e.finished_at} format="time" />
                  </>
                ) : null}
              </dd>
              <dt className="text-fg-muted">{t('tasks.evidence.checksum')}</dt>
              <dd className="break-all font-mono text-xs">sha256:{e.artifact.sha256}</dd>
              <dt className="text-fg-muted">{t('tasks.evidence.size')}</dt>
              <dd>{new Intl.NumberFormat(i18n.language).format(e.artifact.size_bytes)} B</dd>
            </dl>
            {e.command ? <CodeBlock code={e.command} label={t('tasks.evidence.command')} language="shell" wrap maxHeightClass="max-h-40" /> : null}
            {downloadable ? (
              <a className="link self-start" href={artifactDownloadPath(e.artifact.id)} download>
                {t('tasks.evidence.download')}
              </a>
            ) : (
              <p className="text-fg-muted">{t('tasks.evidence.noDownload')}</p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

