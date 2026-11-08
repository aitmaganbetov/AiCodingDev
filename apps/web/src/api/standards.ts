/** Применимость контролей стандартов в проекте (ТЗ §13; listProjectControls / approveProjectControl). */
import { request, seg, type JsonBody, type Query, type Schemas } from './client';

export type ProjectControl = Schemas['ProjectControl'];
export type ProjectControlPage = Schemas['ProjectControlPage'];
export type ControlApprovalState = Schemas['ControlApprovalState'];
export type ProjectControlApprovalRequest = JsonBody<'approveProjectControl'>;
export type ControlApplicability = ProjectControlApprovalRequest['applicability'];
export type AgentRole = Schemas['AgentRole'];
export type ProjectControlsQuery = Query<'listProjectControls'>;

export const AGENT_ROLES: readonly AgentRole[] = [
  'requirements',
  'architect',
  'planner',
  'developer',
  'reviewer',
  'security',
  'qa',
  'database',
  'performance',
  'devops',
  'maintenance',
  'documentation',
];

export const standardsKeys = {
  all: (projectId: string) => ['projectControls', projectId] as const,
  controls: (projectId: string, query: ProjectControlsQuery = {}) => ['projectControls', projectId, query] as const,
};

/** rationale обязателен для not_applicable и tailored (иначе 422). */
export function rationaleRequired(applicability: ControlApplicability): boolean {
  return applicability !== 'applicable';
}

export const standardsApi = {
  controls(projectId: string, query: ProjectControlsQuery = {}, signal?: AbortSignal): Promise<ProjectControlPage> {
    return request({ method: 'GET', path: `/projects/${seg(projectId)}/controls`, query, signal });
  },

  approve(projectId: string, controlId: string, body: ProjectControlApprovalRequest, idempotencyKey: string): Promise<ProjectControl> {
    return request({ method: 'POST', path: `/projects/${seg(projectId)}/controls/${seg(controlId)}/approval`, body, idempotencyKey });
  },
};
