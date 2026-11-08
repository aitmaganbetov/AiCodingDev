/** Форматирование и параметры экрана Analytics (чистые функции — покрыты unit-тестами). */
import type { TFunction } from 'i18next';
import type { AnalyticsQuery } from '../../api/analytics';

export const PERIODS = ['7d', '30d', '90d', '365d'] as const;
export type Period = (typeof PERIODS)[number];
export const DEFAULT_PERIOD: Period = '30d';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DAY_MS = 86_400_000;

export interface AnalyticsParams {
  period: Period;
  projectId: string | null;
}

/** Параметры из URL (`?period=7d&project=<uuid>`); неизвестные значения игнорируются. */
export function paramsFromSearch(search: URLSearchParams): AnalyticsParams {
  const raw = search.get('period');
  const period = (PERIODS as readonly string[]).includes(raw ?? '') ? (raw as Period) : DEFAULT_PERIOD;
  const project = search.get('project');
  return { period, projectId: project && UUID_RE.test(project) ? project : null };
}

export function searchFromParams(params: AnalyticsParams): URLSearchParams {
  const out = new URLSearchParams();
  if (params.period !== DEFAULT_PERIOD) out.set('period', params.period);
  if (params.projectId) out.set('project', params.projectId);
  return out;
}

/**
 * Запрос API: окно заканчивается на начале следующего часа (стабильный ключ кэша в пределах часа),
 * длина — по периоду.
 */
export function queryFromParams(params: AnalyticsParams, now: Date = new Date()): AnalyticsQuery {
  const end = new Date(now);
  end.setUTCMinutes(0, 0, 0);
  end.setUTCHours(end.getUTCHours() + 1);
  const days = Number(params.period.slice(0, -1));
  const start = new Date(end.getTime() - days * DAY_MS);
  const query: AnalyticsQuery = { from: start.toISOString(), to: end.toISOString() };
  if (params.projectId) query.project_id = params.projectId;
  return query;
}

export function formatRate(t: TFunction, value: number | null | undefined): string {
  if (value === null || value === undefined) return t('analytics.noData');
  return `${(value * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`;
}

export function formatDuration(t: TFunction, seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return t('analytics.noData');
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return t('analytics.duration.seconds', { s: total });
  if (total < 3600) return t('analytics.duration.minutes', { m: Math.floor(total / 60), s: total % 60 });
  if (total < 86_400) return t('analytics.duration.hours', { h: Math.floor(total / 3600), m: Math.floor((total % 3600) / 60) });
  return t('analytics.duration.days', { d: Math.floor(total / 86_400), h: Math.floor((total % 86_400) / 3600) });
}

export function formatBytes(t: TFunction, bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return t('analytics.noData');
  const fmt = (v: number) => v.toLocaleString('ru-RU', { maximumFractionDigits: 1 });
  if (bytes < 1024) return t('analytics.bytes.b', { v: fmt(bytes) });
  if (bytes < 1024 * 1024) return t('analytics.bytes.kb', { v: fmt(bytes / 1024) });
  return t('analytics.bytes.mb', { v: fmt(bytes / (1024 * 1024)) });
}

/** Ширина полосы в процентах 0..100 (null — пустая полоса). */
export function barPercent(value: number | null | undefined): number {
  if (value === null || value === undefined || Number.isNaN(value)) return 0;
  return Math.min(100, Math.max(0, value * 100));
}
