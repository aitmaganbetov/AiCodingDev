import { useEffect, useState } from 'react';

/**
 * Текущее время для подписей длительности и фильтра «за период»: обновляется таймером, пока `active`
 * (по умолчанию раз в 15 с — без перерисовки ленты на каждую секунду).
 */
export function useNow(active: boolean, ms = 15_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [active, ms]);
  return now;
}
