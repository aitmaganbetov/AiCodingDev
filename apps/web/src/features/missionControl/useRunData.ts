import { useQuery, useQueryClient } from '@tanstack/react-query';
import type { TFunction } from 'i18next';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { chatKeys } from '../../api/chat';
import { directiveKeys } from '../../api/directives';
import { qualityApi, qualityKeys } from '../../api/quality';
import { runKeys } from '../../api/runs';
import type { RunEvent } from '../../api/runEvents';
import { taskKeys, tasksApi } from '../../api/tasks';
import { useRunEvents } from '../../api/useRunEvents';
import { can } from '../../lib/permissions';
import { CHAT_EVENTS } from '../chat/useChatFeed';
import { enumLabel, roleLabel } from '../runs/labels';
import { applyTaskEvents, useRunTasks } from '../runs/useRunTasks';
import { useRunDetail } from '../runs/useRunStatus';
import { taskIndicators } from './feedModel';

/**
 * Сколько событий run держит экран (журнал, вывод CLI). Поток начинается с seq 1 (история из
 * run_event_log), поэтому лента полная; старше лимита — GET /runs/{id}/activity (Task Detail).
 */
export const MC_EVENT_LIMIT = 10_000;
/** Выборка evidence run для индикаторов узлов (одна страница; больше — подписывается «N+»). */
export const MC_EVIDENCE_SAMPLE = 200;

const RUN_EVENTS = new Set(['run.state_changed', 'run.completed', 'approval.requested', 'approval.decided']);
const TASK_EVENTS = new Set(['task.state_changed', 'gate.completed']);
const DIRECTIVE_EVENTS = new Set(['directive.state_changed', 'directive.impact_ready']);
const REFRESH_DELAY_MS = 400;
/**
 * После `run.completed` сервер закрывает поток, а проекции сообщений агентов в чат (worker, outbox) могут
 * дописаться позже — их `chat.message` уже не придёт. Поэтому ленту дочитываем REST-ом несколько раз
 * в ограниченном окне после завершения (итоговое сообщение агента не теряется без перезагрузки).
 */
export const FINAL_CHAT_REFRESH_MS: readonly number[] = [400, 2_000, 6_000, 15_000];

/**
 * Данные одного run для всех панелей Mission Control: run (getRun), DAG (getRunTasks), один общий
 * SSE-поток run (StreamManager: тот же, что у панели поручений; лимит 5 потоков не тратится),
 * отчёт качества и выборка evidence для измеренных индикаторов узлов. Пачка событий — одна волна
 * инвалидаций (replay сотен событий не порождает сотни запросов); статусы задач — сразу в кэш DAG.
 */
export function useMissionControlRun(runId: string, role: string | null | undefined, selectedTaskId: string | null, t: TFunction) {
  const queryClient = useQueryClient();
  const { run, isTerminal } = useRunDetail(runId, Boolean(runId));
  const { query: tasksQuery, tasks, edges, planVersion } = useRunTasks(runId, { all: true, enabled: Boolean(runId) });
  const [announcement, setAnnouncement] = useState('');
  const [resynced, setResynced] = useState(false);
  const canQuality = can(role, 'quality:read');
  const canEvidence = can(role, 'evidence:read');
  const selectedRef = useRef(selectedTaskId);
  useEffect(() => {
    selectedRef.current = selectedTaskId;
  }, [selectedTaskId]);

  const chatTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const finalTimers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(
    () => () => {
      if (chatTimer.current) clearTimeout(chatTimer.current);
      for (const timer of finalTimers.current) clearTimeout(timer);
    },
    [],
  );

  const onEvents = useCallback(
    (events: RunEvent[]) => {
      applyTaskEvents(queryClient, runId, events);
      const has = (set: ReadonlySet<string>) => events.some((e) => set.has(e.type));
      const lastTask = [...events].reverse().find((e) => e.type === 'task.state_changed');
      if (lastTask) {
        const p = lastTask.payload;
        setAnnouncement(
          t('event.taskTransition', {
            task: typeof p.title === 'string' ? p.title : roleLabel(t, p.agent_role),
            from: enumLabel(t, 'task_status', p.from_status),
            to: enumLabel(t, 'task_status', p.to_status),
          }),
        );
      }
      if (has(RUN_EVENTS) || has(TASK_EVENTS)) void queryClient.invalidateQueries({ queryKey: runKeys.detail(runId), exact: true });
      if (has(TASK_EVENTS)) void queryClient.invalidateQueries({ queryKey: runKeys.tasks(runId) });
      if (events.some((e) => e.type === 'gate.completed' || e.type === 'run.completed')) {
        void queryClient.invalidateQueries({ queryKey: taskKeys.quality(runId) });
        void queryClient.invalidateQueries({ queryKey: ['evidence', 'run', runId] });
      }
      if (has(DIRECTIVE_EVENTS)) void queryClient.invalidateQueries({ queryKey: directiveKeys.all(runId) });
      if (events.some((e) => e.type === 'run.completed') && finalTimers.current.length === 0) {
        finalTimers.current = FINAL_CHAT_REFRESH_MS.map((ms) =>
          setTimeout(() => void queryClient.invalidateQueries({ queryKey: chatKeys.all(runId) }), ms),
        );
      }
      const selected = selectedRef.current;
      if (selected) {
        const mine = events.filter((e) => e.payload.task_id === selected);
        if (mine.some((e) => e.type === 'task.state_changed')) {
          void queryClient.invalidateQueries({ queryKey: taskKeys.detail(selected), exact: true });
          void queryClient.invalidateQueries({ queryKey: taskKeys.attempts(selected) });
        }
        if (mine.some((e) => e.type === 'gate.completed')) void queryClient.invalidateQueries({ queryKey: taskKeys.evidence(selected) });
      }
      if (!has(CHAT_EVENTS) || chatTimer.current) return;
      chatTimer.current = setTimeout(() => {
        chatTimer.current = null;
        void queryClient.invalidateQueries({ queryKey: chatKeys.all(runId) });
      }, REFRESH_DELAY_MS);
    },
    [queryClient, runId, t],
  );
  const onResync = useCallback(() => {
    setResynced(true);
    for (const key of [runKeys.detail(runId), runKeys.tasks(runId), chatKeys.all(runId), directiveKeys.all(runId), taskKeys.quality(runId)])
      void queryClient.invalidateQueries({ queryKey: key });
  }, [queryClient, runId]);
  const stream = useRunEvents(runId, { onEvents, onResync, isTerminal, limit: MC_EVENT_LIMIT });

  const quality = useQuery({
    queryKey: taskKeys.quality(runId),
    queryFn: ({ signal }) => tasksApi.quality(runId, signal),
    enabled: Boolean(runId) && canQuality,
  });
  const evidenceQuery = { limit: MC_EVIDENCE_SAMPLE };
  const evidence = useQuery({
    queryKey: qualityKeys.runEvidence(runId, evidenceQuery),
    queryFn: ({ signal }) => qualityApi.runEvidence(runId, evidenceQuery, signal),
    enabled: Boolean(runId) && canEvidence,
  });
  const indicators = useMemo(
    () => taskIndicators(tasks, quality.data?.gate_executions ?? null, evidence.data?.items ?? null),
    [tasks, quality.data, evidence.data],
  );

  return { run, isTerminal, tasksQuery, tasks, edges, planVersion, stream, announcement, resynced, quality, evidence, indicators };
}

export type MissionControlRun = ReturnType<typeof useMissionControlRun>;
