/**
 * Модель экрана «Гейты качества» (F7, план §3.7, §5.2; D-8): группы проверок, сводное состояние
 * группы в одном run и агрегаты analytics по группам. Только чистые функции (unit-тест).
 *
 * Группы — по точному `gate_type` из перечисления QUALITY_GATES §11 (FORMAT, LINT, TYPECHECK, BUILD,
 * UNIT, INTEGRATION, E2E, REVIEW, COVERAGE, SAST, SCA, SECRETS, IAC, SBOM, SIGN, …). Типы вне
 * перечисления не угадываются по похожему имени — они попадают в «Прочие проверки».
 */
import type { AnalyticsSummary } from '../../api/analytics';
import type { EvidenceVerificationStatus, GateExecution } from '../../api/quality';
import {
  gateAggregateOutcomes,
  gateVerificationState,
  type GateAggregateCounts,
  type GateAggregateOutcome,
  type VerificationDisplayState,
  type VerificationResult,
} from '../../lib/verification';

export type GateCategory = 'lint' | 'typecheck' | 'build' | 'unit' | 'integration' | 'e2e' | 'review' | 'coverage' | 'security' | 'other';

/** Порядок групп на экране (ТЗ: lint, typecheck, build, unit, integration, e2e, code review, coverage, security gates). */
export const GATE_CATEGORIES: readonly GateCategory[] = ['lint', 'typecheck', 'build', 'unit', 'integration', 'e2e', 'review', 'coverage', 'security', 'other'];

const CATEGORY_BY_TYPE: Readonly<Record<string, GateCategory>> = {
  FORMAT: 'lint',
  LINT: 'lint',
  TYPECHECK: 'typecheck',
  BUILD: 'build',
  UNIT: 'unit',
  INTEGRATION: 'integration',
  E2E: 'e2e',
  REVIEW: 'review',
  COVERAGE: 'coverage',
  SAST: 'security',
  SCA: 'security',
  SECRETS: 'security',
  SBOM: 'security',
  IAC: 'security',
  SIGN: 'security',
};

export function gateCategory(gateType: string): GateCategory {
  return CATEGORY_BY_TYPE[gateType] ?? 'other';
}

/**
 * Порядок «хуже → лучше» для сводного состояния группы: один FAIL делает группу FAIL; PASS группы —
 * только если все исполненные gates группы PASS с подтверждённым evidence (остальные — неприменимы).
 */
const SEVERITY_ORDER: readonly VerificationDisplayState[] = [
  'FAIL',
  'BLOCKED',
  'RUNNING',
  'INSUFFICIENT_EVIDENCE',
  'NOT_RUN',
  'MANUAL_REVIEW',
  'PASS',
  'NOT_APPLICABLE',
];

export function worstState(states: readonly VerificationDisplayState[]): VerificationDisplayState | null {
  let best: VerificationDisplayState | null = null;
  for (const s of states) if (best === null || SEVERITY_ORDER.indexOf(s) < SEVERITY_ORDER.indexOf(best)) best = s;
  return best;
}

export interface GateRow {
  gate: GateExecution;
  category: GateCategory;
  verification: VerificationResult;
}

/**
 * Gates run → строки с состоянием D-8. `evidenceStatus(id)` — статус evidence gate, если загружен
 * (undefined — неизвестен: PASS тогда «Недостаточно доказательств», никогда не «Пройдено»).
 */
export function gateRows(gates: readonly GateExecution[], evidenceStatus: (evidenceId: string) => EvidenceVerificationStatus | undefined): GateRow[] {
  return gates.map((gate) => ({
    gate,
    category: gateCategory(gate.gate_type),
    verification: gateVerificationState(gate, gate.evidence_id ? evidenceStatus(gate.evidence_id) : undefined),
  }));
}

export interface CategorySummary {
  category: GateCategory;
  /** null — в run нет gates этой группы. */
  state: VerificationDisplayState | null;
  gates: GateRow[];
}

export function categorySummaries(rows: readonly GateRow[]): CategorySummary[] {
  return GATE_CATEGORIES.map((category) => {
    const gates = rows.filter((r) => r.category === category);
    return { category, state: worstState(gates.map((g) => g.verification.state)), gates };
  }).filter((c) => c.category !== 'other' || c.gates.length > 0);
}

type GateStats = AnalyticsSummary['gates'][number];

export interface CategoryAggregate {
  category: GateCategory;
  gateTypes: string[];
  counts: GateAggregateCounts;
  /** Всего выполнений в окне (включая SKIPPED). */
  total: number;
  outcomes: GateAggregateOutcome[];
  /** Доля «Пройдено» (PASS + VERIFIED evidence) среди исполненных (PASS/FAIL/ERROR); null — исполненных нет. */
  verifiedRate: number | null;
}

/** analytics.gates[] → агрегаты по группам (пять состояний D-8 через `gateAggregateOutcomes`). */
export function categoryAggregates(gates: readonly GateStats[]): CategoryAggregate[] {
  return GATE_CATEGORIES.map((category) => {
    const items = gates.filter((g) => gateCategory(g.gate_type) === category);
    const counts: GateAggregateCounts = { passed_verified: 0, passed_unverified: 0, failed: 0, errored: 0, skipped: 0 };
    let total = 0;
    let executed = 0;
    for (const g of items) {
      counts.passed_verified += g.passed_verified;
      counts.passed_unverified += g.passed_unverified;
      counts.failed += g.failed;
      counts.errored += g.errored;
      counts.skipped += g.skipped;
      total += g.total;
      executed += g.passed + g.failed + g.errored;
    }
    return {
      category,
      gateTypes: items.map((g) => g.gate_type).sort(),
      counts,
      total,
      outcomes: gateAggregateOutcomes(counts),
      verifiedRate: executed > 0 ? counts.passed_verified / executed : null,
    };
  }).filter((c) => c.category !== 'other' || c.gateTypes.length > 0);
}

/** Доля «Пройдено» одного типа gate (PASS + VERIFIED evidence среди исполненных). */
export function gateTypeVerifiedRate(g: GateStats): number | null {
  const executed = g.passed + g.failed + g.errored;
  return executed > 0 ? g.passed_verified / executed : null;
}

/** Окно analytics экрана (как у Dashboard): последние `days` суток до начала следующего часа UTC. */
export function qualityWindow(days: number, now: Date = new Date()): { from: string; to: string } {
  const end = new Date(now);
  end.setUTCMinutes(0, 0, 0);
  end.setUTCHours(end.getUTCHours() + 1);
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000);
  return { from: start.toISOString(), to: end.toISOString() };
}
