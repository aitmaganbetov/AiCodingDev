import { useCallback, useSyncExternalStore } from 'react';

/** Тема оформления: system — по prefers-color-scheme (по умолчанию). */
export type ThemePreference = 'system' | 'light' | 'dark';
export const THEMES: readonly ThemePreference[] = ['system', 'light', 'dark'];

const KEY = 'aicd.theme';
const listeners = new Set<() => void>();

export function readTheme(): ThemePreference {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === 'light' || v === 'dark' ? v : 'system';
  } catch {
    return 'system';
  }
}

/** Применяет тему к <html data-theme> (токены — src/styles/tokens.css). */
export function applyTheme(pref: ThemePreference = readTheme()): void {
  if (typeof document === 'undefined') return;
  if (pref === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = pref;
}

export function setTheme(pref: ThemePreference): void {
  try {
    if (pref === 'system') window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, pref);
  } catch {
    /* хранилище недоступно — тема действует до перезагрузки */
  }
  applyTheme(pref);
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTheme(): [ThemePreference, (pref: ThemePreference) => void] {
  const theme = useSyncExternalStore(subscribe, readTheme, () => 'system' as const);
  return [theme, useCallback((pref: ThemePreference) => setTheme(pref), [])];
}
