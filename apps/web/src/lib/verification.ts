import type { ComplianceStatus } from '../api/compliance';
import type { EvidenceVerificationStatus, GateExecution, GateExecutionStatus } from '../api/quality';

/**
 * Пять состояний верификации UI (docs/design/UI_REDESIGN_PLAN.md §5.2, решение D-8) — производное
 * отображение поверх контрактных статусов. Правило evidence-first (ТЗ §14, AC-11): «Пройдено»
 * невозможно без evidence в статусе VERIFIED; исходный статус контракта всегда показывается рядом.
 */
export type VerificationState = 'PASS' | 'FAIL' | 'BLOCKED' | 'NOT_RUN' | 'INSUFFICIENT_EVIDENCE';
/** Вспомогательные состояния (не входят в пять, но нужны для честного отображения). */
export type VerificationAuxState = 'RUNNING' | 'NOT_APPLICABLE' | 'MANUAL_REVIEW';
export type VerificationDisplayState = VerificationState | VerificationAuxState;

export const VERIFICATION_STATES: readonly VerificationState[] = ['PASS', 'FAIL', 'BLOCKED', 'NOT_RUN', 'INSUFFICIENT_EVIDENCE'];

export interface VerificationResult {
  state: VerificationDisplayState;
  /** Исходный статус контракта (для подсказки/деталей); null — выполнения не было. */
  source: string | null;
}

/**
 * Gate → состояние. `evidenceStatus` — статус evidence из `evidence_id` (если загружен; иначе
 * undefined: тогда PASS не подтверждён и показывается как «Недостаточно доказательств»).
 * `execution === null` — обязательный gate без выполнения → «Не запускалось».
 * `blockedUpstream` — задача/run в BLOCKED до гейта → «Заблокировано».
 */
export function gateVerificationState(
  execution: Pick<GateExecution, 'status' | 'evidence_id'> | null | undefined,
  evidenceStatus?: EvidenceVerificationStatus | null,
  options: { blockedUpstream?: boolean } = {},
): VerificationResult {
  if (!execution) return { state: options.blockedUpstream ? 'BLOCKED' : 'NOT_RUN', source: null };
  const source = execution.status;
  return { state: mapGateStatus(execution.status, Boolean(execution.evidence_id), evidenceStatus, options.blockedUpstream), source };
}

function mapGateStatus(
  status: GateExecutionStatus,
  hasEvidence: boolean,
  evidenceStatus: EvidenceVerificationStatus | null | undefined,
  blockedUpstream = false,
): VerificationDisplayState {
  switch (status) {
    case 'PASS':
      return hasEvidence && evidenceStatus === 'VERIFIED' ? 'PASS' : 'INSUFFICIENT_EVIDENCE';
    case 'FAIL':
      return 'FAIL';
    case 'ERROR':
      return 'BLOCKED';
    case 'RUNNING':
      return 'RUNNING';
    case 'NOT_APPLICABLE':
      return 'NOT_APPLICABLE';
    case 'PENDING':
    case 'SKIPPED':
      return blockedUpstream ? 'BLOCKED' : 'NOT_RUN';
    default:
      // Новый статус контракта, неизвестный UI: никогда не «успех».
      return 'NOT_RUN';
  }
}

/**
 * Контроль compliance → состояние. PASS засчитывается только при наличии проверенного evidence
 * (`hasVerifiedEvidence`); UNVERIFIED — «Недостаточно доказательств».
 */
export function complianceVerificationState(status: ComplianceStatus, hasVerifiedEvidence: boolean): VerificationResult {
  const state: VerificationDisplayState =
    status === 'PASS'
      ? hasVerifiedEvidence
        ? 'PASS'
        : 'INSUFFICIENT_EVIDENCE'
      : status === 'FAIL'
        ? 'FAIL'
        : status === 'UNVERIFIED'
          ? 'INSUFFICIENT_EVIDENCE'
          : status === 'MANUAL_REVIEW'
            ? 'MANUAL_REVIEW'
            : status === 'NOT_APPLICABLE'
              ? 'NOT_APPLICABLE'
              : 'NOT_RUN';
  return { state, source: status };
}
