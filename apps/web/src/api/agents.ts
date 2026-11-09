/**
 * Профили ролей агентов (UI redesign B7, G-13; AGENT_ROLES §1.2, §2, §5): только чтение. Роли —
 * логические профили, а не постоянно работающие сервисы: активность роли выводится из задач
 * (`GET /tasks?agent_role=&status=LEASED&status=RUNNING`, api/tasks.ts), а не из «онлайн»-статуса.
 */
import { request, type Schemas } from './client';

export type AgentProfile = Schemas['AgentProfile'];
export type AgentProfileList = Schemas['AgentProfileList'];
export type AgentPermissions = Schemas['AgentPermissions'];

export const agentKeys = {
  profiles: (workspaceId: string) => ['agent-profiles', workspaceId] as const,
};

export const agentsApi = {
  /** 12 профилей (не пагинируется); eligible_providers — по настройкам провайдеров workspace. */
  profiles(workspaceId: string, signal?: AbortSignal): Promise<AgentProfileList> {
    return request({ method: 'GET', path: '/agent-profiles', workspaceId, signal });
  },
};
