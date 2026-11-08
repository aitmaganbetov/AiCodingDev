/** Аутентификация и сессия (API_DESIGN §3, ADR-0009). */
import { request, type JsonBody, type JsonResponse, type Schemas } from './client';

export type SessionInfo = Schemas['SessionInfo'];
export type WorkspaceMembership = Schemas['WorkspaceMembership'];
export type LoginRequest = JsonBody<'login'>;

export const authApi = {
  login(body: LoginRequest): Promise<JsonResponse<'login', 200>> {
    return request({ method: 'POST', path: '/auth/login', body, csrf: false, redirectOn401: false });
  },

  logout(): Promise<void> {
    return request({ method: 'POST', path: '/auth/logout', redirectOn401: false });
  },

  getSession(signal?: AbortSignal): Promise<JsonResponse<'getSession', 200>> {
    return request({ method: 'GET', path: '/auth/session', redirectOn401: false, signal });
  },
};
