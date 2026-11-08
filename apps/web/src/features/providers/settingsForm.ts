import type { AgentRole, ProviderSettings, ProviderSettingsUpdateRequest } from '../../api/providers';

/** ProviderSettings: max_parallel_sessions 0…64, default_timeout_seconds 60…14400. */
export const MAX_PARALLEL = { min: 0, max: 64 } as const;
export const TIMEOUT_SECONDS = { min: 60, max: 14400 } as const;
export const AGENT_ROLES = [
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
] as const satisfies readonly AgentRole[];

export interface SettingsDraft {
  enabled: boolean;
  maxParallel: string;
  timeout: string;
  roles: AgentRole[];
}

export type SettingsField = 'enabled' | 'max_parallel_sessions' | 'default_timeout_seconds' | 'allowed_roles';

export function draftFrom(s: ProviderSettings): SettingsDraft {
  return { enabled: s.enabled, maxParallel: String(s.max_parallel_sessions), timeout: String(s.default_timeout_seconds), roles: [...s.allowed_roles] };
}

function intInRange(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const n = Number(value.trim());
  return n >= min && n <= max ? n : null;
}

/**
 * Проверка черновика и merge-patch только изменённых полей (minProperties: 1; иные поля — 422,
 * поэтому тело строится только из разрешённых ключей). `patch === null` — изменений нет.
 */
export function buildSettingsPatch(
  current: ProviderSettings,
  draft: SettingsDraft,
): { errors: Partial<Record<SettingsField, 'range' | 'integer'>>; patch: ProviderSettingsUpdateRequest | null } {
  const errors: Partial<Record<SettingsField, 'range' | 'integer'>> = {};
  const maxParallel = intInRange(draft.maxParallel, MAX_PARALLEL.min, MAX_PARALLEL.max);
  const timeout = intInRange(draft.timeout, TIMEOUT_SECONDS.min, TIMEOUT_SECONDS.max);
  if (maxParallel === null) errors.max_parallel_sessions = 'range';
  if (timeout === null) errors.default_timeout_seconds = 'range';
  if (Object.keys(errors).length > 0) return { errors, patch: null };

  const patch: ProviderSettingsUpdateRequest = {};
  if (draft.enabled !== current.enabled) patch.enabled = draft.enabled;
  if (maxParallel !== null && maxParallel !== current.max_parallel_sessions) patch.max_parallel_sessions = maxParallel;
  if (timeout !== null && timeout !== current.default_timeout_seconds) patch.default_timeout_seconds = timeout;
  const ordered = AGENT_ROLES.filter((r) => draft.roles.includes(r));
  const currentSet = new Set(current.allowed_roles);
  if (ordered.length !== currentSet.size || ordered.some((r) => !currentSet.has(r))) patch.allowed_roles = ordered;
  return { errors, patch: Object.keys(patch).length > 0 ? patch : null };
}
