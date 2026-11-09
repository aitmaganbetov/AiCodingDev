/**
 * Модель Центра безопасности (F7, план §3.7): фильтры в URL, распределение по серьёзности,
 * сопоставления CWE / контролей и классификация находок по типу gate-источника.
 *
 * Только данные контракта: CWE — `Finding.cwe_id`, контроли/OWASP — `Finding.control_refs` как есть
 * (категория OWASP не выводится, если её нет в данных), правило — `rule_id`. Тип проверки находки —
 * `gate_type` её `gate_execution_id` из отчёта качества run (`getRunQuality`); по имени правила
 * или файла тип не угадывается.
 */
import {
  FINDING_SEVERITIES,
  FINDING_STATUSES,
  type Finding,
  type FindingSeverity,
  type FindingStatus,
  type ListWorkspaceFindingsQuery,
} from '../../api/findings';

export type FindingSource = NonNullable<Finding['source']>;
export const FINDING_SOURCES: readonly FindingSource[] = ['gate', 'agent_review', 'manual', 'github'];

/** «Требуют решения» — открытые и подтверждённые (ещё не исправлены и не закрыты решением). */
export const UNRESOLVED_STATUSES: readonly FindingStatus[] = ['OPEN', 'ACKNOWLEDGED'];

export interface SecurityFilters {
  severity: FindingSeverity[];
  /** Пусто — все статусы. */
  status: FindingStatus[];
  source: FindingSource[];
  project: string;
  run: string;
  /** Клиентские фильтры по загруженной выборке (в API таких параметров нет). */
  cwe: string;
  control: string;
}

function pick<T extends string>(values: readonly string[], allowed: readonly T[]): T[] {
  return allowed.filter((a) => values.includes(a));
}

export function parseFilters(params: URLSearchParams): SecurityFilters {
  return {
    severity: pick(params.getAll('severity'), FINDING_SEVERITIES),
    status: pick(params.getAll('status'), FINDING_STATUSES),
    source: pick(params.getAll('source'), FINDING_SOURCES),
    project: params.get('project') ?? '',
    run: params.get('run') ?? '',
    cwe: params.get('cwe') ?? '',
    control: params.get('control') ?? '',
  };
}

/** Фильтры → параметры `GET /findings` (серверные: severity, status, source, project_id, run_id). */
export function filtersToQuery(f: SecurityFilters): ListWorkspaceFindingsQuery {
  return {
    ...(f.severity.length ? { severity: f.severity } : {}),
    ...(f.status.length ? { status: f.status } : {}),
    ...(f.source.length ? { source: f.source } : {}),
    ...(f.project ? { project_id: f.project } : {}),
    ...(f.run ? { run_id: f.run } : {}),
  };
}

/** Клиентские фильтры (CWE, контроль) поверх загруженных страниц. */
export function applyClientFilters(items: readonly Finding[], f: Pick<SecurityFilters, 'cwe' | 'control'>): Finding[] {
  return items.filter((x) => (!f.cwe || x.cwe_id === f.cwe) && (!f.control || (x.control_refs ?? []).includes(f.control)));
}

export function severityDistribution(items: readonly Finding[]): Record<FindingSeverity, number> {
  const out = Object.fromEntries(FINDING_SEVERITIES.map((s) => [s, 0])) as Record<FindingSeverity, number>;
  for (const f of items) out[f.severity] += 1;
  return out;
}

export function statusDistribution(items: readonly Finding[]): Record<FindingStatus, number> {
  const out = Object.fromEntries(FINDING_STATUSES.map((s) => [s, 0])) as Record<FindingStatus, number>;
  for (const f of items) out[f.status] = (out[f.status] ?? 0) + 1;
  return out;
}

export interface MappingCount {
  key: string;
  count: number;
  /** Сколько из них ещё требуют решения (OPEN/ACKNOWLEDGED). */
  unresolved: number;
}

function countBy(items: readonly Finding[], keys: (f: Finding) => readonly string[]): MappingCount[] {
  const map = new Map<string, MappingCount>();
  for (const f of items) {
    for (const key of new Set(keys(f))) {
      const cur = map.get(key) ?? { key, count: 0, unresolved: 0 };
      cur.count += 1;
      if (UNRESOLVED_STATUSES.includes(f.status)) cur.unresolved += 1;
      map.set(key, cur);
    }
  }
  return [...map.values()].sort((a, b) => b.count - a.count || a.key.localeCompare(b.key));
}

/** CWE (`cwe_id`) — только присутствующие значения. */
export function cweMappings(items: readonly Finding[]): MappingCount[] {
  return countBy(items, (f) => (f.cwe_id ? [f.cwe_id] : []));
}

/** Ссылки на контроли стандартов (`control_refs`, например ASVS/OWASP) — как в данных. */
export function controlMappings(items: readonly Finding[]): MappingCount[] {
  return countBy(items, (f) => (f.control_refs ?? []).filter(Boolean));
}

/** Группы проверок безопасности по `gate_type` (QUALITY_GATES §2). */
export type SecurityKind = 'dependency' | 'secrets' | 'code' | 'other_gate' | 'not_gate' | 'unknown';
export const DEPENDENCY_GATE_TYPES: readonly string[] = ['SCA', 'SBOM'];
export const SECRETS_GATE_TYPES: readonly string[] = ['SECRETS'];
export const CODE_GATE_TYPES: readonly string[] = ['SAST', 'IAC'];
export const SECURITY_GATE_TYPES: readonly string[] = [...DEPENDENCY_GATE_TYPES, ...SECRETS_GATE_TYPES, ...CODE_GATE_TYPES, 'SIGN'];

/**
 * Вид находки по типу её gate. `gateTypes` — gate_execution_id → gate_type из загруженных отчётов
 * качества; `loadedRuns` — run, чьи отчёты загружены (иначе тип неизвестен — `unknown`).
 */
export function securityKind(f: Finding, gateTypes: ReadonlyMap<string, string>, loadedRuns: ReadonlySet<string>): SecurityKind {
  if (!f.gate_execution_id) return f.source && f.source !== 'gate' ? 'not_gate' : 'unknown';
  const type = gateTypes.get(f.gate_execution_id);
  if (!type) return f.run_id && loadedRuns.has(f.run_id) ? 'other_gate' : 'unknown';
  if (DEPENDENCY_GATE_TYPES.includes(type)) return 'dependency';
  if (SECRETS_GATE_TYPES.includes(type)) return 'secrets';
  if (CODE_GATE_TYPES.includes(type)) return 'code';
  return 'other_gate';
}

/** Run в порядке первого появления (для ограниченной подгрузки отчётов качества). */
export function distinctRunIds(items: readonly Finding[], limit: number): string[] {
  const out: string[] = [];
  for (const f of items) {
    if (f.run_id && !out.includes(f.run_id)) out.push(f.run_id);
    if (out.length >= limit) break;
  }
  return out;
}

/** Обновление параметров URL: пустые значения удаляются, остальные — повторяемые параметры. */
export function withParam(params: URLSearchParams, key: string, value: string | readonly string[]): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete(key);
  for (const v of typeof value === 'string' ? [value] : value) if (v) next.append(key, v);
  return next;
}
