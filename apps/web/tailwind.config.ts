import type { Config } from 'tailwindcss';

/** Семантический цвет из CSS-переменной (src/styles/tokens.css) с поддержкой прозрачности. */
const token = (name: string) => `rgb(var(--color-${name}) / <alpha-value>)`;
const tone = (name: string) => ({
  DEFAULT: token(name),
  soft: token(`${name}-soft`),
  border: token(`${name}-border`),
  fg: token(`${name}-fg`),
});
const verify = (name: string) => ({ DEFAULT: token(`verify-${name}`), soft: token(`verify-${name}-soft`), fg: token(`verify-${name}-fg`) });
const role = (name: string) => ({ DEFAULT: token(`role-${name}`), soft: token(`role-${name}-soft`) });

/**
 * Дизайн-система (F1 редизайна, docs/design/UI_REDESIGN_PLAN.md §5): только семантические токены.
 * Тема (тёмная по умолчанию / светлая / как в системе) переключается значениями переменных,
 * классы компонентов от темы не зависят.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: { DEFAULT: token('canvas'), secondary: token('canvas-secondary') },
        surface: { DEFAULT: token('surface'), muted: token('surface-muted'), raised: token('surface-raised') },
        fg: { DEFAULT: token('fg'), muted: token('fg-muted'), subtle: token('fg-subtle'), disabled: token('fg-disabled') },
        border: { DEFAULT: token('border'), strong: token('border-strong'), active: token('border-active') },
        input: token('input'),
        accent: {
          DEFAULT: token('accent'),
          hover: token('accent-hover'),
          fg: token('accent-fg'),
          soft: token('accent-soft'),
          'soft-fg': token('accent-soft-fg'),
        },
        secondary: { DEFAULT: token('secondary'), text: token('secondary-text'), soft: token('secondary-soft') },
        highlight: { DEFAULT: token('highlight'), text: token('highlight-text'), soft: token('highlight-soft') },
        link: token('link'),
        focus: token('focus'),
        overlay: token('overlay'),
        neutral: tone('neutral'),
        info: tone('info'),
        success: tone('success'),
        warning: tone('warning'),
        danger: { ...tone('danger'), text: token('danger-text') },
        verify: {
          pass: verify('pass'),
          fail: verify('fail'),
          blocked: verify('blocked'),
          'not-run': verify('not-run'),
          insufficient: verify('insufficient'),
        },
        role: { discover: role('discover'), build: role('build'), verify: role('verify'), sustain: role('sustain'), system: role('system') },
      },
      fontFamily: {
        sans: ['"Inter Variable"', 'ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'Arial', 'sans-serif'],
        mono: ['"JetBrains Mono Variable"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      // Шкала типографики (§5.3): caption 12 · body-dense 14 · body 16 · h3 16 · h2 20 · h1 24 · display 30;
      // code 13/20, code-sm 12/18; metric 28–36 (tabular-nums). Текст тела — не меньше 14 px.
      fontSize: {
        xs: ['0.75rem', { lineHeight: '1rem' }],
        sm: ['0.875rem', { lineHeight: '1.25rem' }],
        base: ['1rem', { lineHeight: '1.5rem' }],
        lg: ['1.125rem', { lineHeight: '1.75rem' }],
        xl: ['1.25rem', { lineHeight: '1.75rem' }],
        '2xl': ['1.5rem', { lineHeight: '2rem' }],
        display: ['1.875rem', { lineHeight: '2.25rem', fontWeight: '600' }],
        metric: ['1.75rem', { lineHeight: '1.1', fontWeight: '600' }],
        code: ['0.8125rem', { lineHeight: '1.25rem' }],
        'code-sm': ['0.75rem', { lineHeight: '1.125rem' }],
      },
      // Радиусы (§5.4): md 6 — поля и кнопки; lg 10 — карточки, панели; xl 14 — плитки, диалоги.
      borderRadius: {
        sm: '4px',
        DEFAULT: 'var(--radius-sm)',
        md: 'var(--radius-sm)',
        lg: 'var(--radius-md)',
        xl: 'var(--radius-lg)',
      },
      // Минимальная цель касания 44×44 (основные контролы); 24×24 — минимум WCAG 2.5.8 для плотных таблиц.
      minHeight: { touch: '44px', target: '24px' },
      minWidth: { touch: '44px', target: '24px' },
      spacing: { sidebar: '16rem' },
      boxShadow: {
        e1: 'var(--shadow-e1)',
        e2: 'var(--shadow-e2)',
        e3: 'var(--shadow-e3)',
        elevated: 'var(--shadow-e2)',
        glow: 'var(--shadow-glow)',
      },
      backgroundImage: { 'gradient-brand': 'var(--gradient-brand)', 'gradient-brand-subtle': 'var(--gradient-brand-subtle)' },
      transitionDuration: { instant: 'var(--dur-instant)', base: 'var(--dur-base)', slow: 'var(--dur-slow)' },
      transitionTimingFunction: { standard: 'var(--ease-standard)', exit: 'var(--ease-exit)' },
      zIndex: { overlay: '40', modal: '50', toast: '60' },
      screens: { '3xl': '1920px' },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
      },
      animation: { 'fade-in': 'fade-in var(--dur-instant) var(--ease-standard)' },
    },
  },
  plugins: [],
} satisfies Config;
