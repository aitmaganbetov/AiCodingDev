/**
 * Боковые разделы Code Review: обсуждения (сводка тредов), находки run (D-8 — состояние проверки, породившей
 * находку), тесты и безопасность (gate executions из отчёта качества в пяти состояниях D-8). Тексты находок —
 * недоверенные данные, только как текст.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import type { Finding } from '../../api/findings';
import type { Evidence, GateExecution } from '../../api/quality';
import type { ReviewThread } from '../../api/reviews';
import { Badge, EmptyState, SeverityBadge, VerificationBadge, type Severity } from '../../components/ui';
import { gateVerificationState } from '../../lib/verification';
import { anchorLabel, gatesFor, threadOrder, type GateCategory } from './reviewModel';

type ThreadFilter = 'OPEN' | 'RESOLVED' | 'ALL';

export function ThreadsSummary({
  threads,
  onOpen,
}: {
  threads: readonly ReviewThread[];
  onOpen: (thread: ReviewThread) => void;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<ThreadFilter>('OPEN');
  const shown = threadOrder(threads).filter((th) => filter === 'ALL' || th.status === filter);
  const count = (f: ThreadFilter) => threads.filter((th) => f === 'ALL' || th.status === f).length;
  return (
    <div className="flex flex-col gap-3">
      <div role="group" aria-label={t('review.threads.filter')} className="flex flex-wrap gap-1">
        {(['OPEN', 'RESOLVED', 'ALL'] as const).map((f) => (
          <button
            key={f}
            type="button"
            aria-pressed={filter === f}
            onClick={() => setFilter(f)}
            className="min-h-target rounded-md border border-border px-2 text-xs font-medium aria-pressed:border-accent aria-pressed:bg-accent-soft aria-pressed:text-accent-soft-fg"
          >
            {t(`review.threads.f${f}`, { count: count(f) })}
          </button>
        ))}
      </div>
      {shown.length === 0 ? (
        <p className="text-sm text-fg-muted">{threads.length === 0 ? t('review.threads.none') : t('review.threads.noneFiltered')}</p>
      ) : (
        <ul className="flex flex-col gap-2" aria-label={t('review.threads.listLabel')} data-testid="threads-summary">
          {shown.map((th) => (
            <li key={th.id}>
              <button
                type="button"
                onClick={() => onOpen(th)}
                className="flex w-full min-w-0 flex-col items-start gap-1 rounded-md border border-border bg-surface p-2 text-left text-xs hover:bg-surface-muted"
              >
                <span className="flex flex-wrap items-center gap-1">
                  <span className="break-all font-mono font-semibold">{anchorLabel(th)}</span>
                  <Badge tone={th.status === 'RESOLVED' ? 'success' : 'warning'}>{t(`review.status.${th.status}`)}</Badge>
                  {th.outdated === true ? <Badge tone="neutral">{t('review.thread.outdated')}</Badge> : null}
                </span>
                <span className="line-clamp-2 whitespace-pre-wrap break-words text-fg-muted">{th.comments[0]?.body ?? ''}</span>
                <span className="text-fg-muted">{t('review.threads.comments', { count: th.comment_count })}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const SEVERITIES: readonly string[] = ['critical', 'high', 'medium', 'low', 'info'];

export function FindingsPanel({
  findings,
  gates,
  evidenceStatus,
  workspaceId,
  onOpenLine,
}: {
  findings: readonly Finding[];
  gates: readonly GateExecution[];
  evidenceStatus: ReadonlyMap<string, Evidence['verification_status']>;
  workspaceId: string;
  onOpenLine: (path: string, line: number) => void;
}) {
  const { t } = useTranslation();
  if (findings.length === 0) return <EmptyState headingLevel={3} title={t('review.findings.emptyTitle')} description={t('review.findings.empty')} />;
  const byGate = new Map(gates.map((g) => [g.id, g]));
  return (
    <ul className="flex flex-col gap-2" aria-label={t('review.findings.listLabel')} data-testid="review-findings">
      {findings.map((f) => {
        const gate = f.gate_execution_id ? byGate.get(f.gate_execution_id) : undefined;
        return (
          <li key={f.id} className="flex flex-col gap-1 rounded-md border border-border bg-surface p-2 text-xs" data-finding-id={f.id}>
            <span className="flex flex-wrap items-center gap-1">
              {SEVERITIES.includes(f.severity) ? <SeverityBadge severity={f.severity as Severity} /> : <Badge>{f.severity}</Badge>}
              <Badge tone="neutral">{t(`quality.enums.finding_status.${f.status}`, { defaultValue: f.status })}</Badge>
              {gate ? (
                <VerificationBadge result={gateVerificationState(gate, gate.evidence_id ? evidenceStatus.get(gate.evidence_id) : undefined)} />
              ) : (
                <span className="text-fg-muted">{t(`quality.enums.finding_source.${f.source ?? 'gate'}`, { defaultValue: f.source ?? '' })}</span>
              )}
            </span>
            <span className="whitespace-pre-wrap break-words font-medium text-fg">{f.title ?? f.rule_id}</span>
            <span className="break-all font-mono text-fg-muted">{f.rule_id}</span>
            <span className="flex flex-wrap gap-3">
              {f.file_path && f.line ? (
                <button type="button" className="min-h-target break-all text-left text-link underline" onClick={() => onOpenLine(f.file_path ?? '', f.line ?? 1)}>
                  {t('review.findings.openLine', { path: f.file_path, line: f.line })}
                </button>
              ) : f.file_path ? (
                <span className="break-all font-mono">{f.file_path}</span>
              ) : null}
              <Link className="min-h-target text-link underline" to={`/w/${workspaceId}/quality/findings/${f.id}`}>
                {t('review.findings.open')}
                <span className="sr-only">: {f.rule_id}</span>
              </Link>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function ChecksPanel({
  gates,
  taskId,
  evidenceStatus,
  workspaceId,
  category,
}: {
  gates: readonly GateExecution[];
  taskId: string | null;
  evidenceStatus: ReadonlyMap<string, Evidence['verification_status']>;
  workspaceId: string;
  category: Exclude<GateCategory, 'other'>;
}) {
  const { t } = useTranslation();
  const items = gatesFor(gates, taskId, category);
  return (
    <section aria-labelledby={`checks-${category}`} className="flex flex-col gap-2" data-testid={`checks-${category}`}>
      <h3 id={`checks-${category}`} className="text-sm font-semibold">
        {t(`review.checks.${category}`)}
      </h3>
      {items.length === 0 ? (
        <p className="text-sm text-fg-muted">{t(`review.checks.${category}None`)}</p>
      ) : (
        <ul className="flex flex-col gap-1" aria-label={t(`review.checks.${category}`)}>
          {items.map((g) => (
            <li key={g.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface px-2 py-1 text-xs" data-gate={g.gate_type}>
              <span className="flex min-w-0 flex-col">
                <span className="break-all font-mono font-semibold">{g.gate_type}</span>
                <span className="text-fg-muted">{g.required ? t('review.checks.required') : t('review.checks.optional')}</span>
              </span>
              <span className="flex flex-wrap items-center gap-2">
                <VerificationBadge result={gateVerificationState(g, g.evidence_id ? evidenceStatus.get(g.evidence_id) : undefined)} showSource />
                {g.evidence_id ? (
                  <Link className="text-link underline" to={`/w/${workspaceId}/quality/evidence/${g.evidence_id}`}>
                    {t('review.checks.evidence')}
                    <span className="sr-only">: {g.gate_type}</span>
                  </Link>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
