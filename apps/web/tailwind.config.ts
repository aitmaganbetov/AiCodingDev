import type { Config } from 'tailwindcss';

/** Семантический цвет из CSS-переменной (src/styles/tokens.css) с поддержкой прозрачности. */
const token = (name: string) => `rgb(var(--color-${name}) / <alpha-value>)`;
const tone = (name: string) => ({ soft: token(`${name}-soft`), border: token(`${name}-border`), fg: token(`${name}-fg`) });

/**
 * Дизайн-система (P7-01): только семантические токены. Тема (светлая/тёмная) переключается
 * значениями переменных, классы компонентов от темы не зависят.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        canvas: token('canvas'),
        surface: { DEFAULT: token('surface'), muted: token('surface-muted'), raised: token('surface-raised') },
        fg: { DEFAULT: token('fg'), muted: token('fg-muted'), subtle: token('fg-subtle') },
        border: { DEFAULT: token('border'), strong: token('border-strong') },
        input: token('input'),
        accent: {
          DEFAULT: token('accent'),
          hover: token('accent-hover'),
          fg: token('accent-fg'),
          soft: token('accent-soft'),
          'soft-fg': token('accent-soft-fg'),
        },
        link: token('link'),
        focus: token('focus'),
        overlay: token('overlay'),
        neutral: tone('neutral'),
        info: tone('info'),
        success: tone('success'),
        warning: tone('warning'),
        danger: { ...tone('danger'), text: token('danger-text') },
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', '"Helvetica Neue"', 'Arial', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      // Шкала типографики: 12/14/16/18/20/24 (база 16px — не уменьшать для текста тела).
      fontSize: {
        xs: ['0.75rem', { lineHeight: '1rem' }],
        sm: ['0.875rem', { lineHeight: '1.25rem' }],
        base: ['1rem', { lineHeight: '1.5rem' }],
        lg: ['1.125rem', { lineHeight: '1.75rem' }],
        xl: ['1.25rem', { lineHeight: '1.75rem' }],
        '2xl': ['1.5rem', { lineHeight: '2rem' }],
      },
      // Минимальная цель касания 44×44 (WCAG 2.5.5 AAA, используем как ориентир).
      minHeight: { touch: '44px' },
      minWidth: { touch: '44px' },
      spacing: { sidebar: '16rem' },
      boxShadow: { elevated: 'var(--shadow-elevated)' },
      zIndex: { overlay: '40', modal: '50', toast: '60' },
      keyframes: {
        'fade-in': { from: { opacity: '0' }, to: { opacity: '1' } },
      },
      animation: { 'fade-in': 'fade-in 120ms ease-out' },
    },
  },
  plugins: [],
} satisfies Config;
