/** Находки run (QUALITY_GATES §5.3, API_DESIGN §5.5): список, карточка, решение человека. */
import { request, seg, type JsonBody, type Query, type Schemas } from './client';

export type Finding = Schemas['Finding'];
export type FindingPage = Schemas['FindingPage'];
export type FindingSeverity = Schemas['FindingSeverity'];
export type FindingStatus = Schemas['FindingStatus'];
export type FindingTransitionRequest = JsonBody<'transitionFinding'>;
export type FindingTargetStatus = FindingTransitionRequest['to_status'];
export type ListFindingsQuery = Query<'listRunFindings'>;

export const FINDING_SEVERITIES: readonly FindingSeverity[] = ['critical', 'high', 'medium', 'low', 'info'];
export const FINDING_STATUSES: readonly FindingStatus[] = ['OPEN', 'ACKNOWLEDGED', 'FIXED', 'VERIFIED_FIXED', 'FALSE_POSITIVE', 'ACCEPTED_RISK'];

export const findingKeys = {
  list: (runId: string, query: ListFindingsQuery = {}) => ['findings', runId, query] as const,
  all: (runId: string) => ['findings', runId] as const,
  detail: (findingId: string) => ['finding', findingId] as const,
};

/**
 * FSM решений человека (DATABASE_DESIGN §18.3, контракт transitionFinding):
 * OPEN → ACKNOWLEDGED | FALSE_POSITIVE | ACCEPTED_RISK; ACKNOWLEDGED → OPEN | FALSE_POSITIVE | ACCEPTED_RISK;
 * FALSE_POSITIVE | ACCEPTED_RISK → OPEN. FIXED / VERIFIED_FIXED выставляет только Verification Engine.
 */
const TRANSITIONS: Record<FindingStatus, readonly FindingTargetStatus[]> = {
  OPEN: ['ACKNOWLEDGED', 'FALSE_POSITIVE', 'ACCEPTED_RISK'],
  ACKNOWLEDGED: ['OPEN', 'FALSE_POSITIVE', 'ACCEPTED_RISK'],
  FALSE_POSITIVE: ['OPEN'],
  ACCEPTED_RISK: ['OPEN'],
  FIXED: [],
  VERIFIED_FIXED: [],
};

export function allowedFindingTransitions(status: FindingStatus | (string & {})): readonly FindingTargetStatus[] {
  return TRANSITIONS[status as FindingStatus] ?? [];
}

const ROLE_RANK: Record<string, number> = { viewer: 0, reviewer: 1, developer: 2, admin: 3, owner: 4 };

/** API_DESIGN §5.5: ACCEPTED_RISK — admin и выше, остальные — reviewer и выше (только UX; решает сервер). */
export function canTransitionFinding(role: string | null | undefined, to: FindingTargetStatus): boolean {
  const rank = role ? ROLE_RANK[role] : undefined;
  if (rank === undefined) return false;
  return rank >= (to === 'ACCEPTED_RISK' ? ROLE_RANK.admin! : ROLE_RANK.reviewer!);
}

/** critical-профиль: принять риск можно не выше high (иначе сервер ответит 422). */
export function acceptedRiskBlockedByProfile(severity: FindingSeverity, profile: string | null | undefined): boolean {
  return profile === 'critical' && severity === 'critical';
}

export const findingsApi = {
  list(runId: string, query: ListFindingsQuery = {}, signal?: AbortSignal): Promise<FindingPage> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/findings`, query, signal });
  },

  get(findingId: string, signal?: AbortSignal): Promise<Finding> {
    return request({ method: 'GET', path: `/findings/${seg(findingId)}`, signal });
  },

  /** If-Match: "v<version>" — решение по той версии, которую видел человек (API_DESIGN §9). */
  transition(findingId: string, body: FindingTransitionRequest, idempotencyKey: string, version: number): Promise<Finding> {
    return request({ method: 'POST', path: `/findings/${seg(findingId)}/transitions`, body, idempotencyKey, ifMatchVersion: version });
  },
};
