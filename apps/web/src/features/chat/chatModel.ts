/**
 * Модель Live Agent Chat: фильтры ↔ URL, live-элементы из SSE `chat.message`, объединение с
 * REST-лентой (getRunChat) без дублей. Тексты агентов — недоверенные данные: модель их не
 * интерпретирует, UI выводит только как текст.
 */
import type { RunEvent } from '../../api/runEvents';
import {
  CHAT_ITEM_TYPES,
  CHAT_STREAMS,
  type ChatFilters,
  type ChatItem,
  type ChatItemType,
  type ChatQuery,
  type ChatStream,
} from '../../api/chat';
import type { AgentRole } from '../../api/runs';

export const AGENT_ROLES: readonly AgentRole[] = [
  'requirements',
  'architect',
  'planner',
  'developer',
  'reviewer',
  'security',
  'qa',
  'database',
  'performance',
  'devops',
  'maintenance',
  'documentation',
];
export const MAX_MENTIONS = 12;
export const CHAT_BODY_MAX = 8000;
export const CHAT_Q_MAX = 200;

type AcpType = NonNullable<ChatItem['acp_type']>;
const ACP_TYPES: ReadonlySet<string> = new Set<AcpType>([
  'TASK_ASSIGN',
  'HANDOFF',
  'QUESTION',
  'ANSWER',
  'REVIEW_REQUEST',
  'FINDING',
  'FIX_REQUEST',
  'EVIDENCE_SUBMITTED',
  'DECISION_PROPOSAL',
  'DECISION_RECORDED',
  'BLOCKED',
  'TASK_COMPLETED',
]);
const isAcpType = (v: unknown): v is AcpType => typeof v === 'string' && ACP_TYPES.has(v);

function isRole(v: unknown): v is AgentRole {
  return typeof v === 'string' && (AGENT_ROLES as readonly string[]).includes(v);
}

/** Фильтры из query-параметров URL (ссылкой можно поделиться). */
export function filtersFromParams(params: URLSearchParams): ChatFilters {
  const stream = params.get('stream');
  const role = params.get('role');
  const types = (params.get('types') ?? '')
    .split(',')
    .filter((x): x is ChatItemType => (CHAT_ITEM_TYPES as readonly string[]).includes(x));
  return {
    stream: (CHAT_STREAMS as readonly string[]).includes(stream ?? '') ? (stream as ChatStream) : 'all_agents',
    role: isRole(role) ? role : undefined,
    task_id: params.get('task') || undefined,
    thread_id: params.get('thread') || undefined,
    q: params.get('q')?.slice(0, CHAT_Q_MAX) || undefined,
    item_type: types.length > 0 ? types : undefined,
  };
}

export function paramsFromFilters(f: ChatFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (f.stream !== 'all_agents') p.set('stream', f.stream);
  if (f.role) p.set('role', f.role);
  if (f.task_id) p.set('task', f.task_id);
  if (f.thread_id) p.set('thread', f.thread_id);
  if (f.q) p.set('q', f.q);
  if (f.item_type && f.item_type.length > 0) p.set('types', f.item_type.join(','));
  return p;
}

/** Обязательные параметры потока заданы (role при stream=role, task_id при stream=task). */
export function filtersComplete(f: ChatFilters): boolean {
  if (f.stream === 'role') return Boolean(f.role);
  if (f.stream === 'task') return Boolean(f.task_id);
  return true;
}

/** Query getRunChat: role/task_id отправляются только для своих потоков (иначе сервер сузит ленту). */
export function chatQuery(f: ChatFilters, cursor: string | null, limit: number): ChatQuery {
  return {
    stream: f.stream,
    ...(f.stream === 'role' && f.role ? { role: f.role } : {}),
    ...(f.stream === 'task' && f.task_id ? { task_id: f.task_id } : {}),
    ...(f.thread_id ? { thread_id: f.thread_id } : {}),
    ...(f.q ? { q: f.q } : {}),
    ...(f.item_type && f.item_type.length > 0 ? { item_type: f.item_type } : {}),
    order: 'desc',
    limit,
    ...(cursor ? { cursor } : {}),
  };
}

/** message_kind события chat.message (sse-event.schema.json) → вид элемента ленты ChatItem. */
const KIND: Record<string, [ChatItemType, ChatItem['message_kind']]> = {
  USER_TEXT: ['chat_message', 'text'],
  DIRECTIVE: ['directive', 'directive'],
  AGENT_MESSAGE: ['agent_message', 'agent_relay'],
  CLI_HIGHLIGHT: ['cli_excerpt', 'agent_relay'],
  APPROVAL: ['approval', 'system_notice'],
  STATUS: ['system_event', 'system_notice'],
  DIRECTIVE_STATUS: ['system_event', 'system_notice'],
  SYSTEM_NOTICE: ['system_event', 'system_notice'],
};
type RefType = NonNullable<ChatItem['refs']>[number]['type'];
/** links[].kind события chat.message = ChatItem.refs[].type (контракт выровнен). */
const REF_KINDS: ReadonlySet<string> = new Set<RefType>(['diff', 'evidence', 'test_report', 'artifact', 'finding', 'task', 'approval', 'gate_execution']);
const isRefType = (v: unknown): v is RefType => typeof v === 'string' && REF_KINDS.has(v);

function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

/** Элемент ленты, пришедший по SSE раньше, чем REST-проекция (заменяется ею по id). */
export type TimelineItem = ChatItem & { live?: boolean };

export function liveItemFromEvent(e: RunEvent): TimelineItem | null {
  if (e.type !== 'chat.message') return null;
  const p = e.payload;
  const id = str(p.chat_message_id);
  const body = typeof p.body === 'string' ? p.body : null;
  if (!id || body === null) return null;
  const [itemType, kind] = KIND[String(p.message_kind)] ?? ['system_event', 'system_notice'];
  const senderType = p.sender_type === 'user' || p.sender_type === 'agent' ? p.sender_type : 'system';
  const links = Array.isArray(p.links) ? p.links : [];
  return {
    id,
    item_type: itemType,
    thread_id: str(p.thread_id),
    task_id: str(p.task_id),
    sender_type: senderType,
    // sender_role может быть псевдоролью orchestrator/user (ACP-участник) — показывается как есть.
    sender: { user: null, role: (str(p.sender_role) as AgentRole | null) ?? null, provider: p.provider === 'codex' || p.provider === 'claude' ? p.provider : null },
    recipient_role: isRole(p.target_role) ? p.target_role : null,
    message_kind: kind,
    body,
    acp_type: isAcpType(p.acp_type) ? p.acp_type : null,
    directive_id: str(p.directive_id),
    directive_status: null,
    approval_id: str(p.approval_id),
    approval_status: null,
    mentions: Array.isArray(p.mentions) ? p.mentions.filter(isRole) : [],
    refs: links.flatMap((l: unknown) => {
      if (typeof l !== 'object' || l === null) return [];
      const { kind, id } = l as { kind?: unknown; id?: unknown };
      return isRefType(kind) && typeof id === 'string' ? [{ type: kind, id }] : [];
    }),
    source_event_id: str(p.source_event_id),
    seq: e.seq,
    created_at: str(p.created_at) ?? e.timestamp,
    live: true,
  };
}

/** Приближение серверных фильтров для live-элементов (до прихода REST-проекции). */
export function matchesFilters(item: ChatItem, f: ChatFilters): boolean {
  switch (f.stream) {
    case 'codex':
    case 'claude':
      if (item.sender?.provider !== f.stream) return false;
      break;
    case 'role':
      if (!f.role || !(item.sender?.role === f.role || item.recipient_role === f.role || (item.mentions ?? []).includes(f.role))) return false;
      break;
    case 'task':
      if (!f.task_id || item.task_id !== f.task_id) return false;
      break;
    case 'approvals':
      if (item.item_type !== 'approval') return false;
      break;
    default:
      break;
  }
  if (f.thread_id && item.thread_id !== f.thread_id) return false;
  if (f.item_type && f.item_type.length > 0 && !f.item_type.includes(item.item_type)) return false;
  if (f.q) {
    const words = f.q.toLocaleLowerCase('ru').split(/\s+/).filter(Boolean);
    const body = item.body.toLocaleLowerCase('ru');
    if (!words.every((w) => body.includes(w))) return false;
  }
  return true;
}

/** Хронологическая лента: REST (desc-страницы) + live без дублей по id. */
export function mergeTimeline(restDesc: readonly ChatItem[], live: readonly TimelineItem[]): TimelineItem[] {
  const ids = new Set(restDesc.map((i) => i.id));
  const out: TimelineItem[] = [...restDesc].reverse();
  const extra: TimelineItem[] = [];
  for (const i of live) {
    if (ids.has(i.id)) continue;
    ids.add(i.id);
    extra.push(i);
  }
  if (extra.length === 0) return out;
  return [...out, ...extra].sort((a, b) => a.created_at.localeCompare(b.created_at) || (a.seq ?? 0) - (b.seq ?? 0));
}

/** Упоминания `@role` в тексте (английские имена ролей, как в ACP). */
export function parseMentions(text: string): AgentRole[] {
  const found: AgentRole[] = [];
  for (const m of text.matchAll(/(^|[^\w@])@([a-z]+)\b/g)) {
    const role = m[2];
    if (isRole(role) && !found.includes(role)) found.push(role);
  }
  return found;
}
