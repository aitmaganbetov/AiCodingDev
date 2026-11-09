import type { RouteObject } from 'react-router';
import type { IconName } from '../components/ui/Icon';
import type { Permission } from '../lib/permissions';

/** Группы sidebar (план §6.1, D-3): Обзор · Исполнение · Код и качество · Платформа. */
export type NavGroup = 'overview' | 'execution' | 'code' | 'platform';

/**
 * Ширина рабочей области экрана (F2): `standard` — данные до 1600 px (по умолчанию), `form` — формы
 * и прозаические экраны (≈ 72ch), `full` — рабочие экраны во всю ширину (граф, лента, split view).
 * Задаётся в `handle` маршрута: `{ path, element, handle: { width: 'full' } satisfies RouteHandle }`.
 */
export type MainWidth = 'standard' | 'form' | 'full';
export interface RouteHandle {
  width?: MainWidth;
}

/** Пункт боковой навигации области (виден, когда выбран workspace). */
export interface AreaNav {
  /** Ключ i18n подписи (nav.json). */
  labelKey: string;
  /** Путь относительно /w/:workspaceId/. */
  path: string;
  icon: IconName;
  group: NavGroup;
  /** Порядок внутри группы. */
  order: number;
  /** Буква сочетания «g, затем буква». */
  shortcut?: string;
  /** Скрыть пункт для ролей без права (только UX; сервер всё равно проверяет). */
  permission?: Permission;
  /** Счётчик рядом с пунктом (реальные данные оболочки): `approvals` — ожидают моего решения. */
  badge?: 'approvals';
}

/**
 * Модуль области (экрана) Mission Control: маршруты + пункт навигации.
 * Каждая область — свой файл src/routes/<area>.tsx; App.tsx собирает их из src/routes/index.ts.
 */
export interface AreaModule {
  id: string;
  nav?: AreaNav;
  /** Маршруты внутри /w/:workspaceId/ (пути относительные, без ведущего «/»). Членство уже проверено. */
  workspaceRoutes?: RouteObject[];
  /** Маршруты вне workspace (внутри layout, после входа). */
  routes?: RouteObject[];
  /** Экран «домой» после входа: путь внутри workspace; выигрывает наибольший priority. */
  home?: { path: string; priority: number };
}
