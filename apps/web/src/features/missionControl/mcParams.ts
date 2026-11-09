/**
 * Состояние AI Mission Control в URL (план §6.1 п. 3, C-6): ссылкой можно поделиться, «назад» не
 * засоряется (useInPageSearchParams — replaceState). Параметры:
 *  - `run` — выбранный run; `task` — выбранная задача (Agent Inspector, подсветка узла);
 *  - `view` — вид центральной ленты; `nav` — граф или список в навигаторе; `pane` — вкладка панели
 *    на узких экранах (< 1366 px);
 *  - фильтры «Беседы» — прежние фильтры Live Agent Chat (`stream`, `role`, `thread`, `q`, `types`),
 *    задача потока «Задача» — `ctask` (не путать с выбранной `task`);
 *  - фильтры остальных видов — `frole` (роль агента), `ftask` (задача), `since` (время), `ftypes`
 *    (типы событий журнала), `fq` (поиск по выводу CLI), `fstatus` (статус evidence).
 */
import type { ChatFilters } from '../../api/chat';
import type { AgentRole } from '../../api/runs';
import { AGENT_ROLES, filtersFromParams, paramsFromFilters } from '../chat/chatModel';

export const MC_VIEWS = ['conversation', 'cli', 'system', 'directives', 'evidence'] as const;
export type McView = (typeof MC_VIEWS)[number];
export const MC_PANES = ['navigator', 'feed', 'inspector'] as const;
export type McPane = (typeof MC_PANES)[number];
export type NavMode = 'graph' | 'list';
export const SINCE_OPTIONS = ['15m', '1h', '24h'] as const;
export type Since = (typeof SINCE_OPTIONS)[number];
const SINCE_MS: Record<Since, number> = { '15m': 15 * 60_000, '1h': 3_600_000, '24h': 86_400_000 };

/** Группы типов журнала событий (вид «Системные события»). Пусто — все события run. */
export const EVENT_GROUPS = ['state', 'agents', 'cli', 'quality', 'approvals', 'directives'] as const;
export type EventGroup = (typeof EVENT_GROUPS)[number];
export const EVENT_GROUP_TYPES: Record<EventGroup, readonly string[]> = {
  state: ['run.state_changed', 'task.state_changed', 'run.completed'],
  agents: ['agent.message', 'chat.message'],
  cli: ['cli.output'],
  quality: ['gate.completed'],
  approvals: ['approval.requested', 'approval.decided'],
  directives: ['directive.state_changed', 'directive.impact_ready'],
};

export const EVIDENCE_STATUSES = ['VERIFIED', 'PENDING', 'UNVERIFIED', 'REJECTED'] as const;
export type EvidenceStatusFilter = (typeof EVIDENCE_STATUSES)[number];

export interface FeedFilters {
  role?: AgentRole | undefined;
  task?: string | undefined;
  since?: Since | undefined;
  groups?: EventGroup[] | undefined;
  q?: string | undefined;
  evidenceStatus?: EvidenceStatusFilter | undefined;
}

export interface McState {
  runId: string | null;
  taskId: string | null;
  view: McView;
  nav: NavMode;
  pane: McPane | null;
  chat: ChatFilters;
  feed: FeedFilters;
}

const oneOf = <T extends string>(list: readonly T[], v: string | null): T | undefined =>
  v !== null && (list as readonly string[]).includes(v) ? (v as T) : undefined;

export function readMcState(params: URLSearchParams): McState {
  // Фильтры чата читаются прежним разбором (chatModel); `task` потока «Задача» — из `ctask`.
  const chatParams = new URLSearchParams();
  for (const key of ['stream', 'role', 'thread', 'q', 'types']) {
    const v = params.get(key);
    if (v !== null) chatParams.set(key, v);
  }
  const ctask = params.get('ctask');
  if (ctask) chatParams.set('task', ctask);
  const groups = (params.get('ftypes') ?? '').split(',').filter((g): g is EventGroup => (EVENT_GROUPS as readonly string[]).includes(g));
  return {
    runId: params.get('run') || null,
    taskId: params.get('task') || null,
    view: oneOf(MC_VIEWS, params.get('view')) ?? 'conversation',
    nav: params.get('nav') === 'list' ? 'list' : 'graph',
    pane: oneOf(MC_PANES, params.get('pane')) ?? null,
    chat: filtersFromParams(chatParams),
    feed: {
      role: oneOf(AGENT_ROLES, params.get('frole')),
      task: params.get('ftask') || undefined,
      since: oneOf(SINCE_OPTIONS, params.get('since')),
      groups: groups.length > 0 ? groups : undefined,
      q: params.get('fq')?.slice(0, 200) || undefined,
      evidenceStatus: oneOf(EVIDENCE_STATUSES, params.get('fstatus')),
    },
  };
}

export function writeMcState(s: McState): URLSearchParams {
  const p = new URLSearchParams();
  if (s.runId) p.set('run', s.runId);
  if (s.taskId) p.set('task', s.taskId);
  if (s.view !== 'conversation') p.set('view', s.view);
  if (s.nav !== 'graph') p.set('nav', s.nav);
  if (s.pane) p.set('pane', s.pane);
  const chat = paramsFromFilters(s.chat);
  for (const [k, v] of chat) p.set(k === 'task' ? 'ctask' : k, v);
  if (s.feed.role) p.set('frole', s.feed.role);
  if (s.feed.task) p.set('ftask', s.feed.task);
  if (s.feed.since) p.set('since', s.feed.since);
  if (s.feed.groups && s.feed.groups.length > 0) p.set('ftypes', s.feed.groups.join(','));
  if (s.feed.q) p.set('fq', s.feed.q);
  if (s.feed.evidenceStatus) p.set('fstatus', s.feed.evidenceStatus);
  return p;
}

/** Нижняя граница времени фильтра «за период» (null — без ограничения). */
export function sinceCutoff(since: Since | undefined, now: number): number | null {
  return since ? now - SINCE_MS[since] : null;
}

/** Ссылка на Mission Control (уведомления, поиск, Task Detail). */
export function missionControlHref(workspaceId: string, runId: string, extra: Record<string, string> = {}): string {
  const p = new URLSearchParams({ run: runId, ...extra });
  return `/w/${workspaceId}/mission-control?${p.toString()}`;
}
