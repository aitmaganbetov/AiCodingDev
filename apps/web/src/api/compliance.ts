/** Матрица соответствия run и вердикт человека по контролю (ТЗ §13, QUALITY_GATES §8.2). */
import { request, seg, type JsonBody, type Query, type Schemas } from './client';

export type ComplianceRow = Schemas['ComplianceRow'];
export type CompliancePage = Schemas['CompliancePage'];
export type ComplianceStatus = Schemas['ComplianceStatus'];
export type ComplianceReviewRequest = JsonBody<'reviewComplianceControl'>;
export type RunComplianceQuery = Query<'getRunCompliance'>;

export const COMPLIANCE_STATUSES: readonly ComplianceStatus[] = ['PASS', 'FAIL', 'UNVERIFIED', 'MANUAL_REVIEW', 'NOT_APPLICABLE'];

export const complianceKeys = {
  all: (runId: string) => ['compliance', runId] as const,
  run: (runId: string, query: RunComplianceQuery = {}) => ['compliance', runId, query] as const,
};

const MANUAL_METHODS: readonly string[] = ['manual', 'document_check', 'agent_review'];
const REVIEW_ROLES: readonly string[] = ['owner', 'admin', 'developer', 'reviewer'];

/**
 * Можно ли запросить вердикт человека (API_DESIGN §5.5): manual/document_check/agent_review в
 * MANUAL_REVIEW/UNVERIFIED или UNVERIFIED автоматического контроля; применимость утверждена.
 * Остальное (вердикт gate, NOT_APPLICABLE, терминальный run) сервер отклонит 409.
 */
export function isComplianceReviewable(row: ComplianceRow): boolean {
  if (row.applicability !== 'APPLICABLE') return false;
  if (row.status === 'UNVERIFIED') return true;
  return row.status === 'MANUAL_REVIEW' && MANUAL_METHODS.includes(row.verification_method ?? '');
}

/** compliance:review — reviewer и выше (только UX). */
export function canReviewCompliance(role: string | null | undefined): boolean {
  return Boolean(role && REVIEW_ROLES.includes(role));
}

export const complianceApi = {
  run(runId: string, query: RunComplianceQuery = {}, signal?: AbortSignal): Promise<CompliancePage> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/compliance`, query, signal });
  },

  review(runId: string, controlId: string, body: ComplianceReviewRequest, idempotencyKey: string): Promise<ComplianceRow> {
    return request({ method: 'POST', path: `/runs/${seg(runId)}/compliance/${seg(controlId)}/review`, body, idempotencyKey });
  },
};
