import { Navigate, useLocation, useParams } from 'react-router';

/**
 * Редирект старого пути внутри workspace на новый (F2, план §6.1): query и hash сохраняются,
 * чтобы прежние deep links (фильтры аудита, `?provider=`) открывали тот же контент.
 */
export function WorkspaceRedirect({ to }: { to: string }) {
  const { workspaceId } = useParams();
  const { search, hash } = useLocation();
  return <Navigate replace to={`/w/${workspaceId ?? ''}/${to}${search}${hash}`} />;
}

/** Фильтры прежнего Live Agent Chat, которые Mission Control понимает так же. */
const CHAT_PARAMS = ['stream', 'role', 'thread', 'q', 'types'] as const;

/**
 * Старые экраны run (F4, план §6.1 п. 3, C-7): обзор run, граф и чат открываются в AI Mission Control
 * с тем же run и тем же выбором. Граф: `?view=list` → список навигатора. Чат: фильтры потока
 * сохраняются, `task` (поток «Задача») → фильтр ленты `ctask` и выбранная задача инспектора.
 * Task Detail остаётся отдельным экраном (deep links на вкладки задачи).
 */
export function missionControlTarget(runId: string, from: 'run' | 'graph' | 'chat', search: string): string {
  const old = new URLSearchParams(search);
  const next = new URLSearchParams({ run: runId });
  if (from === 'graph') {
    if (old.get('view') === 'list') next.set('nav', 'list');
  } else if (from === 'chat') {
    next.set('view', 'conversation');
    for (const key of CHAT_PARAMS) {
      const v = old.get(key);
      if (v) next.set(key, v);
    }
    const task = old.get('task');
    if (task) {
      next.set('ctask', task);
      next.set('task', task);
    }
  } else {
    next.set('view', 'system');
  }
  return `mission-control?${next.toString()}`;
}

export function RunRedirect({ from }: { from: 'run' | 'graph' | 'chat' }) {
  const { workspaceId = '', runId = '' } = useParams();
  const { search, hash } = useLocation();
  return <Navigate replace to={`/w/${workspaceId}/${missionControlTarget(runId, from, search)}${hash}`} />;
}
