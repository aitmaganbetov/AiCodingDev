import { useId, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { cx } from './cx';

/**
 * Доступные диаграммы без библиотек (F3, D-10: без Recharts): SVG/CSS. Графика — дополнение к
 * тексту: у SVG есть текстовая альтернатива (role="img" + aria-label со сводкой), а значения
 * доступны таблицей (`<details>`) или подписями. Цвет — не единственный носитель смысла:
 * у сегментов есть подписи в легенде. Анимаций нет.
 */

export interface SeriesPoint {
  /** Подпись точки (дата и т. п.) — для таблицы. */
  label: string;
  /** null — нет данных в точке (разрыв линии, не 0). */
  value: number | null;
}

/** Координаты полилиний спарклайна; null-точки разрывают линию. Чистая функция (unit-тест). */
export function sparklineSegments(values: readonly (number | null)[], width: number, height: number, pad = 2): [number, number][][] {
  const present = values.filter((v): v is number => v !== null && Number.isFinite(v));
  if (present.length === 0) return [];
  const min = Math.min(...present, 0);
  const max = Math.max(...present);
  const span = max - min || 1;
  const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0;
  const segments: [number, number][][] = [];
  let current: [number, number][] = [];
  values.forEach((v, i) => {
    if (v === null || !Number.isFinite(v)) {
      if (current.length > 0) segments.push(current);
      current = [];
      return;
    }
    const x = values.length > 1 ? pad + i * step : width / 2;
    const y = height - pad - ((v - min) / span) * (height - pad * 2);
    current.push([Math.round(x * 100) / 100, Math.round(y * 100) / 100]);
  });
  if (current.length > 0) segments.push(current);
  return segments;
}

/**
 * Спарклайн ряда (например, завершённые runs по дням). `summary` — текстовая альтернатива
 * (обязательна: что показано, за какое окно, последнее значение). `table` — показать значения
 * таблицей под графиком.
 */
export function Sparkline({
  points,
  summary,
  valueHeader,
  labelHeader,
  table = true,
  className,
  testId,
}: {
  points: readonly SeriesPoint[];
  summary: string;
  valueHeader?: string;
  labelHeader?: string;
  table?: boolean;
  className?: string;
  testId?: string;
}) {
  const { t } = useTranslation();
  const width = 120;
  const height = 32;
  const segments = sparklineSegments(
    points.map((p) => p.value),
    width,
    height,
  );
  return (
    <figure className={cx('flex min-w-0 flex-col gap-1', className)} data-testid={testId}>
      <svg role="img" aria-label={summary} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="h-8 w-full" data-chart="sparkline">
        <line x1={0} y1={height - 0.5} x2={width} y2={height - 0.5} className="stroke-border" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        {segments.map((seg, i) =>
          seg.length === 1 ? (
            <circle key={i} cx={seg[0]![0]} cy={seg[0]![1]} r={1.5} className="fill-accent" />
          ) : (
            <polyline
              key={i}
              points={seg.map(([x, y]) => `${x},${y}`).join(' ')}
              fill="none"
              className="stroke-accent"
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          ),
        )}
      </svg>
      {table && points.length > 0 ? (
        <details className="text-xs">
          <summary className="min-h-target cursor-pointer py-1 text-link underline">{t('ds.chart.showTable')}</summary>
          {/* Прокручиваемая таблица доступна с клавиатуры (axe scrollable-region-focusable). */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
          <div className="mt-1 max-h-40 overflow-auto rounded-md border border-border" tabIndex={0} role="region" aria-label={summary}>
            <table className="w-full text-left text-xs">
              <caption className="sr-only">{summary}</caption>
              <thead>
                <tr className="bg-surface-muted">
                  <th scope="col" className="px-2 py-1 font-semibold">
                    {labelHeader ?? t('ds.chart.label')}
                  </th>
                  <th scope="col" className="px-2 py-1 text-right font-semibold">
                    {valueHeader ?? t('ds.chart.value')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {points.map((p) => (
                  <tr key={p.label} className="border-t border-border">
                    <th scope="row" className="px-2 py-1 font-normal">
                      {p.label}
                    </th>
                    <td className="px-2 py-1 text-right tabular-nums">{p.value === null ? t('ds.metric.noData') : p.value.toLocaleString('ru-RU')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : null}
    </figure>
  );
}

/** Ширина полосы 0..100 по доле 0..1 (null — пустая полоса). */
export function meterPercent(ratio: number | null | undefined): number {
  if (ratio === null || ratio === undefined || Number.isNaN(ratio)) return 0;
  return Math.min(100, Math.max(0, ratio * 100));
}

const METER_TONE = {
  accent: 'bg-accent',
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  info: 'bg-info',
} as const;

/**
 * Горизонтальная полоса доли с видимым текстом значения (`valueText`) — полоса декоративна
 * (aria-hidden), смысл передаёт текст. null — «нет данных», полоса пустая.
 */
export function Meter({
  label,
  ratio,
  valueText,
  tone = 'accent',
  testId,
}: {
  label: ReactNode;
  ratio: number | null;
  valueText: ReactNode;
  tone?: keyof typeof METER_TONE;
  testId?: string;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1" data-testid={testId} data-ratio={ratio ?? 'none'}>
      <div className="flex flex-wrap justify-between gap-x-2 text-sm">
        <span className="min-w-0 break-words text-fg">{label}</span>
        <span className="tabular-nums text-fg-muted">{valueText}</span>
      </div>
      <div aria-hidden="true" className="h-2 w-full overflow-hidden rounded-full bg-surface-muted ring-1 ring-inset ring-border">
        <div className={cx('h-full rounded-full', METER_TONE[tone])} style={{ width: `${meterPercent(ratio)}%` }} />
      </div>
    </div>
  );
}

export interface DonutSegment {
  key: string;
  label: ReactNode;
  value: number;
  /** Класс заливки сегмента (stroke-*) и образца легенды (bg-*). */
  strokeClass: string;
  swatchClass: string;
  /** Дополнение к подписи в легенде (иконка/бейдж) — смысл не только цветом. */
  marker?: ReactNode;
}

/** Длины дуг кольца (доли окружности); сумма 0 — пустое кольцо. Чистая функция (unit-тест). */
export function donutArcs(values: readonly number[], circumference: number): { length: number; offset: number }[] {
  const total = values.reduce((a, v) => a + Math.max(0, v), 0);
  let acc = 0;
  return values.map((v) => {
    const length = total > 0 ? (Math.max(0, v) / total) * circumference : 0;
    const arc = { length, offset: -acc };
    acc += length;
    return arc;
  });
}

/**
 * Кольцевая диаграмма с легендой-списком (значения текстом). SVG — role="img" со сводкой
 * `summary`; легенда — список с подписью и числом каждого сегмента.
 */
export function Donut({
  segments,
  summary,
  center,
  legendLabel,
  testId,
}: {
  segments: readonly DonutSegment[];
  summary: string;
  center?: ReactNode;
  legendLabel: string;
  testId?: string;
}) {
  const id = useId();
  const r = 15.915; // окружность ≈ 100
  const c = 2 * Math.PI * r;
  const arcs = donutArcs(
    segments.map((s) => s.value),
    c,
  );
  return (
    <figure className="flex min-w-0 flex-wrap items-center gap-4" data-testid={testId}>
      <div className="relative h-28 w-28 shrink-0">
        <svg role="img" aria-label={summary} aria-describedby={`${id}-legend`} viewBox="0 0 36 36" className="h-full w-full -rotate-90" data-chart="donut">
          <circle cx={18} cy={18} r={r} fill="none" className="stroke-surface-muted" strokeWidth={4} />
          {segments.map((s, i) =>
            arcs[i]!.length > 0 ? (
              <circle
                key={s.key}
                cx={18}
                cy={18}
                r={r}
                fill="none"
                className={s.strokeClass}
                strokeWidth={4}
                strokeDasharray={`${arcs[i]!.length} ${c - arcs[i]!.length}`}
                strokeDashoffset={arcs[i]!.offset}
              />
            ) : null,
          )}
        </svg>
        {center ? <div className="absolute inset-0 flex items-center justify-center text-center text-sm font-semibold tabular-nums text-fg">{center}</div> : null}
      </div>
      <ul id={`${id}-legend`} aria-label={legendLabel} className="flex min-w-0 flex-1 flex-col gap-1.5 text-sm">
        {segments.map((s) => (
          <li key={s.key} className="flex min-w-0 flex-wrap items-center justify-between gap-2" data-segment={s.key}>
            <span className="flex min-w-0 items-center gap-2">
              <span aria-hidden="true" className={cx('inline-block h-3 w-3 shrink-0 rounded-sm', s.swatchClass)} />
              {s.marker ?? <span className="text-fg">{s.label}</span>}
            </span>
            <span className="tabular-nums text-fg-muted">{s.value.toLocaleString('ru-RU')}</span>
          </li>
        ))}
      </ul>
    </figure>
  );
}
