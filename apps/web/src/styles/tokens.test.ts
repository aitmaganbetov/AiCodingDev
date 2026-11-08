import { describe, expect, it } from 'vitest';
import css from './tokens.css?raw';


type Rgb = [number, number, number];

function block(selector: string): Map<string, Rgb> {
  const start = css.indexOf(selector);
  expect(start, selector).toBeGreaterThanOrEqual(0);
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start));
  const out = new Map<string, Rgb>();
  for (const m of body.matchAll(/--color-([\w-]+):\s*(\d+) (\d+) (\d+);/g)) out.set(m[1]!, [Number(m[2]), Number(m[3]), Number(m[4])]);
  return out;
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

const light = block(":root[data-theme='light']");
const darkMedia = block(":root:not([data-theme='light'])");
const darkForced = block(":root[data-theme='dark']");

/** [текст, фон, минимум]: 4.5 — текст (1.4.3), 3 — границы полей и кольцо фокуса (1.4.11). */
const PAIRS: [string, string, number][] = [
  ['fg', 'surface', 4.5],
  ['fg', 'canvas', 4.5],
  ['fg', 'surface-raised', 4.5],
  ['fg-muted', 'surface', 4.5],
  ['fg-muted', 'canvas', 4.5],
  ['fg-muted', 'surface-muted', 4.5],
  ['fg-subtle', 'surface', 4.5],
  ['fg-subtle', 'surface-muted', 4.5],
  ['accent-fg', 'accent', 4.5],
  ['accent-soft-fg', 'accent-soft', 4.5],
  ['link', 'surface', 4.5],
  ['link', 'canvas', 4.5],
  ['danger-text', 'surface', 4.5],
  ['danger-text', 'danger-soft', 4.5],
  ...(['neutral', 'info', 'success', 'warning', 'danger'] as const).map((t): [string, string, number] => [`${t}-fg`, `${t}-soft`, 4.5]),
  ['input', 'surface', 3],
  ['focus', 'surface', 3],
  ['focus', 'canvas', 3],
];

describe('design tokens', () => {
  it('dark theme is identical for prefers-color-scheme and data-theme="dark"; every token is defined in both themes', () => {
    expect([...darkMedia.entries()]).toEqual([...darkForced.entries()]);
    expect([...darkMedia.keys()].sort()).toEqual([...light.keys()].sort());
  });

  it.each([
    ['light', light],
    ['dark', darkMedia],
  ] as const)('%s theme meets WCAG 2.1 AA contrast for every text/background pair', (_name, theme) => {
    for (const [fg, bg, min] of PAIRS) {
      const ratio = contrast(theme.get(fg)!, theme.get(bg)!);
      expect(ratio, `${fg} on ${bg}`).toBeGreaterThanOrEqual(min);
    }
  });
});
