import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { setUnauthorizedHandler } from '../api/client';
import { clearSession } from './session';

/** Связывает 401 от API (истёкшая/отозванная сессия) с переходом на страницу входа. */
export function UnauthorizedBridge() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const current = useRef('/');
  useEffect(() => {
    current.current = `${location.pathname}${location.search}`;
  }, [location.pathname, location.search]);

  useEffect(
    () =>
      setUnauthorizedHandler(() => {
        const next = current.current;
        clearSession(queryClient);
        // Уже на странице входа (поздний 401 фонового запроса оболочки: опрос CLI, поток уведомлений) —
        // не перезаписывать адрес: `next=` первого перехода должен сохраниться.
        if (next.startsWith('/login')) return;
        navigate(`/login?next=${encodeURIComponent(next)}`, { replace: true });
      }),
    [navigate, queryClient],
  );
  return null;
}
