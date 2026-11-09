/**
 * Модель «Стандарты и соответствие» (F7, план §3.8, ТЗ §13): реестр стандартов из контролей проекта
 * (глобального реестра в API нет — `listProjectControls` по проекту), клиентские фильтры контролей,
 * сопоставление строк матрицы с evidence и подсветка отсутствующих доказательств. Чистые функции.
 */
import type { ComplianceRow } from '../../api/compliance';
import type { EvidenceVerificationStatus } from '../../api/quality';
import type { ProjectControl } from '../../api/standards';
import { complianceVerificationState, type VerificationResult } from '../../lib/verification';

export interface StandardEntry {
  code: string;
  edition: string;
  title: string | null;
  controls: number;
  approved: number;
  /** Утверждены как применимые (applicable/tailored) — контроль проверяется в run. */
  applicable: number;
  notApplicable: number;
  pendingApproval: number;
  blocking: number;
  methods: Record<string, number>;
}

/** Контроли проекта → реестр стандартов (стандарт + редакция), в порядке кода. */
export function standardsRegistry(controls: readonly ProjectControl[]): StandardEntry[] {
  const map = new Map<string, StandardEntry>();
  for (const c of controls) {
    const key = `${c.standard_code}@${c.edition}`;
    const e =
      map.get(key) ??
      ({ code: c.standard_code, edition: c.edition, title: c.standard_title ?? null, controls: 0, approved: 0, applicable: 0, notApplicable: 0, pendingApproval: 0, blocking: 0, methods: {} } satisfies StandardEntry);
    e.controls += 1;
    if (c.approval_state === 'approved') e.approved += 1;
    else e.pendingApproval += 1;
    if (c.applicability === 'APPLICABLE') e.applicable += 1;
    if (c.applicability === 'NOT_APPLICABLE') e.notApplicable += 1;
    if (c.effective_blocking) e.blocking += 1;
    e.methods[c.verification_method] = (e.methods[c.verification_method] ?? 0) + 1;
    if (!e.title && c.standard_title) e.title = c.standard_title;
    map.set(key, e);
  }
  return [...map.values()].sort((a, b) => a.code.localeCompare(b.code) || a.edition.localeCompare(b.edition));
}

export interface ControlFilters {
  standard: string;
  applicability: string;
  blocking: '' | 'yes' | 'no';
  q: string;
}

function matchesText(q: string, ...fields: (string | null | undefined)[]): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return fields.some((f) => (f ?? '').toLowerCase().includes(needle));
}

export function filterControls(controls: readonly ProjectControl[], f: ControlFilters): ProjectControl[] {
  return controls.filter(
    (c) =>
      (!f.standard || c.standard_code === f.standard) &&
      (!f.applicability || c.applicability === f.applicability) &&
      (!f.blocking || c.effective_blocking === (f.blocking === 'yes')) &&
      matchesText(f.q, c.control_code, c.clause_ref, c.description),
  );
}

/**
 * Доказательства строки матрицы:
 * - `not_required` — контроль неприменим / ожидает утверждения применимости;
 * - `missing` — применимый контроль без evidence (подсвечивается);
 * - `unverified` — evidence есть, но не VERIFIED (не доказательство PASS);
 * - `verified` — evidence VERIFIED;
 * - `unknown` — evidence есть, статус не загружен (вне выборки).
 */
export type EvidenceGap = 'not_required' | 'missing' | 'unverified' | 'verified' | 'unknown';

export function evidenceGap(row: Pick<ComplianceRow, 'applicability' | 'status' | 'evidence_id'>, status: EvidenceVerificationStatus | undefined): EvidenceGap {
  if (row.applicability !== 'APPLICABLE' || row.status === 'NOT_APPLICABLE') return 'not_required';
  if (!row.evidence_id) return 'missing';
  if (status === undefined) return 'unknown';
  return status === 'VERIFIED' ? 'verified' : 'unverified';
}

/** Состояние D-8 строки: PASS засчитывается только с evidence VERIFIED. */
export function rowVerification(row: Pick<ComplianceRow, 'status' | 'evidence_id'>, status: EvidenceVerificationStatus | undefined): VerificationResult {
  return complianceVerificationState(row.status, Boolean(row.evidence_id) && status === 'VERIFIED');
}

export function filterRows(rows: readonly ComplianceRow[], f: { q: string; missingOnly: boolean }, statusOf: (id: string) => EvidenceVerificationStatus | undefined): ComplianceRow[] {
  return rows.filter((r) => {
    if (!matchesText(f.q, r.control_code, r.clause_ref)) return false;
    if (!f.missingOnly) return true;
    const gap = evidenceGap(r, r.evidence_id ? statusOf(r.evidence_id) : undefined);
    return gap === 'missing' || gap === 'unverified';
  });
}
