/**
 * Поручения run (ТЗ §8.2, API_DESIGN §12.4; FR-CHAT-03/04/05). 202 при создании означает
 * «сохранено», а не «доставлено» или «выполнено»: статус обработки (`status`), доставка
 * (`delivery`) и выполнение (`completion`) — три разных поля Directive.
 */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Directive = Schemas['Directive'];
export type DirectivePage = Schemas['DirectivePage'];
export type DirectiveStatus = Schemas['DirectiveStatus'];
export type DirectiveType = Schemas['DirectiveType'];
export type DirectiveTargetType = Schemas['DirectiveTargetType'];
export type DirectiveImpact = Schemas['DirectiveImpact'];
export type DirectiveAccepted = Schemas['DirectiveAccepted'];
export type DirectiveCreateRequest = JsonBody<'createDirective'>;
export type ListDirectivesQuery = Query<'listDirectives'>;
export type Approval = Schemas['Approval'];

export const DIRECTIVE_TYPES: readonly DirectiveType[] = [
  'ASK',
  'NEW_TASK',
  'CHANGE_REQUEST',
  'STOP',
  'PAUSE',
  'RESUME',
  'REVIEW',
  'APPROVE',
  'REJECT',
];

/** Типы, доступные роли (x-permission-rules createDirective; окончательно решает сервер). */
export function directiveTypesFor(role: string | null | undefined): readonly DirectiveType[] {
  if (role === 'owner' || role === 'admin' || role === 'developer') return DIRECTIVE_TYPES;
  if (role === 'reviewer') return ['ASK', 'REVIEW', 'APPROVE', 'REJECT'];
  return [];
}

/** До DISPATCHED поручение можно отозвать (cancelDirective). */
export const DIRECTIVE_CANCELLABLE: readonly DirectiveStatus[] = [
  'RECEIVED',
  'IMPACT_ANALYSIS_PENDING',
  'IMPACT_READY',
  'AWAITING_CONFIRMATION',
  'CONFIRMED',
];
/** Анализ влияния ещё идёт — результат придёт событием directive.impact_ready. */
export const DIRECTIVE_ANALYZING: readonly DirectiveStatus[] = ['RECEIVED', 'IMPACT_ANALYSIS_PENDING'];
export const DIRECTIVE_TERMINAL: readonly DirectiveStatus[] = ['COMPLETED', 'REJECTED', 'CANCELLED', 'FAILED'];

export const directiveKeys = {
  all: (runId: string) => ['directives', runId] as const,
  list: (runId: string) => ['directives', runId, 'list'] as const,
  detail: (runId: string, directiveId: string) => ['directives', runId, 'detail', directiveId] as const,
  approvals: (runId: string) => ['directives', runId, 'approvals'] as const,
};

export const directivesApi = {
  list(runId: string, query: ListDirectivesQuery = {}, signal?: AbortSignal): Promise<DirectivePage> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/directives`, query, signal });
  },

  create(runId: string, body: DirectiveCreateRequest, idempotencyKey: string): Promise<JsonResponse<'createDirective', 202>> {
    return request({ method: 'POST', path: `/runs/${seg(runId)}/directives`, body, idempotencyKey });
  },

  get(runId: string, directiveId: string, signal?: AbortSignal): Promise<Directive> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/directives/${seg(directiveId)}`, signal });
  },

  /** Подтвердить impact preview (только из AWAITING_CONFIRMATION; If-Match обязателен). */
  confirm(runId: string, directiveId: string, version: number, idempotencyKey: string): Promise<Directive> {
    return request({
      method: 'POST',
      path: `/runs/${seg(runId)}/directives/${seg(directiveId)}/confirm`,
      idempotencyKey,
      ifMatchVersion: version,
    });
  },

  /** Отозвать до DISPATCHED. */
  cancel(runId: string, directiveId: string, version: number, idempotencyKey: string): Promise<Directive> {
    return request({
      method: 'POST',
      path: `/runs/${seg(runId)}/directives/${seg(directiveId)}/cancel`,
      idempotencyKey,
      ifMatchVersion: version,
    });
  },

  /** Ожидающие одобрения run — цели директив APPROVE/REJECT. */
  pendingApprovals(workspaceId: string, runId: string, signal?: AbortSignal): Promise<Schemas['ApprovalPage']> {
    return request({ method: 'GET', path: '/approvals', query: { run_id: runId, status: ['PENDING'] }, workspaceId, signal });
  },
};
