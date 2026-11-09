import { useCallback, useEffect, useRef } from 'react';
import { useSearchParams } from 'react-router';

/**
 * Параметры URL экрана-фильтра с обновлением «от последних выставленных», а не от снимка рендера:
 * несколько быстрых изменений фильтров подряд (до того, как роутер применил предыдущее) не теряют
 * друг друга. Промежуточные коммиты роутера (ещё не догнавшие последнее обновление) игнорируются;
 * внешняя навигация (ссылка, «назад») принимается, когда ожидающих обновлений нет.
 * Обновление — `replace` (состояние того же экрана, не новая запись истории).
 */
export function useLatestSearchParams(): [URLSearchParams, (update: (prev: URLSearchParams) => URLSearchParams) => void] {
  const [params, setSearchParams] = useSearchParams();
  const latest = useRef(params);
  const pending = useRef<string | null>(null);
  useEffect(() => {
    const current = params.toString();
    if (pending.current === null || pending.current === current) {
      pending.current = null;
      latest.current = params;
    }
  }, [params]);
  const update = useCallback(
    (fn: (prev: URLSearchParams) => URLSearchParams) => {
      const next = fn(new URLSearchParams(latest.current));
      latest.current = next;
      pending.current = next.toString();
      setSearchParams(next, { replace: true });
    },
    [setSearchParams],
  );
  return [params, update];
}
