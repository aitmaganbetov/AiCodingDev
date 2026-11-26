import type { AgentRole, Provider, ProviderSettings, ProviderSettingsUpdateRequest } from '../../api/providers';

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

/** ProviderSettings.model: идентификатор модели CLI — уходит в argv, поэтому без пробелов и не с дефиса. */
export const MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:[\]-]{0,99}$/;
/**
 * Модели для выбора. Их называет сам CLI — probe кладёт список в `capabilities.available_models` установки
 * (каталог `codex debug models`, псевдонимы из справки `claude --model`). Запасной список — на случай, когда
 * probe ещё не выполнялся: псевдонимы Claude Code из его справки; у Codex общего набора псевдонимов нет.
 */
export const MODEL_FALLBACK: Readonly<Record<string, readonly string[]>> = {
  claude: ['fable', 'opus', 'sonnet'],
  codex: [],
};

/** Список моделей провайдера: объединение по установкам в порядке CLI, без повторов и без мусора. */
export function modelChoices(provider: Pick<Provider, 'code' | 'installations'>): string[] {
  const found: string[] = [];
  for (const installation of provider.installations) {
    const reported = (installation.capabilities as { available_models?: unknown }).available_models;
    if (!Array.isArray(reported)) continue;
    for (const name of reported) {
      if (typeof name === 'string' && MODEL_PATTERN.test(name) && !found.includes(name)) found.push(name);
    }
  }
  return found.length > 0 ? found : [...(MODEL_FALLBACK[provider.code] ?? [])];
}

export interface SettingsDraft {
  enabled: boolean;
  maxParallel: string;
  timeout: string;
  roles: AgentRole[];
  /** Пустая строка — модель по умолчанию самого CLI (model: null). */
  model: string;
}

export type SettingsField = 'enabled' | 'max_parallel_sessions' | 'default_timeout_seconds' | 'allowed_roles' | 'model';

export function draftFrom(s: ProviderSettings): SettingsDraft {
  return {
    enabled: s.enabled,
    maxParallel: String(s.max_parallel_sessions),
    timeout: String(s.default_timeout_seconds),
    roles: [...s.allowed_roles],
    model: s.model ?? '',
  };
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
): { errors: Partial<Record<SettingsField, 'range' | 'integer' | 'invalid'>>; patch: ProviderSettingsUpdateRequest | null } {
  const errors: Partial<Record<SettingsField, 'range' | 'integer' | 'invalid'>> = {};
  const model = draft.model.trim();
  if (model !== '' && !MODEL_PATTERN.test(model)) errors.model = 'invalid';
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
  if ((model || null) !== (current.model ?? null)) patch.model = model || null;
  return { errors, patch: Object.keys(patch).length > 0 ? patch : null };
}
