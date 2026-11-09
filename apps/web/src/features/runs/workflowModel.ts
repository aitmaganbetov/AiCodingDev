/**
 * Workflows (пункт «Процессы», F5; план §3.4): запуск = процесс. Чистые функции экрана — этап
 * жизненного цикла run по FSM, длительность, прогресс (только по подтверждённым задачам — `runProgress`).
 */
import type { RunStatus, RunSummary } from '../../api/runs';

/** Основные этапы FSM run (как «Этапы» навигатора Mission Control). */
export const WORKFLOW_STAGES = ['CREATED', 'DISCOVERY', 'PLANNED', 'RUNNING', 'VERIFYING', 'COMPLETED'] as const satisfies readonly RunStatus[];

export interface WorkflowStage {
  /** Достигнутый этап (индекс в WORKFLOW_STAGES). */
  index: number;
  stage: (typeof WORKFLOW_STAGES)[number];
  /** Статус вне основных этапов (PAUSED, BLOCKED, AWAITING_APPROVAL, FAILED, CANCELLED). */
  special: boolean;
}

export function workflowStage(run: Pick<RunSummary, 'status' | 'started_at' | 'finished_at'>): WorkflowStage {
  const direct = (WORKFLOW_STAGES as readonly string[]).indexOf(run.status);
  if (direct >= 0) return { index: direct, stage: WORKFLOW_STAGES[direct] ?? 'CREATED', special: false };
  // AWAITING_APPROVAL — план готов и ждёт одобрения; прочие — по началу/завершению run.
  const index =
    run.status === 'AWAITING_APPROVAL'
      ? WORKFLOW_STAGES.indexOf('PLANNED')
      : run.finished_at
        ? WORKFLOW_STAGES.length - 1
        : run.started_at
          ? WORKFLOW_STAGES.indexOf('RUNNING')
          : 0;
  return { index, stage: WORKFLOW_STAGES[index] ?? 'CREATED', special: true };
}

/** Длительность, с: от started_at до finished_at (или `now`, если run идёт); null — не начат. */
export function workflowDurationSeconds(run: Pick<RunSummary, 'started_at' | 'finished_at'>, now: number): number | null {
  if (!run.started_at) return null;
  const start = Date.parse(run.started_at);
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  return Math.max(0, Math.round((end - start) / 1000));
}

/** Разбиение длительности для подписи: часы/минуты/секунды (секунды — только для < 1 ч). */
export function durationParts(seconds: number): { h: number; m: number; s: number | null } {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return { h, m, s: h > 0 ? null : seconds % 60 };
}

export const FINISHED_RUN_STATUSES: readonly RunStatus[] = ['COMPLETED', 'FAILED', 'CANCELLED'];

export function isRunFinished(status: RunStatus): boolean {
  return FINISHED_RUN_STATUSES.includes(status);
}
