import { useId } from 'react';
import { cx } from './cx';

/**
 * Знак AiCodingDev (ВРЕМЕННЫЙ, решение D-7): векторная перерисовка по растровому
 * `Logo AiCodingDev.png` — буква «A» с `</>`, полушестерня и орбита. Будет заменён исходником
 * дизайнера, когда он появится. PNG (1,5 МБ) в бандл не входит.
 *
 * `variant="auto"` берёт цвета из токенов темы (accent → border-active → highlight), поэтому
 * знак одинаково читается на тёмной и светлой теме; `dark`/`light` — фиксированные палитры
 * (как в public/logo-mark-*.svg). В forced-colors — монохромный (CanvasText).
 * Декоративен по умолчанию; с `title` — img с доступным именем.
 */
const PALETTE = {
  dark: ['#00CFFF', '#4969FF', '#B64DFF', '#7FE6FF'],
  light: ['#006E8C', '#3A57E8', '#8A2BD0', '#005A73'],
  auto: ['rgb(var(--color-accent))', 'rgb(var(--color-border-active))', 'rgb(var(--color-highlight))', 'rgb(var(--color-accent-soft-fg))'],
} as const;

export function LogoMark({ variant = 'auto', title, className }: { variant?: 'auto' | 'dark' | 'light'; title?: string; className?: string }) {
  const uid = useId().replace(/:/g, '');
  const [c1, c2, c3, code] = PALETTE[variant];
  const grad = `lg-${uid}`;
  const clip = `gc-${uid}`;
  return (
    <svg
      viewBox="0 0 64 64"
      className={cx('logo-mark shrink-0', className ?? 'h-8 w-8')}
      role={title ? 'img' : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
      focusable="false"
    >
      <defs>
        <linearGradient id={grad} x1="6" y1="10" x2="58" y2="54" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: c1 }} />
          <stop offset="0.55" style={{ stopColor: c2 }} />
          <stop offset="1" style={{ stopColor: c3 }} />
        </linearGradient>
        <clipPath id={clip}>
          <rect x="44" y="0" width="20" height="40" />
        </clipPath>
      </defs>
      {/* Полушестерня справа за буквой. */}
      <g clipPath={`url(#${clip})`} className="logo-stroke" stroke={`url(#${grad})`} fill="none">
        <circle cx="44" cy="20" r="11" strokeWidth="5" strokeDasharray="4.3 4.3" />
        <circle cx="44" cy="20" r="7" strokeWidth="3" />
      </g>
      {/* Орбита: дуга сверху-слева и «росчерк» снизу, пересекающий букву. */}
      <path className="logo-stroke" d="M9 33 A24 24 0 0 1 36 8.5" fill="none" stroke={`url(#${grad})`} strokeWidth="2" strokeLinecap="round" />
      {/* Буква «A» без перекладины. */}
      <path className="logo-fill" d="M7 57 L27 7 H37 L57 57 H46.5 L32 20.5 L17.5 57 Z" fill={`url(#${grad})`} />
      <path
        className="logo-stroke"
        d="M5 47 C 16 58, 44 56, 61 38"
        fill="none"
        stroke={`url(#${grad})`}
        strokeWidth="3"
        strokeLinecap="round"
      />
      {/* </> внутри буквы. */}
      <path
        className="logo-stroke"
        d="M28.5 37 l-3 3 3 3 M35.5 37 l3 3 -3 3 M33.2 35.2 l-2.4 9.6"
        fill="none"
        stroke={code}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Знак + словесный знак (набран Inter, не растром). «Dev» — цветом ссылки (контраст ≥ 4,5:1). */
export function Logo({ className, markClassName }: { className?: string; markClassName?: string }) {
  return (
    <span className={cx('inline-flex items-center gap-2 font-semibold tracking-tight text-fg', className)}>
      <LogoMark className={markClassName ?? 'h-7 w-7'} />
      <span>
        AiCoding<span className="text-link">Dev</span>
      </span>
    </span>
  );
}
