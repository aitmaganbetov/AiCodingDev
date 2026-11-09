/** Состояние платформы (UI redesign B2, G-05): GET /system/health, право system:health (owner/admin). */
import { request, type Schemas } from './client';

export type SystemHealth = Schemas['SystemHealth'];
export type SystemHealthStatus = Schemas['SystemHealthStatus'];
export type SystemHealthComponent = Schemas['SystemHealthComponent'];
export type WorkerKindHealth = Schemas['WorkerKindHealth'];
export type QueueHealth = Schemas['QueueHealth'];
export type BackupComponentHealth = Schemas['BackupComponentHealth'];

export const systemKeys = {
  health: (workspaceId: string) => ['system', 'health', workspaceId] as const,
};

export const systemApi = {
  /** Ответ кэшируется сервером на cache_ttl_seconds; лимит 30/мин на пользователя. */
  health(workspaceId: string, signal?: AbortSignal): Promise<SystemHealth> {
    return request({ method: 'GET', path: '/system/health', workspaceId, signal });
  },
};
