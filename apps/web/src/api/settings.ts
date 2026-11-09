/** Настройки workspace: участники и роли (дополнение к ТЗ, API_DESIGN §2, AC-12). */
import { request, seg, type JsonBody, type JsonResponse, type Query, type Schemas } from './client';

export type Member = Schemas['Member'];
export type MemberCreateRequest = JsonBody<'addMember'>;
export type MemberUpdateRequest = JsonBody<'updateMember'>;
export type ListMembersQuery = Query<'listMembers'>;

export const memberKeys = {
  list: (workspaceId: string) => ['members', workspaceId] as const,
};

export const membersApi = {
  list(workspaceId: string, query: ListMembersQuery = {}, signal?: AbortSignal): Promise<JsonResponse<'listMembers', 200>> {
    return request({ method: 'GET', path: `/workspaces/${seg(workspaceId)}/members`, query, signal });
  },

  add(workspaceId: string, body: MemberCreateRequest, idempotencyKey: string): Promise<JsonResponse<'addMember', 201>> {
    return request({ method: 'POST', path: `/workspaces/${seg(workspaceId)}/members`, body, idempotencyKey });
  },

  /** Понижение последнего owner ⇒ 409 last_owner. */
  update(workspaceId: string, userId: string, body: MemberUpdateRequest, version: number): Promise<JsonResponse<'updateMember', 200>> {
    return request({
      method: 'PATCH',
      path: `/workspaces/${seg(workspaceId)}/members/${seg(userId)}`,
      body,
      ifMatchVersion: version,
    });
  },

  remove(workspaceId: string, userId: string, version: number): Promise<void> {
    return request({ method: 'DELETE', path: `/workspaces/${seg(workspaceId)}/members/${seg(userId)}`, ifMatchVersion: version });
  },
};
