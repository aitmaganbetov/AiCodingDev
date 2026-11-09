/**
 * Фокус уже переведён приложением за пределы закрывающегося слоя (например, навигация
 * по ссылке из панели фокусирует h1 новой страницы) — не возвращать его на кнопку-триггер.
 */
export function focusMovedElsewhere(): boolean {
  const active = typeof document === 'undefined' ? null : document.activeElement;
  return active instanceof HTMLElement && active !== document.body && active.isConnected;
}
