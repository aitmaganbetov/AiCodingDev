import { useCallback, useState } from 'react';
import { useLocation } from 'react-router';

/**
 * Query-параметры состояния внутри экрана (вкладка, вид, фильтры): URL обновляется через
 * history.replaceState без навигации роутера, состояние применяется синхронно. Навигация
 * react-router идёт в transition: контролируемый чекбокс/вкладка, завязанные на URL, на миг
 * «откатываются» (check() в E2E и скринридер видят старое значение). Фокус от смены query не
 * зависит: PageHeading переводит его на h1 только при смене pathname. Ссылкой на URL можно поделиться.
 */
export function useInPageSearchParams(): [URLSearchParams, (next: URLSearchParams) => void] {
  const location = useLocation();
  const [state, setState] = useState(() => ({ seen: location.search, params: new URLSearchParams(location.search) }));
  let params = state.params;
  if (state.seen !== location.search) {
    // Навигация роутера (ссылка на тот же экран с другими параметрами) — источник истины URL.
    params = new URLSearchParams(location.search);
    setState({ seen: location.search, params });
  }
  const set = useCallback(
    (next: URLSearchParams) => {
      setState({ seen: location.search, params: next });
      const qs = next.toString();
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`);
    },
    [location.search],
  );
  return [params, set];
}
