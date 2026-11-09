import { useTranslation } from 'react-i18next';
import type { VerificationDisplayState, VerificationResult } from '../../lib/verification';
import { TONE_CLASS } from './Badge';
import { cx } from './cx';
import { Icon, type IconName } from './Icon';

/**
 * Бейдж одного из пяти состояний верификации (§5.2): иконка + текстовая метка + паттерн
 * (сплошная заливка / штриховка / пунктир / контур с точкой) — состояние различимо без цвета.
 * Исходный статус контракта — в подсказке (title) и для скринридера; `showSource` — видимо.
 */
const STYLE: Record<VerificationDisplayState, { icon: IconName; className: string }> = {
  PASS: { icon: 'verifyPass', className: 'border-verify-pass bg-verify-pass-soft text-verify-pass-fg' },
  FAIL: { icon: 'verifyFail', className: 'border-verify-fail bg-verify-fail-soft text-verify-fail-fg' },
  BLOCKED: { icon: 'verifyBlocked', className: 'pattern-hatch border-verify-blocked bg-verify-blocked-soft text-verify-blocked-fg' },
  NOT_RUN: { icon: 'verifyNotRun', className: 'border-dashed border-verify-not-run bg-verify-not-run-soft text-verify-not-run-fg' },
  INSUFFICIENT_EVIDENCE: {
    icon: 'verifyInsufficient',
    className: 'border-verify-insufficient bg-verify-insufficient-soft text-verify-insufficient-fg',
  },
  RUNNING: { icon: 'statusRunning', className: TONE_CLASS.info },
  NOT_APPLICABLE: { icon: 'statusNeutral', className: TONE_CLASS.neutral },
  MANUAL_REVIEW: { icon: 'statusPaused', className: TONE_CLASS.warning },
};

export function VerificationBadge({
  result,
  showSource = false,
  className,
}: {
  result: VerificationResult;
  showSource?: boolean;
  className?: string;
}) {
  const { t } = useTranslation();
  const { icon, className: style } = STYLE[result.state];
  const source = result.source ? t('ds.verification.source', { status: result.source }) : undefined;
  return (
    <span
      className={cx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-semibold', style, className)}
      title={source}
      data-verification={result.state}
      data-source={result.source ?? undefined}
    >
      <Icon name={icon} className="h-3.5 w-3.5 shrink-0" spin={result.state === 'RUNNING'} />
      {result.state === 'INSUFFICIENT_EVIDENCE' ? <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" /> : null}
      {t(`ds.verification.${result.state}`)}
      {source ? <span className={showSource ? 'ml-1 font-mono font-normal' : 'sr-only'}>{showSource ? `(${result.source})` : `, ${source}`}</span> : null}
    </span>
  );
}

export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info';

const SEVERITY: Record<Severity, { icon: IconName; tone: keyof typeof TONE_CLASS }> = {
  critical: { icon: 'severityCritical', tone: 'danger' },
  high: { icon: 'severityHigh', tone: 'danger' },
  medium: { icon: 'severityMedium', tone: 'warning' },
  low: { icon: 'info', tone: 'info' },
  info: { icon: 'info', tone: 'neutral' },
};

/** Серьёзность находки (§5.2): тон + иконка + метка текстом всегда. */
export function SeverityBadge({ severity, className }: { severity: Severity; className?: string }) {
  const { t } = useTranslation();
  const s = SEVERITY[severity];
  return (
    <span
      className={cx('inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-semibold', TONE_CLASS[s.tone], className)}
      data-severity={severity}
    >
      <Icon name={s.icon} className="h-3.5 w-3.5 shrink-0" />
      {t(`ds.severity.${severity}`)}
    </span>
  );
}
