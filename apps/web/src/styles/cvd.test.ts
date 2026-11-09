import { describe, expect, it } from 'vitest';
import { TONE_ICON, type Tone } from '../components/ui/Badge';
import { ICONS } from '../components/ui/Icon';
import { VERIFICATION_STYLE } from '../components/ui/VerificationBadge';
import { AGENT_ROLE_META, type AgentFamily } from '../lib/agentRoles';
import { VERIFICATION_STATES } from '../lib/verification';
import css from './tokens.css?raw';

/**
 * Цветовое зрение (WCAG 1.4.1 «Использование цвета», план §5.2, §5.7): палитры ролей, тонов статусов и
 * 5 состояний верификации симулируются для протанопии, дейтеранопии и тританопии (матрицы Machado,
 * Oliveira, Fernandes 2009, тяжесть 1,0, в линейном sRGB). Цвет — вторичный признак: тест фиксирует,
 * какие пары после симуляции почти неразличимы (ΔE*ab < 12), и требует, чтобы каждую такую пару
 * различали глиф (иконка), код/метка текстом или паттерн. Отдельно — элементы остаются ≥ 3:1 к
 * поверхности (1.4.11) и после симуляции.
 */

type Rgb = [number, number, number];
type Matrix = [Rgb, Rgb, Rgb];

const CVD: Record<'protanopia' | 'deuteranopia' | 'tritanopia', Matrix> = {
  protanopia: [
    [0.152286, 1.052583, -0.204868],
    [0.114503, 0.786281, 0.099216],
    [-0.003882, -0.048116, 1.051998],
  ],
  deuteranopia: [
    [0.367322, 0.860646, -0.227968],
    [0.280085, 0.672501, 0.047413],
    [-0.01182, 0.04294, 0.968881],
  ],
  tritanopia: [
    [1.255528, -0.076749, -0.178779],
    [-0.078411, 0.930809, 0.147602],
    [0.004733, 0.691367, 0.3039],
  ],
};

function theme(selector: string): Map<string, Rgb> {
  const start = css.indexOf(selector);
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start));
  const colors = new Map<string, Rgb>();
  for (const m of body.matchAll(/--color-([\w-]+):\s*(\d+) (\d+) (\d+);/g)) colors.set(m[1]!, [Number(m[2]), Number(m[3]), Number(m[4])]);
  return colors;
}

const toLinear = (v: number) => {
  const s = v / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
};
const toSrgb = (v: number) => {
  const c = Math.min(1, Math.max(0, v));
  return Math.round((c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055) * 255);
};

function simulate(rgb: Rgb, m: Matrix): Rgb {
  const lin = rgb.map(toLinear) as Rgb;
  return m.map((row) => toSrgb(row[0] * lin[0] + row[1] * lin[1] + row[2] * lin[2])) as Rgb;
}

function lab(rgb: Rgb): Rgb {
  const [r, g, b] = rgb.map(toLinear) as Rgb;
  const xyz = [
    (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047,
    0.2126 * r + 0.7152 * g + 0.0722 * b,
    (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883,
  ].map((t) => (t > 216 / 24389 ? Math.cbrt(t) : (24389 / 27 * t + 16) / 116)) as Rgb;
  return [116 * xyz[1] - 16, 500 * (xyz[0] - xyz[1]), 200 * (xyz[1] - xyz[2])];
}

function deltaE(a: Rgb, b: Rgb): number {
  const [l1, a1, b1] = lab(a);
  const [l2, a2, b2] = lab(b);
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2);
}

function contrast(a: Rgb, b: Rgb): number {
  const lum = (c: Rgb) => 0.2126 * toLinear(c[0]) + 0.7152 * toLinear(c[1]) + 0.0722 * toLinear(c[2]);
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** Пары с ΔE < порога после симуляции: [тема, вид CVD, a, b, ΔE]. */
function confusable(colors: Map<string, Rgb>, names: readonly string[], token: (n: string) => string, themeName: string) {
  const out: string[] = [];
  for (const [kind, m] of Object.entries(CVD)) {
    for (let i = 0; i < names.length; i++) {
      for (let j = i + 1; j < names.length; j++) {
        const a = simulate(colors.get(token(names[i]!))!, m);
        const b = simulate(colors.get(token(names[j]!))!, m);
        const d = deltaE(a, b);
        if (d < CONFUSABLE_DE) out.push(`${themeName}/${kind}: ${names[i]} ~ ${names[j]} (ΔE ${d.toFixed(1)})`);
      }
    }
  }
  return out;
}

const CONFUSABLE_DE = 12;
const THEMES = [
  ['dark', theme(":root[data-theme='dark']")],
  ['light', theme(":root[data-theme='light']")],
] as const;
const FAMILIES: readonly AgentFamily[] = ['discover', 'build', 'verify', 'sustain'];
const TONES: readonly Tone[] = ['neutral', 'info', 'success', 'warning', 'danger'];
const VERIFY = ['pass', 'fail', 'blocked', 'not-run', 'insufficient'] as const;

describe('color-vision deficiency (1.4.1): color is never the only cue', () => {
  it('simulation matrices behave (white/black preserved, red and green collapse for deuteranopia)', () => {
    for (const m of Object.values(CVD)) {
      expect(simulate([255, 255, 255], m).every((c) => c >= 250)).toBe(true);
      expect(simulate([0, 0, 0], m)).toEqual([0, 0, 0]);
    }
    expect(deltaE(simulate([0, 160, 0], CVD.deuteranopia), simulate([160, 120, 0], CVD.deuteranopia))).toBeLessThan(
      deltaE([0, 160, 0], [160, 120, 0]),
    );
  });

  it('12 agent roles: unique glyph and unique text code — role families that collapse under CVD stay distinguishable', () => {
    const roles = Object.entries(AGENT_ROLE_META);
    expect(new Set(roles.map(([, m]) => ICONS[m.icon])).size).toBe(roles.length);
    expect(new Set(roles.map(([, m]) => m.code)).size).toBe(roles.length);
    const collapsed = THEMES.flatMap(([name, colors]) => confusable(colors, FAMILIES, (f) => `role-${f}`, name));
    // Семейства — вторичный признак: при совпадении цвета роль различима глифом и кодом (проверено выше).
    // Сами пары фиксируются, чтобы изменение палитры было видно в ревью.
    expect(collapsed).toMatchInlineSnapshot(`
      [
        "dark/protanopia: discover ~ verify (ΔE 3.0)",
        "dark/protanopia: discover ~ sustain (ΔE 3.0)",
        "dark/protanopia: verify ~ sustain (ΔE 6.0)",
        "dark/deuteranopia: discover ~ verify (ΔE 3.6)",
        "dark/deuteranopia: discover ~ sustain (ΔE 4.8)",
        "dark/deuteranopia: verify ~ sustain (ΔE 8.5)",
        "light/protanopia: discover ~ verify (ΔE 7.5)",
        "light/deuteranopia: verify ~ sustain (ΔE 1.4)",
        "light/tritanopia: build ~ sustain (ΔE 11.5)",
      ]
    `);
  });

  it('status tones and 5 verification states: unique icon per tone/state (+ pattern), so collapsed colors stay distinguishable', () => {
    expect(new Set(TONES.map((t) => ICONS[TONE_ICON[t]])).size).toBe(TONES.length);
    const states = VERIFICATION_STATES.map((s) => VERIFICATION_STYLE[s]);
    expect(new Set(states.map((s) => ICONS[s.icon])).size).toBe(VERIFICATION_STATES.length);
    // Паттерны: штриховка BLOCKED, пунктир NOT_RUN — отличаются от сплошных PASS/FAIL даже в оттенках серого.
    expect(VERIFICATION_STYLE.BLOCKED.className).toMatch(/pattern-hatch/);
    expect(VERIFICATION_STYLE.NOT_RUN.className).toMatch(/border-dashed/);
    const collapsed = THEMES.flatMap(([name, colors]) => [
      ...confusable(colors, TONES, (t) => t, name),
      ...confusable(colors, VERIFY, (v) => `verify-${v}`, name),
    ]);
    expect(collapsed).toMatchInlineSnapshot(`
      [
        "dark/deuteranopia: success ~ danger (ΔE 10.5)",
        "dark/protanopia: blocked ~ insufficient (ΔE 0.0)",
        "dark/deuteranopia: pass ~ fail (ΔE 10.5)",
        "dark/deuteranopia: blocked ~ insufficient (ΔE 0.0)",
        "dark/tritanopia: blocked ~ insufficient (ΔE 0.0)",
        "light/protanopia: blocked ~ insufficient (ΔE 0.0)",
        "light/deuteranopia: blocked ~ insufficient (ΔE 0.0)",
        "light/tritanopia: blocked ~ insufficient (ΔE 0.0)",
      ]
    `);
  });

  it('tone, verification and role colors keep ≥ 3:1 against surfaces (1.4.11) under every simulation', () => {
    const failures: string[] = [];
    for (const [name, colors] of THEMES) {
      for (const [kind, m] of Object.entries(CVD)) {
        const tokens = [...TONES, ...VERIFY.map((v) => `verify-${v}`), ...FAMILIES.map((f) => `role-${f}`), 'focus', 'accent'];
        for (const token of tokens) {
          for (const bg of ['surface', 'canvas'] as const) {
            const ratio = contrast(simulate(colors.get(token)!, m), simulate(colors.get(bg)!, m));
            if (ratio < 3) failures.push(`${name}/${kind}: ${token} on ${bg} ${ratio.toFixed(2)}`);
          }
        }
      }
    }
    expect(failures).toEqual([]);
  });
});
