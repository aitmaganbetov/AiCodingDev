import { useEffect, useSyncExternalStore } from 'react';

/**
 * Глобальные сочетания клавиш. Односимвольные сочетания можно отключить (WCAG 2.1.4):
 * настройка хранится в localStorage и меняется в диалоге «Сочетания клавиш».
 */
const KEY = 'aicd.shortcuts';
const listeners = new Set<() => void>();

export function shortcutsEnabled(): boolean {
  try {
    return window.localStorage.getItem(KEY) !== 'off';
  } catch {
    return true;
  }
}

export function setShortcutsEnabled(enabled: boolean): void {
  try {
    if (enabled) window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, 'off');
  } catch {
    /* ignore */
  }
  for (const l of listeners) l();
}

export function useShortcutsEnabled(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    shortcutsEnabled,
    () => true,
  );
}

/** Фокус в поле ввода/редакторе — клавиши принадлежат ему. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (target as HTMLInputElement).type;
    return !['checkbox', 'radio', 'button', 'submit', 'reset'].includes(type);
  }
  return Boolean(target.closest('[role="dialog"], [role="menu"], [role="listbox"], [role="combobox"]'));
}

export interface ShortcutHandlers {
  /** `?` — справка. */
  onHelp: () => void;
  /** `g`, затем буква → раздел. */
  sequences: Record<string, () => void>;
}

/** Последовательности «g, затем буква» (окно 1,5 с) и `?`. */
export function useGlobalShortcuts({ onHelp, sequences }: ShortcutHandlers, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return undefined;
    let pendingG = 0;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || isTypingTarget(e.target)) return;
      if (e.key === '?') {
        e.preventDefault();
        pendingG = 0;
        onHelp();
        return;
      }
      const key = e.key.toLowerCase();
      if (pendingG && Date.now() - pendingG < 1500) {
        pendingG = 0;
        const action = sequences[key];
        if (action) {
          e.preventDefault();
          action();
        }
        return;
      }
      pendingG = key === 'g' && !e.shiftKey ? Date.now() : 0;
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [enabled, onHelp, sequences]);
}
