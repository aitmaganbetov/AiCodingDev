import type { RunStatus, RunSummary } from '../../api/runs';

/** Статусы run, которые идут сейчас (не терминальные и не требующие человека). */
export const ACTIVE_RUN_STATUSES: readonly RunStatus[] = ['CREATED', 'DISCOVERY', 'PLANNED', 'RUNNING', 'VERIFYING'];
/** Статусы, требующие внимания человека (одобрение, блокировка, пауза). */
export const ATTENTION_RUN_STATUSES: readonly RunStatus[] = ['AWAITING_APPROVAL', 'BLOCKED', 'PAUSED'];

export interface RunsSummary {
  active: RunSummary[];
  attention: RunSummary[];
  recent: RunSummary[];
  failed: number;
  completed: number;
}

/** Группировка последних runs workspace для Dashboard (без отдельного агрегирующего эндпоинта). */
export function summarizeRuns(runs: readonly RunSummary[], recentLimit = 8): RunsSummary {
  const byNewest = [...runs].sort((a, b) => b.created_at.localeCompare(a.created_at));
  return {
    active: byNewest.filter((r) => ACTIVE_RUN_STATUSES.includes(r.status)),
    attention: byNewest.filter((r) => ATTENTION_RUN_STATUSES.includes(r.status)),
    recent: byNewest.slice(0, recentLimit),
    failed: byNewest.filter((r) => r.status === 'FAILED').length,
    completed: byNewest.filter((r) => r.status === 'COMPLETED').length,
  };
}
