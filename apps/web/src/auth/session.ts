import { useQuery, type QueryClient } from '@tanstack/react-query';
import { authApi, type SessionInfo } from '../api/auth';
import { isApiError, setCsrfToken } from '../api/client';

export const sessionQueryKey = ['session'] as const;

/** null — пользователь не аутентифицирован (GET /auth/session → 401). */
export async function fetchSession(signal?: AbortSignal): Promise<SessionInfo | null> {
  try {
    const session = await authApi.getSession(signal);
    setCsrfToken(session.csrf_token);
    return session;
  } catch (e) {
    if (isApiError(e) && e.status === 401) {
      setCsrfToken(null);
      return null;
    }
    throw e;
  }
}

export function useSession() {
  return useQuery({
    queryKey: sessionQueryKey,
    queryFn: ({ signal }) => fetchSession(signal),
    staleTime: 60_000,
  });
}

// Признак явного выхода: страница входа показывает уведомление, а охрана маршрутов
// не добавляет ?next= (пользователь сам ушёл со страницы).
let loggedOut = false;
export function wasLoggedOut(): boolean {
  return loggedOut;
}

export function storeSession(queryClient: QueryClient, session: SessionInfo): void {
  loggedOut = false;
  setCsrfToken(session.csrf_token);
  queryClient.setQueryData(sessionQueryKey, session);
}

/** Локальная очистка после logout/401: CSRF-токен и все кэши данных. */
export function clearSession(queryClient: QueryClient, opts: { explicitLogout?: boolean } = {}): void {
  loggedOut = opts.explicitLogout === true;
  setCsrfToken(null);
  queryClient.cancelQueries().catch(() => undefined);
  queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== sessionQueryKey[0] });
  queryClient.setQueryData(sessionQueryKey, null);
}

/** Безопасный внутренний путь для возврата после входа (защита от open redirect). */
export function safeNextPath(next: string | null): string | null {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return null;
  if (next.startsWith('/login')) return null;
  return next;
}
