/** Live Agent Chat (ТЗ §11, FR-CHAT-01/02/06): единая лента run с потоками-фильтрами. */
import { request, seg, type JsonBody, type Query, type Schemas } from './client';

export type ChatItem = Schemas['ChatItem'];
export type ChatItemType = ChatItem['item_type'];
export type ChatPage = Schemas['ChatPage'];
export type ChatThread = Schemas['ChatThread'];
export type ChatStream = Schemas['ChatStream'];
export type ChatMessage = Schemas['ChatMessage'];
export type ChatMessageCreateRequest = JsonBody<'postChatMessage'>;
export type ChatQuery = Query<'getRunChat'>;

export const CHAT_STREAMS: readonly ChatStream[] = ['all_agents', 'codex', 'claude', 'role', 'task', 'approvals'];
export const CHAT_ITEM_TYPES: readonly ChatItemType[] = [
  'agent_message',
  'chat_message',
  'directive',
  'approval',
  'cli_excerpt',
  'system_event',
];

/** Фильтры ленты (часть ключа кэша). */
export interface ChatFilters {
  stream: ChatStream;
  role?: Schemas['AgentRole'] | undefined;
  task_id?: string | undefined;
  thread_id?: string | undefined;
  q?: string | undefined;
  item_type?: ChatItemType[] | undefined;
}

export const chatKeys = {
  all: (runId: string) => ['chat', runId] as const,
  feed: (runId: string, filters: ChatFilters) => ['chat', runId, 'feed', filters] as const,
};

export const chatApi = {
  feed(runId: string, query: ChatQuery = {}, signal?: AbortSignal): Promise<ChatPage> {
    return request({ method: 'GET', path: `/runs/${seg(runId)}/chat`, query, signal });
  },

  /** Комментарий (агентам не доставляется; для воздействия — директива). 201 ChatMessage. */
  post(runId: string, body: ChatMessageCreateRequest, idempotencyKey: string): Promise<ChatMessage> {
    return request({ method: 'POST', path: `/runs/${seg(runId)}/chat/messages`, body, idempotencyKey });
  },
};
