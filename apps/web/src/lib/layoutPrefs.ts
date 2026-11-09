import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';

/**
 * Раскладка оболочки (F2 редизайна, план §3.1 «Persisted layout»): свёрнутость sidebar, открытость
 * правой контекстной панели (по экрану) и её ширина. Только удобство пользователя: хранится в
 * localStorage этого устройства (не синхронизируется между устройствами), недоступное хранилище —
 * значения действуют до перезагрузки. Все ключи начинаются с `aicd.layout.` (как размеры панелей
 * `Resizable`), поэтому «Сбросить раскладку» (Настройки › Внешний вид) удаляет всё одним проходом.
 */
export const LAYOUT_PREFIX = 'aicd.layout.';
export const SIDEBAR_KEY = `${LAYOUT_PREFIX}shell.sidebar`;
export const CONTEXT_WIDTH_KEY = `${LAYOUT_PREFIX}shell.context.width`;
export const contextOpenKey = (screen: string) => `${LAYOUT_PREFIX}shell.context.open.${screen}`;

/** Ширина правой панели, px (план §5.8: 320 на 1366, 360–400 на 1920). */
export const CONTEXT_WIDTH = { min: 280, max: 560, default: 340, step: 16 } as const;

/** `auto`: rail на 768–1279 px, развёрнут ≥ 1280 px; явный выбор пользователя сохраняется. */
export type SidebarMode = 'auto' | 'expanded' | 'collapsed';

const listeners = new Set<() => void>();
const memory = new Map<string, string>();

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return memory.get(key) ?? null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    if (value === null) memory.delete(key);
    else memory.set(key, value);
  }
  for (const l of listeners) l();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key === null || e.key.startsWith(LAYOUT_PREFIX)) listener();
  };
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
}

function usePref(key: string): string | null {
  return useSyncExternalStore(subscribe, () => read(key), () => null);
}

export function readSidebarMode(): SidebarMode {
  const v = read(SIDEBAR_KEY);
  return v === 'expanded' || v === 'collapsed' ? v : 'auto';
}

export function useSidebarMode(): [SidebarMode, (mode: SidebarMode) => void] {
  const raw = usePref(SIDEBAR_KEY);
  const mode: SidebarMode = raw === 'expanded' || raw === 'collapsed' ? raw : 'auto';
  return [mode, useCallback((m: SidebarMode) => write(SIDEBAR_KEY, m === 'auto' ? null : m), [])];
}

export function clampContextWidth(px: number): number {
  return Math.round(Math.min(CONTEXT_WIDTH.max, Math.max(CONTEXT_WIDTH.min, px)));
}

export function useContextWidth(): [number, (px: number | null) => void] {
  const raw = usePref(CONTEXT_WIDTH_KEY);
  const parsed = raw === null ? Number.NaN : Number(raw);
  const width = Number.isFinite(parsed) ? clampContextWidth(parsed) : CONTEXT_WIDTH.default;
  return [width, useCallback((px: number | null) => write(CONTEXT_WIDTH_KEY, px === null ? null : String(clampContextWidth(px))), [])];
}

/** Открыта ли контекстная панель экрана `screen`; `null` — пользователь ещё не выбирал (решает ширина окна). */
export function useContextOpenPref(screen: string | null): [boolean | null, (open: boolean) => void] {
  const raw = usePref(screen ? contextOpenKey(screen) : `${LAYOUT_PREFIX}shell.context.open.-`);
  const value = raw === 'open' ? true : raw === 'closed' ? false : null;
  return [value, useCallback((open: boolean) => (screen ? write(contextOpenKey(screen), open ? 'open' : 'closed') : undefined), [screen])];
}

/** Удаляет все сохранённые раскладки (оболочка + панели экранов `Resizable`). Возвращает число ключей. */
export function resetLayout(): number {
  let removed = 0;
  try {
    for (let i = window.localStorage.length - 1; i >= 0; i -= 1) {
      const key = window.localStorage.key(i);
      if (key?.startsWith(LAYOUT_PREFIX)) {
        window.localStorage.removeItem(key);
        removed += 1;
      }
    }
  } catch {
    /* хранилище недоступно */
  }
  for (const key of [...memory.keys()]) if (key.startsWith(LAYOUT_PREFIX)) memory.delete(key);
  for (const l of listeners) l();
  return removed;
}

/** Совпадение media query с подпиской (SSR/jsdom без matchMedia — `fallback`). */
export function useMediaQuery(query: string, fallback = false): boolean {
  const get = () => {
    try {
      return window.matchMedia(query).matches;
    } catch {
      return fallback;
    }
  };
  const [matches, setMatches] = useState(get);
  useEffect(() => {
    let mql: MediaQueryList;
    try {
      mql = window.matchMedia(query);
    } catch {
      return undefined;
    }
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [query]);
  return matches;
}

export const MQ_DESKTOP = '(min-width: 768px)';
export const MQ_WIDE = '(min-width: 1280px)';
export const MQ_REDUCED_MOTION = '(prefers-reduced-motion: reduce)';
