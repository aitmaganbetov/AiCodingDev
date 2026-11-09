import type { Provider, ProviderInstallation } from '../api/providers';
import type { Tone } from '../components/ui/Badge';
import type { IconName } from '../components/ui/Icon';

/**
 * Статус CLI-провайдера для чипа верхней панели (план §3.1, §5.2): только из `listProviders`
 * (auth_status, availability, compatibility, enabled). Никаких квот/лимитов подписки — CLI их не
 * сообщают. Нет данных ≠ «готов»: без установок или без проверки — «неизвестно».
 */
export type ProviderChipState =
  | 'ready'
  | 'auth_required'
  | 'unavailable'
  | 'rate_limited'
  | 'degraded'
  | 'error'
  | 'unsupported'
  | 'disabled'
  | 'not_installed'
  | 'unknown';

export const CHIP_STATE_TONE: Record<ProviderChipState, Tone> = {
  ready: 'success',
  auth_required: 'warning',
  unavailable: 'danger',
  rate_limited: 'warning',
  degraded: 'warning',
  error: 'danger',
  unsupported: 'danger',
  disabled: 'neutral',
  not_installed: 'neutral',
  unknown: 'neutral',
};

export const CHIP_STATE_ICON: Record<ProviderChipState, IconName> = {
  ready: 'statusSuccess',
  auth_required: 'verifyBlocked',
  unavailable: 'statusFailed',
  rate_limited: 'statusPaused',
  degraded: 'alert',
  error: 'statusFailed',
  unsupported: 'statusFailed',
  disabled: 'statusNeutral',
  not_installed: 'verifyNotRun',
  unknown: 'help',
};

/** Состояние одной установки (хоста). Порядок важности: совместимость → вход → доступность. */
export function installationState(i: ProviderInstallation): ProviderChipState {
  if (i.compatibility === 'UNSUPPORTED') return 'unsupported';
  if (i.auth_status === 'AUTH_REQUIRED' || i.auth_status === 'EXPIRED') return 'auth_required';
  if (i.auth_status === 'ERROR') return 'error';
  if (i.availability === 'UNAVAILABLE') return 'unavailable';
  if (i.availability === 'RATE_LIMITED') return 'rate_limited';
  if (i.availability === 'DEGRADED') return 'degraded';
  if (i.auth_status === 'UNKNOWN' || !i.checked_at) return 'unknown';
  return 'ready';
}

/** Приоритет при агрегации по хостам: хотя бы один готовый хост — провайдер готов (маршрутизация выберет его). */
const AGGREGATE_ORDER: readonly ProviderChipState[] = [
  'ready',
  'degraded',
  'rate_limited',
  'auth_required',
  'unknown',
  'error',
  'unavailable',
  'unsupported',
];

export interface ProviderChipModel {
  state: ProviderChipState;
  /** Установки с версией/хостом/временем проверки — для подсказки. */
  installations: ProviderInstallation[];
  /** Самая свежая проверка среди установок (ISO) или null. */
  checkedAt: string | null;
}

export function providerChipModel(provider: Provider | undefined): ProviderChipModel {
  if (!provider) return { state: 'not_installed', installations: [], checkedAt: null };
  const installations = provider.installations;
  const checkedAt =
    installations
      .map((i) => i.checked_at ?? null)
      .filter((v): v is string => Boolean(v))
      .sort()
      .at(-1) ?? null;
  if (!provider.enabled_globally || !provider.workspace_settings.enabled) return { state: 'disabled', installations, checkedAt };
  if (installations.length === 0) return { state: 'not_installed', installations, checkedAt };
  const states = new Set(installations.map(installationState));
  const state = AGGREGATE_ORDER.find((s) => states.has(s)) ?? 'unknown';
  return { state, installations, checkedAt };
}
