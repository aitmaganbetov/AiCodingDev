export function formatDate(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(d);
}

export type DateTimeFormat = 'date' | 'datetime' | 'time';

const STYLES: Record<DateTimeFormat, Intl.DateTimeFormatOptions> = {
  date: { dateStyle: 'medium' },
  datetime: { dateStyle: 'medium', timeStyle: 'short' },
  time: { timeStyle: 'medium' },
};

export function formatDateTime(iso: string, locale: string, format: DateTimeFormat = 'datetime'): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat(locale, STYLES[format]).format(d);
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['week', 7 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
];

/** «5 минут назад», «через 2 часа», «вчера» (Intl.RelativeTimeFormat, numeric: auto). */
export function formatRelative(iso: string, locale: string, now: number = Date.now()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const diffSec = Math.round((d.getTime() - now) / 1000);
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
  const abs = Math.abs(diffSec);
  if (abs < 45) return rtf.format(0, 'second');
  for (const [unit, sec] of UNITS) {
    if (abs >= sec || unit === 'second') return rtf.format(Math.round(diffSec / sec), unit);
  }
  return rtf.format(0, 'second');
}
