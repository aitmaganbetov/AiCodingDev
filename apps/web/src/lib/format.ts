export type DateTimeFormat = 'date' | 'datetime' | 'time';

/**
 * Кэш форматтеров Intl по (локаль, формат): создание Intl.DateTimeFormat заметно дороже
 * форматирования, а время рендерится в каждой строке длинных виртуализированных лент (CLI, события).
 */
const dateTimeFormatters = new Map<string, Intl.DateTimeFormat>();
function dateTimeFormatter(locale: string, format: DateTimeFormat): Intl.DateTimeFormat {
  const key = `${locale}|${format}`;
  let f = dateTimeFormatters.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(locale, STYLES[format]);
    dateTimeFormatters.set(key, f);
  }
  return f;
}
const relativeFormatters = new Map<string, Intl.RelativeTimeFormat>();
function relativeFormatter(locale: string): Intl.RelativeTimeFormat {
  let f = relativeFormatters.get(locale);
  if (!f) {
    f = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' });
    relativeFormatters.set(locale, f);
  }
  return f;
}

export function formatDate(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return dateTimeFormatter(locale, 'date').format(d);
}

const STYLES: Record<DateTimeFormat, Intl.DateTimeFormatOptions> = {
  date: { dateStyle: 'medium' },
  datetime: { dateStyle: 'medium', timeStyle: 'short' },
  time: { timeStyle: 'medium' },
};

export function formatDateTime(iso: string, locale: string, format: DateTimeFormat = 'datetime'): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return dateTimeFormatter(locale, format).format(d);
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
  const rtf = relativeFormatter(locale);
  const abs = Math.abs(diffSec);
  if (abs < 45) return rtf.format(0, 'second');
  for (const [unit, sec] of UNITS) {
    if (abs >= sec || unit === 'second') return rtf.format(Math.round(diffSec / sec), unit);
  }
  return rtf.format(0, 'second');
}
