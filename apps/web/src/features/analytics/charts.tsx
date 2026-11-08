/**
 * Простые доступные диаграммы без библиотек (P8-07): SVG-столбцы и CSS-полосы.
 * Графика — дополнение: те же значения всегда доступны текстом (подписи полос) или таблицей
 * (`<details>` под диаграммой), поэтому SVG имеет текстовую альтернативу (role="img" + aria-label),
 * а полосы декоративны (aria-hidden).
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import type { AnalyticsSummary } from '../../api/analytics';
import { Table, type Column } from '../../components/ui';
import { barPercent } from './format';

type Day = AnalyticsSummary['daily'][number];

const SERIES = [
  { key: 'runs_completed', className: 'fill-success-fg', swatch: 'bg-success-fg', label: 'analytics.daily.completed' },
  { key: 'runs_failed', className: 'fill-danger-text', swatch: 'bg-danger-text', label: 'analytics.daily.failed' },
  { key: 'runs_cancelled', className: 'fill-fg-muted', swatch: 'bg-fg-muted', label: 'analytics.daily.cancelled' },
] as const;

function other(d: Day): number {
  return Math.max(0, d.runs_created - d.runs_completed - d.runs_failed - d.runs_cancelled);
}

export function RunsByDayChart({ days }: { days: Day[] }) {
  const { t } = useTranslation();
  const titleId = useId();
  const max = Math.max(1, ...days.map((d) => d.runs_created));
  const width = Math.max(days.length * 24, 240);
  const height = 120;
  const totals = days.reduce(
    (acc, d) => ({
      total: acc.total + d.runs_created,
      completed: acc.completed + d.runs_completed,
      failed: acc.failed + d.runs_failed,
      cancelled: acc.cancelled + d.runs_cancelled,
    }),
    { total: 0, completed: 0, failed: 0, cancelled: 0 },
  );
  const columns: Column<Day>[] = [
    { id: 'date', header: t('analytics.daily.date'), rowHeader: true, cell: (d) => d.date, sortValue: (d) => d.date },
    { id: 'created', header: t('analytics.daily.created'), cell: (d) => d.runs_created, sortValue: (d) => d.runs_created },
    { id: 'completed', header: t('analytics.daily.completed'), cell: (d) => d.runs_completed, sortValue: (d) => d.runs_completed },
    { id: 'failed', header: t('analytics.daily.failed'), cell: (d) => d.runs_failed, sortValue: (d) => d.runs_failed },
    { id: 'cancelled', header: t('analytics.daily.cancelled'), cell: (d) => d.runs_cancelled, sortValue: (d) => d.runs_cancelled },
    { id: 'other', header: t('analytics.daily.other'), cell: (d) => other(d), sortValue: (d) => other(d) },
  ];
  return (
    <figure aria-labelledby={titleId} className="flex flex-col gap-3">
      <figcaption id={titleId} className="sr-only">
        {t('analytics.daily.title')}
      </figcaption>
      <div className="overflow-x-auto">
        <svg
          role="img"
          aria-label={t('analytics.daily.chartLabel', totals)}
          viewBox={`0 0 ${width} ${height}`}
          className="h-32 w-full min-w-[240px]"
          preserveAspectRatio="none"
          data-testid="runs-by-day-chart"
        >
          <line x1={0} y1={height - 0.5} x2={width} y2={height - 0.5} className="stroke-border-strong" strokeWidth={1} />
          {days.map((d, i) => {
            let y = height;
            const x = i * (width / days.length) + 2;
            const w = Math.max(width / days.length - 4, 2);
            const parts = [...SERIES.map((s) => ({ value: d[s.key], className: s.className })), { value: other(d), className: 'fill-accent' }];
            return (
              <g key={d.date}>
                {parts.map((p, j) => {
                  const h = (p.value / max) * (height - 4);
                  y -= h;
                  return h > 0 ? <rect key={j} x={x} y={y} width={w} height={h} className={p.className} /> : null;
                })}
              </g>
            );
          })}
        </svg>
      </div>
      <ul aria-label={t('analytics.daily.legend')} className="flex flex-wrap gap-4 text-sm text-fg-muted">
        {SERIES.map((s) => (
          <li key={s.key} className="flex items-center gap-2">
            <span aria-hidden="true" className={`inline-block h-3 w-3 rounded-sm ${s.swatch}`} />
            {t(s.label)}
          </li>
        ))}
        <li className="flex items-center gap-2">
          <span aria-hidden="true" className="inline-block h-3 w-3 rounded-sm bg-accent" />
          {t('analytics.daily.other')}
        </li>
      </ul>
      <details>
        <summary className="min-h-touch cursor-pointer py-2 text-link underline">{t('analytics.daily.showTable')}</summary>
        <Table caption={t('analytics.daily.caption')} columns={columns} rows={days} rowKey={(d) => d.date} minWidthClass="min-w-[480px]" />
      </details>
    </figure>
  );
}

export interface Bar {
  key: string;
  label: string;
  value: number | null;
  /** Текст значения (виден и озвучивается). */
  text: string;
}

/** Горизонтальные полосы долей 0..1 с текстовыми значениями. */
export function BarList({ bars, label }: { bars: Bar[]; label: string }) {
  return (
    <ul aria-label={label} className="flex flex-col gap-3">
      {bars.map((b) => (
        <li key={b.key} className="flex flex-col gap-1" data-bar={b.key}>
          <div className="flex flex-wrap justify-between gap-2 text-sm">
            <span className="font-medium text-fg">{b.label}</span>
            <span className="text-fg-muted">{b.text}</span>
          </div>
          <div aria-hidden="true" className="h-2 w-full overflow-hidden rounded-full bg-surface-muted ring-1 ring-inset ring-border">
            <div className="h-full rounded-full bg-accent" style={{ width: `${barPercent(b.value)}%` }} />
          </div>
        </li>
      ))}
    </ul>
  );
}
