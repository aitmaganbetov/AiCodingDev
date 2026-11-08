import type { AuditFilters } from '../../api/audit';

export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const FILTER_KEYS = ['action', 'resource_type', 'resource_id', 'actor_id', 'from', 'to'] as const;
export type FilterKey = (typeof FILTER_KEYS)[number];

/** Фильтры журнала из query-строки страницы (from/to — ISO 8601 UTC, как в API). */
export function filtersFromSearch(params: URLSearchParams): AuditFilters {
  const result: AuditFilters = {};
  for (const key of FILTER_KEYS) {
    const v = params.get(key)?.trim();
    if (v) result[key] = v;
  }
  return result;
}

export function searchFromFilters(filters: AuditFilters): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of FILTER_KEYS) {
    const v = filters[key];
    if (v) params.set(key, v);
  }
  return params;
}

/** <input type="datetime-local"> (локальное время) → ISO UTC; пусто/невалидно → undefined. */
export function localInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** ISO UTC → значение datetime-local в локальной зоне браузера. */
export function isoToLocalInput(iso: string | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export interface FilterDraft {
  action: string;
  resource_type: string;
  resource_id: string;
  actor_id: string;
  from: string;
  to: string;
}

export function draftFromFilters(f: AuditFilters): FilterDraft {
  return {
    action: f.action ?? '',
    resource_type: f.resource_type ?? '',
    resource_id: f.resource_id ?? '',
    actor_id: f.actor_id ?? '',
    from: isoToLocalInput(f.from),
    to: isoToLocalInput(f.to),
  };
}

/** Проверка и преобразование черновика: actor_id — UUID, from ≤ to. */
export function filtersFromDraft(d: FilterDraft): { filters: AuditFilters; errors: Partial<Record<FilterKey, 'uuid' | 'range' | 'date'>> } {
  const errors: Partial<Record<FilterKey, 'uuid' | 'range' | 'date'>> = {};
  const filters: AuditFilters = {};
  for (const key of ['action', 'resource_type', 'resource_id'] as const) {
    const v = d[key].trim();
    if (v) filters[key] = v;
  }
  const actor = d.actor_id.trim();
  if (actor) {
    if (UUID_PATTERN.test(actor)) filters.actor_id = actor.toLowerCase();
    else errors.actor_id = 'uuid';
  }
  const from = localInputToIso(d.from);
  const to = localInputToIso(d.to);
  if (d.from && !from) errors.from = 'date';
  if (d.to && !to) errors.to = 'date';
  if (from) filters.from = from;
  if (to) filters.to = to;
  if (from && to && from > to) errors.to = 'range';
  return { filters, errors };
}
