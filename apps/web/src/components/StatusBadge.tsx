import { useTranslation } from 'react-i18next';
import type { RunStatus, TaskStatus } from '../api/client';

type Tone = 'neutral' | 'active' | 'success' | 'warning' | 'danger';

// Контраст текста ≥ 4.5:1 на фоне бейджа (WCAG 1.4.3); цвет дублируется текстом статуса.
const TONE_CLASS: Record<Tone, string> = {
  neutral: 'border-slate-300 bg-slate-100 text-slate-800',
  active: 'border-blue-300 bg-blue-50 text-blue-900',
  success: 'border-green-300 bg-green-50 text-green-900',
  warning: 'border-amber-300 bg-amber-50 text-amber-900',
  danger: 'border-red-300 bg-red-50 text-red-900',
};

const TONES: Record<string, Tone> = {
  RUNNING: 'active',
  VERIFYING: 'active',
  DISCOVERY: 'active',
  LEASED: 'active',
  READY: 'active',
  COMPLETED: 'success',
  SUCCEEDED: 'success',
  PAUSED: 'warning',
  BLOCKED: 'warning',
  AWAITING_APPROVAL: 'warning',
  RETRY_WAIT: 'warning',
  FAILED: 'danger',
  CANCELLED: 'neutral',
};

export function StatusBadge({ kind, status }: { kind: 'run' | 'task'; status: RunStatus | TaskStatus | string }) {
  const { t } = useTranslation();
  const tone = TONES[status] ?? 'neutral';
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-semibold ${TONE_CLASS[tone]}`}
      data-status={status}
    >
      {t(`enums.${kind}_status.${status}`, { defaultValue: status })}
    </span>
  );
}

export const RUN_TERMINAL: readonly string[] = ['COMPLETED', 'FAILED', 'CANCELLED'];
/** Статусы, из которых FSM допускает PAUSED (packages/orchestration fsm._RUN). */
export const RUN_PAUSABLE: readonly string[] = ['DISCOVERY', 'PLANNED', 'AWAITING_APPROVAL', 'RUNNING', 'VERIFYING', 'BLOCKED'];
