import { useCallback, useSyncExternalStore } from 'react';

/**
 * Тема оформления. По умолчанию — тёмная (палитра владельца, F1 редизайна); `system` следует
 * prefers-color-scheme. Выбор сохраняется в localStorage (недоступное хранилище — тема действует
 * до перезагрузки) и синхронизируется между вкладками (событие storage).
 */
export type ThemePreference = 'system' | 'light' | 'dark';
export const THEMES: readonly ThemePreference[] = ['dark', 'light', 'system'];
export const DEFAULT_THEME: ThemePreference = 'dark';

export const THEME_STORAGE_KEY = 'aicd.theme';
const listeners = new Set<() => void>();

function isTheme(v: unknown): v is ThemePreference {
  return v === 'light' || v === 'dark' || v === 'system';
}

export function readTheme(): ThemePreference {
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(v) ? v : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** Фактическая тема с учётом `system` (для компонентов, которым нужен вариант логотипа и т. п.). */
export function resolveTheme(pref: ThemePreference = readTheme()): 'light' | 'dark' {
  if (pref !== 'system') return pref;
  try {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  } catch {
    return 'dark';
  }
}

/** Применяет тему к <html data-theme> (токены — src/styles/tokens.css). */
export function applyTheme(pref: ThemePreference = readTheme()): void {
  if (typeof document === 'undefined') return;
  document.documentElement.dataset.theme = pref;
}

export function setTheme(pref: ThemePreference): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    /* хранилище недоступно — тема действует до перезагрузки */
  }
  applyTheme(pref);
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key !== THEME_STORAGE_KEY) return;
    applyTheme();
    listener();
  };
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
}

export function useTheme(): [ThemePreference, (pref: ThemePreference) => void] {
  const theme = useSyncExternalStore(subscribe, readTheme, () => DEFAULT_THEME);
  return [theme, useCallback((pref: ThemePreference) => setTheme(pref), [])];
}
