import { afterEach, describe, expect, it, vi } from 'vitest';
import { applyTheme, DEFAULT_THEME, readTheme, resolveTheme, setTheme, THEME_STORAGE_KEY } from './theme';

afterEach(() => {
  delete document.documentElement.dataset.theme;
});

describe('theme', () => {
  it('defaults to dark and applies data-theme on <html>', () => {
    expect(DEFAULT_THEME).toBe('dark');
    expect(readTheme()).toBe('dark');
    applyTheme();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('persists light/dark/system and ignores garbage values', () => {
    for (const pref of ['light', 'system', 'dark'] as const) {
      setTheme(pref);
      expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe(pref);
      expect(readTheme()).toBe(pref);
      expect(document.documentElement.dataset.theme).toBe(pref);
    }
    window.localStorage.setItem(THEME_STORAGE_KEY, 'neon');
    expect(readTheme()).toBe('dark');
  });

  it('resolves "system" through prefers-color-scheme', () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q === '(prefers-color-scheme: light)' }));
    expect(resolveTheme('system')).toBe('light');
    vi.stubGlobal('matchMedia', () => ({ matches: false }));
    expect(resolveTheme('system')).toBe('dark');
    expect(resolveTheme('light')).toBe('light');
  });

  it('survives an unavailable localStorage', () => {
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage')!;
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('SecurityError');
      },
    });
    try {
      expect(readTheme()).toBe('dark');
      expect(() => setTheme('light')).not.toThrow();
      expect(document.documentElement.dataset.theme).toBe('light');
    } finally {
      Object.defineProperty(window, 'localStorage', original);
    }
  });
});
