import type { TFunction } from 'i18next';

/** Подпись значения перечисления: `enums.<group>.<value>` (common/runs), неизвестное значение — как есть. */
export function enumLabel(t: TFunction, group: string, value: unknown): string {
  const s = typeof value === 'string' ? value : typeof value === 'number' ? String(value) : '';
  return s ? t(`enums.${group}.${s}`, { defaultValue: s }) : '';
}

/** Роль агента (ACP-участник: роль ∪ orchestrator/user). */
export function roleLabel(t: TFunction, role: unknown): string {
  return enumLabel(t, 'agent_role', role);
}

/** CLI-провайдер (codex/claude). */
export function providerLabel(t: TFunction, provider: unknown): string {
  const s = typeof provider === 'string' ? provider : '';
  return s ? t(`runs.enums.provider.${s}`, { defaultValue: s }) : '';
}

/** «Роль · провайдер» — текущий исполнитель задачи (ТЗ §11). */
export function executorLabel(t: TFunction, role: unknown, provider: unknown): string {
  const p = providerLabel(t, provider);
  return p ? t('runs.executor', { role: roleLabel(t, role), provider: p }) : roleLabel(t, role);
}

/** Статусы задачи, при которых у неё есть активный исполнитель. */
export const TASK_ACTIVE: readonly string[] = ['LEASED', 'RUNNING', 'VERIFYING'];
