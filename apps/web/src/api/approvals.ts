/** Подтверждения опасных действий (ТЗ §15, API_DESIGN §5.3; AC-13). */
import { request, seg, type JsonBody, type Query, type Schemas } from './client';

export type Approval = Schemas['Approval'];
export type ApprovalPage = Schemas['ApprovalPage'];
export type ApprovalStatus = Schemas['ApprovalStatus'];
export type ApprovalActionType = Schemas['ApprovalActionType'];
export type RiskLevel = Schemas['RiskLevel'];
export type ApprovalDecisionRequest = JsonBody<'decideApproval'>;
export type ApprovalDecision = ApprovalDecisionRequest['decision'];
export type ListApprovalsQuery = Query<'listApprovals'>;

export const DECIDED_STATUSES: readonly ApprovalStatus[] = ['APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED'];

/** Действия, для которых действует four-eyes (API_DESIGN §5.3). */
export const FOUR_EYES_ACTIONS: readonly ApprovalActionType[] = [
  'MERGE',
  'PRODUCTION_DEPLOY',
  'DESTRUCTIVE_MIGRATION',
  'DATA_DELETION',
  'SECRET_CHANGE',
  'UNRESTRICTED_NETWORK',
  'CRITICAL_DIRECTIVE',
  'LOCAL_EXECUTION_FALLBACK',
  'VULN_DB_UPDATE',
];

/** События SSE run, после которых список/карточка подтверждений устарели. */
export const APPROVAL_EVENT_TYPES: readonly string[] = ['approval.requested', 'approval.decided'];

export const approvalKeys = {
  all: (workspaceId: string) => ['approvals', workspaceId] as const,
  list: (workspaceId: string, query: ListApprovalsQuery = {}) => ['approvals', workspaceId, query] as const,
  detail: (approvalId: string) => ['approval', approvalId] as const,
};

/** Причина обязательна для REJECTED и для critical-действий (контракт ApprovalDecisionRequest, 422). */
export function decisionReasonRequired(decision: ApprovalDecision, risk: RiskLevel): boolean {
  return decision === 'REJECTED' || risk === 'critical';
}

/** Истёк ли срок (по часам клиента; окончательно решает сервер). */
export function isApprovalExpired(approval: Pick<Approval, 'expires_at'>, now: number = Date.now()): boolean {
  if (!approval.expires_at) return false;
  const t = Date.parse(approval.expires_at);
  return !Number.isNaN(t) && t <= now;
}

export const approvalsApi = {
  list(workspaceId: string, query: ListApprovalsQuery = {}, signal?: AbortSignal): Promise<ApprovalPage> {
    // client.ts принимает строковые query: boolean → "true" / "false".
    const { decidable_by_me: mine, ...rest } = query;
    return request({
      method: 'GET',
      path: '/approvals',
      query: { ...rest, decidable_by_me: mine === undefined ? undefined : String(mine) },
      workspaceId,
      signal,
    });
  },

  get(approvalId: string, signal?: AbortSignal): Promise<Approval> {
    return request({ method: 'GET', path: `/approvals/${seg(approvalId)}`, signal });
  },

  /** If-Match: "v<version>" — решение по версии, которую видел человек. */
  decide(approvalId: string, body: ApprovalDecisionRequest, idempotencyKey: string, version: number): Promise<Approval> {
    return request({ method: 'POST', path: `/approvals/${seg(approvalId)}/decision`, body, idempotencyKey, ifMatchVersion: version });
  },
};
