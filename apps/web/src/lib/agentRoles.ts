import type { components } from '../api/schema';
import type { IconName } from '../components/ui/Icon';

/**
 * Идентичность 12 ролей агентов (`AgentRole`, ТЗ §4 = AGENT_ROLES.md = контракт; решение D-2) —
 * docs/design/UI_REDESIGN_PLAN.md §5.7. Основное — глиф + код + название (текст), цвет семейства —
 * вторичный признак (дальтонизм: семейства различаются и глифом, и светлотой; статусные зелёный/
 * жёлтый/красный не используются). Компоненты платформы (оркестратор, маршрутизатор, верификация,
 * CLI Gateway) — не агенты: показываются как «Система».
 */
export type AgentRole = components['schemas']['AgentRole'];
export type AgentFamily = 'discover' | 'build' | 'verify' | 'sustain';

export interface AgentRoleMeta {
  code: string;
  icon: IconName;
  family: AgentFamily;
}

export const AGENT_ROLE_META = {
  requirements: { code: 'REQ', icon: 'roleRequirements', family: 'discover' },
  architect: { code: 'ARC', icon: 'roleArchitect', family: 'discover' },
  planner: { code: 'PLN', icon: 'rolePlanner', family: 'discover' },
  developer: { code: 'DEV', icon: 'roleDeveloper', family: 'build' },
  database: { code: 'DB', icon: 'roleDatabase', family: 'build' },
  devops: { code: 'OPS', icon: 'roleDevops', family: 'build' },
  reviewer: { code: 'REV', icon: 'roleReviewer', family: 'verify' },
  security: { code: 'SEC', icon: 'roleSecurity', family: 'verify' },
  qa: { code: 'QA', icon: 'roleQa', family: 'verify' },
  performance: { code: 'PRF', icon: 'rolePerformance', family: 'verify' },
  maintenance: { code: 'MNT', icon: 'roleMaintenance', family: 'sustain' },
  documentation: { code: 'DOC', icon: 'roleDocumentation', family: 'sustain' },
} as const satisfies Record<AgentRole, AgentRoleMeta>;

/** Порядок ролей — по фазам конвейера (как в таблице §5.7). */
export const AGENT_ROLE_ORDER = Object.keys(AGENT_ROLE_META) as AgentRole[];

export function isAgentRole(value: unknown): value is AgentRole {
  return typeof value === 'string' && Object.hasOwn(AGENT_ROLE_META, value);
}

/** Метаданные роли; любое значение вне `AgentRole` (orchestrator, router, user…) — null («Система»). */
export function agentRoleMeta(value: string | null | undefined): AgentRoleMeta | null {
  return isAgentRole(value) ? AGENT_ROLE_META[value] : null;
}
