import { useTranslation } from 'react-i18next';
import type { RunStatus, TaskStatus } from '../api/runs';
import { Badge, type Tone } from './ui/Badge';

const TONES: Record<string, Tone> = {
  RUNNING: 'info',
  VERIFYING: 'info',
  DISCOVERY: 'info',
  LEASED: 'info',
  READY: 'info',
  COMPLETED: 'success',
  SUCCEEDED: 'success',
  PASS: 'success',
  PAUSED: 'warning',
  BLOCKED: 'warning',
  AWAITING_APPROVAL: 'warning',
  RETRY_WAIT: 'warning',
  MANUAL_REVIEW: 'warning',
  UNVERIFIED: 'warning',
  FAILED: 'danger',
  FAIL: 'danger',
  ERROR: 'danger',
  CANCELLED: 'neutral',
};

/** Тон по значению статуса (общий для run/task/gate/compliance); неизвестный — neutral. */
export function statusTone(status: string): Tone {
  return TONES[status] ?? 'neutral';
}

/**
 * Бейдж статуса: текст — из `enums.<kind>_status.<STATUS>` (или `labelKey`), тон — statusTone.
 * Новые сущности: `<StatusBadge kind="approval" status=…>` + ключи `enums.approval_status.*`
 * в своём namespace (например approvals.json → approvals.enums… через labelKey).
 */
export function StatusBadge({
  kind,
  status,
  labelKey,
}: {
  kind: 'run' | 'task' | (string & {});
  status: RunStatus | TaskStatus | (string & {});
  labelKey?: string;
}) {
  const { t } = useTranslation();
  return (
    <Badge tone={statusTone(status)} data-status={status}>
      {t(labelKey ?? `enums.${kind}_status.${status}`, { defaultValue: status })}
    </Badge>
  );
}

export const RUN_TERMINAL: readonly string[] = ['COMPLETED', 'FAILED', 'CANCELLED'];
/** Статусы, из которых FSM допускает PAUSED (packages/orchestration fsm._RUN). */
export const RUN_PAUSABLE: readonly string[] = ['DISCOVERY', 'PLANNED', 'AWAITING_APPROVAL', 'RUNNING', 'VERIFYING', 'BLOCKED'];
