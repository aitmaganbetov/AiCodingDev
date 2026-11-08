import { QueryClient } from '@tanstack/react-query';

/**
 * Повторы транспорта (сеть, 503, idempotency_key_in_flight) выполняет api/client.ts —
 * с тем же Idempotency-Key. На уровне TanStack Query повторов нет: 4xx детерминированы,
 * а пользователь всегда может повторить из состояния ошибки.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
}
