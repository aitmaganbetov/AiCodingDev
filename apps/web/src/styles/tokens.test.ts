import { describe, expect, it } from 'vitest';
import css from './tokens.css?raw';

/**
 * WCAG 2.2 AA для дизайн-токенов (docs/design/UI_REDESIGN_PLAN.md §4.5, §5.9): каждая пара
 * «текст/фон» ≥ 4,5:1 (1.4.3), границы полей, кольцо фокуса, иконки и заливки статусов ≥ 3:1 (1.4.11).
 * Проверяются обе темы; блок «как в системе» обязан совпадать со светлой темой.
 */

type Rgb = [number, number, number];

function block(selector: string): { colors: Map<string, Rgb>; vars: Map<string, string> } {
  const start = css.indexOf(selector);
  expect(start, selector).toBeGreaterThanOrEqual(0);
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start));
  const colors = new Map<string, Rgb>();
  for (const m of body.matchAll(/--color-([\w-]+):\s*(\d+) (\d+) (\d+);/g)) colors.set(m[1]!, [Number(m[2]), Number(m[3]), Number(m[4])]);
  const vars = new Map<string, string>();
  for (const m of body.matchAll(/--([\w-]+):\s*([^;]+);/g)) vars.set(m[1]!, m[2]!.trim());
  return { colors, vars };
}

function luminance([r, g, b]: Rgb): number {
  const ch = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** Цвет полупрозрачного слоя `top` с альфой `alpha` поверх непрозрачного `bottom`. */
function over(top: Rgb, alpha: number, bottom: Rgb): Rgb {
  return top.map((c, i) => Math.round(c * alpha + bottom[i]! * (1 - alpha))) as Rgb;
}

const dark = block(":root[data-theme='dark']");
const light = block(":root[data-theme='light']");
const system = block(":root[data-theme='system']");

const SURFACES = ['canvas', 'canvas-secondary', 'surface', 'surface-muted', 'surface-raised'] as const;
const TONES = ['neutral', 'info', 'success', 'warning', 'danger'] as const;
const VERIFY = ['pass', 'fail', 'blocked', 'not-run', 'insufficient'] as const;
const ROLES = ['discover', 'build', 'verify', 'sustain', 'system'] as const;

/** [передний план, фон, минимум]. */
const PAIRS: [string, string, number][] = [
  // Основной, вторичный и вспомогательный текст — на всех поверхностях.
  ...SURFACES.flatMap((bg): [string, string, number][] => [
    ['fg', bg, 4.5],
    ['fg-muted', bg, 4.5],
    ['fg-subtle', bg, 4.5],
    ['link', bg, 4.5],
    ['danger-text', bg, 4.5],
    ['secondary-text', bg, 4.5],
    ['highlight-text', bg, 4.5],
    // Не-текстовые элементы (1.4.11): граница поля, кольцо фокуса, выделение, CTA-заливка.
    ['input', bg, 3],
    ['focus', bg, 3],
    ['border-active', bg, 3],
    ['accent', bg, 3],
  ]),
  ['accent-fg', 'accent', 4.5],
  ['accent-fg', 'accent-hover', 4.5],
  ['accent-soft-fg', 'accent-soft', 4.5],
  ['secondary-text', 'secondary-soft', 4.5],
  ['highlight-text', 'highlight-soft', 4.5],
  ['danger-text', 'danger-soft', 4.5],
  // Тона статусов: текст на soft-фоне; заливка/иконка тона ≥ 3 на surface и на soft.
  ...TONES.flatMap((t): [string, string, number][] => [
    [`${t}-fg`, `${t}-soft`, 4.5],
    [t, 'surface', 3],
    [t, `${t}-soft`, 3],
  ]),
  // 5 состояний верификации: метка ≥ 4,5 на своём фоне, иконка/контур ≥ 3.
  ...VERIFY.flatMap((v): [string, string, number][] => [
    [`verify-${v}-fg`, `verify-${v}-soft`, 4.5],
    [`verify-${v}-fg`, 'surface', 4.5],
    [`verify-${v}`, `verify-${v}-soft`, 3],
    [`verify-${v}`, 'surface', 3],
  ]),
  // Семейства ролей агентов: глиф ≥ 3 на своём фоне; код/название (текст) ≥ 4,5 на soft и surface.
  ...ROLES.flatMap((r): [string, string, number][] => [
    [`role-${r}`, `role-${r}-soft`, 4.5],
    [`role-${r}`, 'surface', 4.5],
    ['fg', `role-${r}-soft`, 4.5],
  ]),
];

describe('design tokens', () => {
  it('every color token is defined in both themes; "system" equals the light theme', () => {
    expect([...dark.colors.keys()].sort()).toEqual([...light.colors.keys()].sort());
    expect([...system.colors.entries()]).toEqual([...light.colors.entries()]);
    expect([...system.vars.entries()]).toEqual([...light.vars.entries()]);
    expect(dark.colors.size).toBeGreaterThan(70);
  });

  it('dark is the default theme (bare :root) and owner palette anchors are kept', () => {
    expect(css).toMatch(/:root,\s*:root\[data-theme='dark'\]\s*\{/);
    expect(dark.colors.get('canvas')).toEqual([8, 11, 20]); // #080B14
    expect(dark.colors.get('accent')).toEqual([0, 207, 255]); // #00CFFF
    expect(dark.colors.get('accent-fg')).toEqual([8, 11, 20]); // тёмный текст на cyan
  });

  it.each([
    ['light', light.colors],
    ['dark', dark.colors],
  ] as const)('%s theme meets WCAG 2.2 AA for every foreground/background pair', (_name, theme) => {
    const failures: string[] = [];
    for (const [fg, bg, min] of PAIRS) {
      const a = theme.get(fg);
      const b = theme.get(bg);
      expect(a, `token ${fg}`).toBeDefined();
      expect(b, `token ${bg}`).toBeDefined();
      const ratio = contrast(a!, b!);
      if (ratio < min) failures.push(`${fg} on ${bg}: ${ratio.toFixed(2)} < ${min}`);
    }
    expect(failures).toEqual([]);
  });

  it.each([
    ['light', light, 0.92],
    ['dark', dark, 0.88],
  ] as const)('%s glass: alpha ≥ %s; text on glass over flat canvas and over the brand cyan stays AA', (_name, theme, minAlpha) => {
    const alpha = Number(theme.vars.get('glass-alpha'));
    expect(alpha).toBeGreaterThanOrEqual(minAlpha);
    const glass = theme.colors.get('glass')!;
    for (const under of ['canvas', 'canvas-secondary', 'surface', 'accent'] as const) {
      const bg = over(glass, alpha, theme.colors.get(under)!);
      for (const fg of ['fg', 'fg-muted'] as const) {
        expect(contrast(theme.colors.get(fg)!, bg), `${fg} on glass over ${under}`).toBeGreaterThanOrEqual(4.5);
      }
      expect(contrast(theme.colors.get('focus')!, bg), `focus on glass over ${under}`).toBeGreaterThanOrEqual(3);
    }
  });

  it('gradients are decorative tokens; motion stays within 120–250 ms and collapses under reduced motion', () => {
    for (const theme of [dark, light]) expect(theme.vars.get('gradient-brand')).toMatch(/^linear-gradient\(/);
    const root = block(':root {');
    const ms = ['dur-instant', 'dur-base', 'dur-slow'].map((k) => Number(root.vars.get(k)?.replace('ms', '')));
    expect(ms).toEqual([120, 180, 250]);
    const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'));
    expect(reduced).toMatch(/--dur-instant:\s*0ms/);
    expect(reduced).toMatch(/--dur-slow:\s*0ms/);
  });
});
